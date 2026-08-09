"""pymoo backend: multi-objective evolutionary algorithms.

Provides NSGA-II, NSGA-III, MOEA/D, SMS-EMOA, and single-objective GA.
Uses the same ``SolverEvaluationSession`` interface as the SciPy backend,
so no evaluation logic is duplicated.
"""

from __future__ import annotations

import logging
import time
from typing import Any

import numpy as np

from lib_runtime.solver.cancellation_token import CancellationToken
from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_BACKEND_FAILED,
)
from lib_runtime.solver.solver_types import (
    SolverProblem,
    SolverResult,
    SolverPoint,
    TerminationStatus,
)
from lib_runtime.solver.solver_evaluation_session import SolverEvaluationSession

logger = logging.getLogger(__name__)


def _build_problem_object(
    problem: SolverProblem,
    session: SolverEvaluationSession,
    cancellation_token: CancellationToken,
    deadline: float | None,
):
    """Build a pymoo Problem object that delegates to the evaluation session."""
    from pymoo.core.problem import Problem

    n_obj = problem.n_objectives
    n_con = problem.n_constraints

    directions = [o.direction for o in problem.objectives]

    class _EngineProblem(Problem):
        def __init__(self):
            xl = np.array([
                v.lower_bound if v.lower_bound is not None else -1e12
                for v in problem.variables
            ], dtype=float)
            xu = np.array([
                v.upper_bound if v.upper_bound is not None else 1e12
                for v in problem.variables
            ], dtype=float)
            super().__init__(n_var=problem.n_variables, n_obj=n_obj, n_ieq_constr=n_con,
                             xl=xl, xu=xu, vtype=float)

        def _evaluate(self, X, out, *args, **kwargs):
            n = X.shape[0]
            F = np.zeros((n, n_obj))
            G = np.zeros((n, n_con)) if n_con > 0 else None

            for i in range(n):
                if cancellation_token.is_cancelled:
                    break
                if deadline is not None and time.time() > deadline:
                    cancellation_token.cancel()
                    break

                x_list = list(X[i])
                result = session.evaluate_candidate(x_list)

                for j, direction in enumerate(directions):
                    val = float(result["objectives"][j]) if j < len(result["objectives"]) else 0.0
                    # pymoo always minimizes; negate for maximize
                    if direction == "maximize":
                        F[i, j] = -val
                    else:
                        F[i, j] = val

                if n_con > 0:
                    for j, con in enumerate(problem.constraints):
                        raw_val = float(result["constraints"][j]) if j < len(result.get("constraints", [])) else 0.0
                        if con.constraint_type == "upper":
                            bv = con.bound_value if con.bound_value is not None else con.upper_bound_value
                            G[i, j] = raw_val - (bv or 0.0)
                        elif con.constraint_type == "lower":
                            bv = con.bound_value if con.bound_value is not None else con.lower_bound_value
                            G[i, j] = (bv or 0.0) - raw_val
                        elif con.constraint_type == "equality":
                            bv = con.bound_value or 0.0
                            G[i, j] = abs(raw_val - bv)
                        elif con.constraint_type == "range":
                            lbv = con.lower_bound_value
                            ubv = con.upper_bound_value
                            if ubv is not None and lbv is not None:
                                G[i, j] = max(raw_val - ubv, lbv - raw_val)
                            elif ubv is not None:
                                G[i, j] = raw_val - ubv
                            elif lbv is not None:
                                G[i, j] = lbv - raw_val
                            else:
                                G[i, j] = raw_val
                        else:
                            G[i, j] = raw_val

            out["F"] = F
            if n_con > 0:
                out["G"] = G

    return _EngineProblem()


def _termination_from_limits(limits: dict[str, Any]):
    """Build a pymoo termination criterion from solver limits."""
    from pymoo.termination import get_termination

    max_iter = int(limits.get("max_iterations", 200))
    tol = float(limits.get("tol", 1e-6))

    return get_termination("n_gen", max_iter)


def _unnegate_objectives(F_row, directions):
    """Convert pymoo minimized objectives back to user-facing values."""
    out = []
    for j, direction in enumerate(directions):
        val = float(F_row[j]) if j < len(F_row) else 0.0
        if direction == "maximize":
            val = -val
        out.append(val)
    return out


def _extract_pareto_front(result, problem, directions):
    """Extract non-dominated solutions from pymoo result population.

    Returns a list of SolverPoint, deterministically sorted by normalized
    objective vector (lexicographic), with variable vector as tie-breaker.
    """
    if not hasattr(result, "pop") or result.pop is None or len(result.pop) == 0:
        return []

    pop = result.pop
    X_all = pop.get("X")
    F_all = pop.get("F")
    G_all = pop.get("G") if problem.n_constraints > 0 else None

    if X_all is None or F_all is None or len(X_all) == 0:
        return []

    n = len(X_all)
    n_obj = problem.n_objectives
    n_con = problem.n_constraints

    # Compute constraint violation per solution.
    cv = np.zeros(n)
    if G_all is not None and n_con > 0:
        cv = np.sum(np.maximum(G_all, 0.0), axis=1)

    # Filter to feasible solutions.
    feasible_mask = cv <= 1e-6
    if np.any(feasible_mask):
        indices = np.where(feasible_mask)[0]
    else:
        # No feasible solutions — include all (least-infeasible will sort first).
        indices = np.arange(n)

    X_pool = X_all[indices]
    F_pool = F_all[indices]
    G_pool = G_all[indices] if G_all is not None else None

    # Find non-dominated set within the pool.
    # (pymoo's final population should already be non-dominated, but
    # we re-check to be safe, especially if feasibility filtering removed some.)
    nd_indices = _find_non_dominated(F_pool)

    points = []
    for idx in nd_indices:
        var_vals = [float(v) for v in X_pool[idx]]
        obj_vals = _unnegate_objectives(F_pool[idx], directions)
        con_vals = None
        if G_pool is not None:
            con_vals = [float(v) for v in G_pool[idx]]
        points.append(SolverPoint(
            variable_values=var_vals,
            objective_values=obj_vals,
            constraint_values=con_vals,
        ))

    # Deterministic sort: lexicographic by normalized objective vector,
    # with variable vector as tie-breaker.
    if len(points) > 1:
        points = _deterministic_sort(points, n_obj)

    return points


def _find_non_dominated(F):
    """Return indices of non-dominated solutions (minimization sense).

    F is (n, n_obj) array. Solution i dominates j if i is <= in all
    objectives and < in at least one.
    """
    n = len(F)
    if n <= 1:
        return list(range(n))

    non_dominated = []
    for i in range(n):
        dominated = False
        for j in range(n):
            if i == j:
                continue
            # j dominates i if j <= i in all objectives and j < i in at least one.
            if np.all(F[j] <= F[i] + 1e-12) and np.any(F[j] < F[i] - 1e-12):
                dominated = True
                break
        if not dominated:
            non_dominated.append(i)
    return non_dominated


def _deterministic_sort(points, n_obj):
    """Sort points lexicographically by normalized objective vector.

    Normalization maps each objective to [0, 1] across the front.
    Variable vector is used as a final tie-breaker for full determinism.
    """
    if len(points) <= 1:
        return points

    import numpy as np

    F = np.array([[p.objective_values[j] for j in range(n_obj)] for p in points])
    # Normalize per objective.
    mins = F.min(axis=0)
    maxs = F.max(axis=0)
    ranges = maxs - mins
    ranges[ranges == 0] = 1.0
    F_norm = (F - mins) / ranges

    # Sort key: (normalized objectives..., variable values...)
    indexed = list(enumerate(points))
    indexed.sort(key=lambda kv: (
        tuple(round(float(v), 12) for v in F_norm[kv[0]]),
        tuple(round(float(v), 12) for v in kv[1].variable_values),
    ))
    return [p for _, p in indexed]


def _result_from_pymoo(result, problem: SolverProblem, eval_count: int,
                       timed_out: bool, cancelled: bool) -> SolverResult:
    """Convert pymoo result to SolverResult."""
    if timed_out:
        term = TerminationStatus.TIMEOUT
    elif cancelled:
        term = TerminationStatus.CANCELLED
    elif result.X is not None or (hasattr(result, "pop") and result.pop is not None and len(result.pop) > 0):
        term = TerminationStatus.OPTIMAL
    else:
        term = TerminationStatus.FAILED

    directions = [o.direction for o in problem.objectives]

    metadata = {
        "backend": "pymoo",
        "algorithm": problem.algorithm,
        "n_evals": eval_count,
    }
    if hasattr(result, "pop") and result.pop is not None:
        metadata["population_size"] = len(result.pop)

    solution = None
    pareto_front = None

    if problem.n_objectives > 1:
        # Multi-objective: extract Pareto front.
        pareto_front = _extract_pareto_front(result, problem, directions)
        if pareto_front:
            metadata["pareto_front_size"] = len(pareto_front)
        elif result.X is not None:
            # Fallback: single solution from result.X.
            X = result.X
            if X.ndim > 1:
                X = X[0]
            F = result.F
            if F.ndim > 1:
                F = F[0]
            con_vals = None
            if result.G is not None and problem.n_constraints > 0:
                G = result.G
                if G.ndim > 1:
                    G = G[0]
                con_vals = [float(v) for v in G]
            solution = SolverPoint(
                variable_values=[float(v) for v in X],
                objective_values=_unnegate_objectives(F, directions),
                constraint_values=con_vals,
            )
    else:
        # Single-objective: extract best solution.
        if result.X is not None:
            X = result.X
            if X.ndim > 1:
                X = X[0]
            F = result.F
            if F is not None:
                if F.ndim > 1:
                    F = F[0]
                obj_vals = _unnegate_objectives(F, directions)
            else:
                obj_vals = [0.0]
            con_vals = None
            if result.G is not None and problem.n_constraints > 0:
                G = result.G
                if G.ndim > 1:
                    G = G[0]
                con_vals = [float(v) for v in G]
            solution = SolverPoint(
                variable_values=[float(v) for v in X],
                objective_values=obj_vals,
                constraint_values=con_vals,
            )

    return SolverResult(
        termination_status=term,
        n_evaluations=eval_count,
        backend_metadata=metadata,
        message="pymoo optimization complete" if term == TerminationStatus.OPTIMAL else "",
        solution=solution,
        pareto_front=pareto_front,
    )


def solve(problem: SolverProblem, session: SolverEvaluationSession,
          cancellation_token: CancellationToken, limits=None) -> SolverResult:
    """Dispatch to the appropriate pymoo algorithm based on problem.algorithm."""
    limits = limits or {}
    algorithm = problem.algorithm
    maxiter = int(limits.get("max_iterations", 200))
    max_wall_time = float(limits.get("max_wall_time_seconds", 300.0))
    deadline = time.time() + max_wall_time if max_wall_time > 0 else None

    if problem.n_variables == 0:
        raise SolverException(SOLVER_BACKEND_FAILED, "Cannot solve problem with 0 variables")

    from pymoo.algorithms.moo.nsga2 import NSGA2
    from pymoo.algorithms.moo.nsga3 import NSGA3
    from pymoo.algorithms.moo.moead import MOEAD
    from pymoo.algorithms.moo.sms import SMSEMOA
    from pymoo.algorithms.soo.nonconvex.ga import GA
    from pymoo.optimize import minimize as pymoo_minimize

    pop_size = int(limits.get("pop_size", 100))

    if algorithm == "auto":
        if problem.n_objectives <= 1:
            algorithm = "ga"
        elif problem.n_objectives <= 3:
            algorithm = "nsga2"
        else:
            algorithm = "nsga3"

    if algorithm == "nsga2":
        algo = NSGA2(pop_size=pop_size)
    elif algorithm == "nsga3":
        from pymoo.util.ref_dirs import get_reference_directions
        ref_dirs = get_reference_directions("das-dennis", problem.n_objectives, n_partitions=12)
        algo = NSGA3(pop_size=pop_size, ref_dirs=ref_dirs)
    elif algorithm == "moead":
        algo = MOEAD(pop_size=pop_size)
    elif algorithm == "sms-emoa":
        algo = SMSEMOA(pop_size=pop_size)
    elif algorithm == "ga":
        algo = GA(pop_size=pop_size)
    else:
        raise SolverException(SOLVER_BACKEND_FAILED, f"Unknown pymoo algorithm: {algorithm}")

    pymoo_problem = _build_problem_object(problem, session, cancellation_token, deadline)
    termination = _termination_from_limits(limits)

    eval_count = 0
    _orig_evaluate = session.evaluate_candidate

    def _counting_evaluate(values):
        nonlocal eval_count
        eval_count += 1
        return _orig_evaluate(values)

    session.evaluate_candidate = _counting_evaluate

    timed_out = False
    cancelled = False

    try:
        result = pymoo_minimize(
            pymoo_problem,
            algo,
            termination=termination,
            seed=int(limits.get("seed", 42)),
            verbose=False,
        )
    except Exception as e:
        logger.exception("pymoo solve failed")
        if cancellation_token.is_cancelled:
            cancelled = True
            result = type("EmptyResult", (), {"X": None, "F": None, "G": None, "pop": None})()
        else:
            raise
    finally:
        session.evaluate_candidate = _orig_evaluate

    if deadline is not None and time.time() > deadline:
        timed_out = True

    return _result_from_pymoo(result, problem, eval_count, timed_out, cancelled)
