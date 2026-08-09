"""SciPy backend: COBYLA, SLSQP, trust-constr (NLP) and linprog/HiGHS (LP)."""

from __future__ import annotations

import logging
import time
import warnings
from typing import Any

import numpy as np
from scipy.optimize import minimize, linprog

from lib_runtime.solver.cancellation_token import CancellationToken
from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_BACKEND_FAILED,
    SOLVER_LIMIT_EXCEEDED,
    SOLVER_NONLINEAR_MODEL_REJECTED_BY_LP_BACKEND,
    SOLVER_CONSTRAINT_UNSUPPORTED,
)
from lib_runtime.solver.solver_types import (
    SolverProblem,
    SolverResult,
    SolverPoint,
    TerminationStatus,
)
from lib_runtime.solver.solver_evaluation_session import SolverEvaluationSession
from lib_runtime.solver.lp_extractor import extract_lp_matrix, LPMatrix

logger = logging.getLogger(__name__)


def solve_cobyla(
    problem: SolverProblem,
    session: SolverEvaluationSession,
    cancellation_token: CancellationToken,
    *,
    maxiter: int = 1000,
    rhobeg: float = 1.0,
    tol: float = 1e-6,
    deadline: float | None = None,
) -> SolverResult:
    """Run COBYLA optimization against the live-Engine evaluation session."""
    if problem.n_variables == 0:
        raise SolverException(SOLVER_BACKEND_FAILED, "Cannot solve problem with 0 variables")

    x0 = np.array([v.initial_value for v in problem.variables], dtype=float)
    eval_count = 0
    timed_out = False
    _last_x: list[float] | None = None
    _last_result: dict[str, Any] | None = None

    def _evaluate(x):
        nonlocal eval_count, _last_x, _last_result, timed_out
        eval_count += 1
        if deadline is not None and time.time() > deadline:
            timed_out = True
            cancellation_token.cancel()
        if cancellation_token.is_cancelled:
            if _last_result is not None:
                return _last_result
            return {"objectives": [0.0], "constraints": [0.0] * problem.n_constraints}
        x_list = list(x)
        result = session.evaluate_candidate(x_list)
        _last_x = x_list
        _last_result = result
        return result

    direction = problem.objectives[0].direction if problem.objectives else "minimize"
    sign = -1.0 if direction == "maximize" else 1.0

    def signed_objective_fn(x):
        r = _evaluate(x)
        return sign * float(r["objectives"][0])

    def _read_con(x, idx):
        r = _evaluate(x)
        if idx < len(r["constraints"]):
            return float(r["constraints"][idx])
        return 0.0

    scipy_constraints = []
    for idx, con in enumerate(problem.constraints):
        if con.constraint_type == "lower":
            lb = con.bound_value if con.bound_value is not None else con.lower_bound_value
            if lb is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _lb=lb: _read_con(x, _i) - _lb})
        elif con.constraint_type == "upper":
            ub = con.bound_value if con.bound_value is not None else con.upper_bound_value
            if ub is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _ub=ub: _ub - _read_con(x, _i)})
        elif con.constraint_type == "range":
            lbv, ubv = con.lower_bound_value, con.upper_bound_value
            if ubv is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _ub=ubv: _ubv - _read_con(x, _i)})
            if lbv is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _lb=lbv: _read_con(x, _i) - _lbv})
        elif con.constraint_type == "equality":
            val = con.bound_value
            if val is not None:
                scipy_constraints.append({"type": "eq", "fun": lambda x, _i=idx, _v=val: _read_con(x, _i) - _v})

    callback_count = 0
    def callback(xk, *args, **kwargs):
        nonlocal callback_count
        callback_count += 1
        if cancellation_token.is_cancelled:
            raise StopIteration

    bounds_constraints = []
    for i, var in enumerate(problem.variables):
        if var.lower_bound is not None:
            bounds_constraints.append({"type": "ineq", "fun": lambda x, _i=i, _lb=var.lower_bound: x[_i] - _lb})
        if var.upper_bound is not None:
            bounds_constraints.append({"type": "ineq", "fun": lambda x, _i=i, _ub=var.upper_bound: _ub - x[_i]})

    all_constraints = bounds_constraints + scipy_constraints

    try:
        result = minimize(
            signed_objective_fn, x0=x0, method="COBYLA",
            constraints=all_constraints if all_constraints else None,
            callback=callback,
            options={"maxiter": maxiter, "rhobeg": rhobeg, "tol": tol},
        )
    except StopIteration:
        best_x = _last_x if _last_x is not None else list(x0)
        best_obj = _last_result["objectives"] if _last_result else [0.0]
        if timed_out:
            return SolverResult(TerminationStatus.TIMEOUT, n_evaluations=eval_count, message="Time limit exceeded",
                            solution=SolverPoint(variable_values=best_x, objective_values=best_obj))
        return SolverResult(TerminationStatus.CANCELLED, n_evaluations=eval_count, message="Cancelled by user",
                            solution=SolverPoint(variable_values=best_x, objective_values=best_obj))

    if timed_out:
        term_status, message = TerminationStatus.TIMEOUT, "Time limit exceeded"
    elif cancellation_token.is_cancelled:
        term_status, message = TerminationStatus.CANCELLED, "Cancelled by user"
    elif result.success:
        term_status, message = TerminationStatus.OPTIMAL, result.message
    else:
        term_status, message = TerminationStatus.FAILED, result.message

    final_objectives = []
    if problem.objectives:
        try:
            final_result = session.evaluate_candidate(list(result.x))
            final_objectives = final_result["objectives"]
        except Exception:
            final_objectives = [sign * result.fun]

    final_constraints = None
    if problem.n_constraints > 0:
        try:
            final_result = session.evaluate_candidate(list(result.x))
            final_constraints = final_result["constraints"]
        except Exception:
            pass

    return SolverResult(
        term_status, n_evaluations=eval_count,
        backend_metadata={"scipy_success": result.success, "scipy_status": result.status,
                          "scipy_nit": getattr(result, "nit", None), "callback_count": callback_count},
        message=message,
        solution=SolverPoint(
            variable_values=[float(v) for v in result.x],
            objective_values=final_objectives,
            constraint_values=final_constraints,
        ),
    )


def _build_scipy_constraints(problem, _read_con):
    """Build scipy constraint dicts from problem constraints.

    Shared by SLSQP and trust-constr. Returns list of {'type': 'ineq'|'eq', 'fun': callback}.
    """
    scipy_constraints = []
    for idx, con in enumerate(problem.constraints):
        if con.constraint_type == "lower":
            lb = con.bound_value if con.bound_value is not None else con.lower_bound_value
            if lb is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _lb=lb: _read_con(x, _i) - _lb})
        elif con.constraint_type == "upper":
            ub = con.bound_value if con.bound_value is not None else con.upper_bound_value
            if ub is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _ub=ub: _ub - _read_con(x, _i)})
        elif con.constraint_type == "range":
            lbv, ubv = con.lower_bound_value, con.upper_bound_value
            if ubv is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _ub=ubv: _ubv - _read_con(x, _i)})
            if lbv is not None:
                scipy_constraints.append({"type": "ineq", "fun": lambda x, _i=idx, _lb=lbv: _read_con(x, _i) - _lbv})
        elif con.constraint_type == "equality":
            val = con.bound_value
            if val is not None:
                scipy_constraints.append({"type": "eq", "fun": lambda x, _i=idx, _v=val: _read_con(x, _i) - _v})
    return scipy_constraints


def _make_evaluation_closure(problem, session, cancellation_token, deadline):
    """Create shared evaluation, objective, and constraint-reading closures.

    Returns (signed_objective_fn, _read_con, eval_count_ref, last_x_ref, last_result_ref, timed_out_ref).
    """
    eval_count = 0
    timed_out = False
    _last_x = None
    _last_result = None

    def _evaluate(x):
        nonlocal eval_count, _last_x, _last_result, timed_out
        eval_count += 1
        if deadline is not None and time.time() > deadline:
            timed_out = True
            cancellation_token.cancel()
        if cancellation_token.is_cancelled:
            if _last_result is not None:
                return _last_result
            return {"objectives": [0.0], "constraints": [0.0] * problem.n_constraints}
        x_list = list(x)
        result = session.evaluate_candidate(x_list)
        _last_x = x_list
        _last_result = result
        return result

    direction = problem.objectives[0].direction if problem.objectives else "minimize"
    sign = -1.0 if direction == "maximize" else 1.0

    def signed_objective_fn(x):
        r = _evaluate(x)
        return sign * float(r["objectives"][0])

    def _read_con(x, idx):
        r = _evaluate(x)
        if idx < len(r["constraints"]):
            return float(r["constraints"][idx])
        return 0.0

    return signed_objective_fn, _read_con, lambda: eval_count, lambda: _last_x, lambda: _last_result, lambda: timed_out


def solve_gradient_nlp(
    problem: SolverProblem,
    session: SolverEvaluationSession,
    cancellation_token: CancellationToken,
    *,
    method: str,
    maxiter: int = 1000,
    tol: float = 1e-6,
    deadline: float | None = None,
) -> SolverResult:
    """Run a gradient-based NLP solver (SLSQP or trust-constr) with finite-difference gradients."""
    if problem.n_variables == 0:
        raise SolverException(SOLVER_BACKEND_FAILED, "Cannot solve problem with 0 variables")

    x0 = np.array([v.initial_value for v in problem.variables], dtype=float)
    signed_obj, _read_con, get_eval_count, get_last_x, get_last_result, get_timed_out = \
        _make_evaluation_closure(problem, session, cancellation_token, deadline)

    scipy_constraints = _build_scipy_constraints(problem, _read_con)

    bounds = []
    for var in problem.variables:
        lb = var.lower_bound if var.lower_bound is not None else -np.inf
        ub = var.upper_bound if var.upper_bound is not None else np.inf
        bounds.append((lb, ub))

    callback_count = 0
    def callback(xk, *args, **kwargs):
        nonlocal callback_count
        callback_count += 1
        if cancellation_token.is_cancelled:
            raise StopIteration

    options = {"maxiter": maxiter}
    if method == "SLSQP":
        options["ftol"] = tol
    elif method == "trust-constr":
        options["gtol"] = tol
    elif method == "Nelder-Mead":
        options["xatol"] = tol
        options["fatol"] = tol
    elif method == "Powell":
        options["ftol"] = tol

    try:
        with warnings.catch_warnings():
            warnings.filterwarnings(
                "ignore",
                message="delta_grad == 0.0",
                category=UserWarning,
            )
            result = minimize(
                signed_obj, x0=x0, method=method,
                bounds=bounds,
                constraints=scipy_constraints if scipy_constraints else None,
                callback=callback,
                options=options,
            )
    except StopIteration:
        best_x = get_last_x() if get_last_x() is not None else list(x0)
        best_obj = get_last_result()["objectives"] if get_last_result() else [0.0]
        if get_timed_out():
            return SolverResult(TerminationStatus.TIMEOUT, n_evaluations=get_eval_count(), message="Time limit exceeded",
                                solution=SolverPoint(variable_values=best_x, objective_values=best_obj))
        return SolverResult(TerminationStatus.CANCELLED, n_evaluations=get_eval_count(), message="Cancelled by user",
                            solution=SolverPoint(variable_values=best_x, objective_values=best_obj))

    timed_out = get_timed_out()
    if timed_out:
        term_status, message = TerminationStatus.TIMEOUT, "Time limit exceeded"
    elif cancellation_token.is_cancelled:
        term_status, message = TerminationStatus.CANCELLED, "Cancelled by user"
    elif result.success:
        term_status, message = TerminationStatus.OPTIMAL, result.message
    else:
        term_status, message = TerminationStatus.FAILED, result.message

    final_objectives = []
    if problem.objectives:
        try:
            final_result = session.evaluate_candidate(list(result.x))
            final_objectives = final_result["objectives"]
        except Exception:
            final_objectives = [result.fun]

    final_constraints = None
    if problem.n_constraints > 0:
        try:
            final_result = session.evaluate_candidate(list(result.x))
            final_constraints = final_result["constraints"]
        except Exception:
            pass

    return SolverResult(
        term_status, n_evaluations=get_eval_count(),
        backend_metadata={"scipy_success": result.success, "scipy_status": result.status,
                          "scipy_nit": getattr(result, "nit", None), "callback_count": callback_count,
                          "method": method},
        message=message,
        solution=SolverPoint(
            variable_values=[float(v) for v in result.x],
            objective_values=final_objectives,
            constraint_values=final_constraints,
        ),
    )


def solve_linprog(problem, session, cancellation_token, *, lp_matrix=None, deadline=None):
    """Solve an LP problem using scipy.optimize.linprog (HiGHS)."""
    if problem.n_variables == 0:
        raise SolverException(SOLVER_BACKEND_FAILED, "Cannot solve LP with 0 variables")

    if lp_matrix is None:
        lp_matrix = extract_lp_matrix(problem, session)

    if cancellation_token.is_cancelled:
        return SolverResult(TerminationStatus.CANCELLED,
                            n_evaluations=lp_matrix.n_evaluations, message="Cancelled before LP solve",
                            solution=SolverPoint(
                                variable_values=[v.initial_value for v in problem.variables],
                                objective_values=[0.0],
                            ))

    result = linprog(c=lp_matrix.c, A_ub=lp_matrix.A_ub, b_ub=lp_matrix.b_ub,
                     A_eq=lp_matrix.A_eq, b_eq=lp_matrix.b_eq,
                     bounds=list(zip(lp_matrix.lb, lp_matrix.ub)), method="highs")

    if cancellation_token.is_cancelled:
        term_status, message = TerminationStatus.CANCELLED, "Cancelled by user"
    elif result.success:
        term_status, message = TerminationStatus.OPTIMAL, result.message
    elif result.status == 2:
        term_status, message = TerminationStatus.INFEASIBLE, "Problem appears infeasible"
    elif result.status == 3:
        term_status, message = TerminationStatus.FAILED, "Problem appears unbounded"
    else:
        term_status, message = TerminationStatus.FAILED, result.message

    if not result.success:
        return SolverResult(term_status,
                            n_evaluations=lp_matrix.n_evaluations,
                            backend_metadata={"lp_matrix_version": lp_matrix.version, "lp_source": lp_matrix.source,
                                              "linprog_status": result.status, "linprog_success": result.success},
                            message=message,
                            solution=SolverPoint(
                                variable_values=[v.initial_value for v in problem.variables],
                                objective_values=[],
                            ))

    final_objectives, final_constraints = [], None
    try:
        final_result = session.evaluate_candidate(list(result.x))
        final_objectives = final_result["objectives"]
        final_constraints = final_result["constraints"] if problem.n_constraints > 0 else None
    except Exception:
        final_objectives = [float(result.fun)]

    return SolverResult(
        term_status, n_evaluations=lp_matrix.n_evaluations,
        backend_metadata={"lp_matrix_version": lp_matrix.version, "lp_source": lp_matrix.source,
                          "linprog_status": result.status, "linprog_success": result.success,
                          "max_validation_error": lp_matrix.max_validation_error},
        message=message,
        solution=SolverPoint(
            variable_values=[float(v) for v in result.x],
            objective_values=final_objectives,
            constraint_values=final_constraints,
        ),
    )


_PDFO_FALLBACK_NOTICE = """\
╔══════════════════════════════════════════════════════════════════════════════╗
║  ALGORITHM NOTICE: {algo_upper} is falling back to COBYLA                    ║
╠══════════════════════════════════════════════════════════════════════════════╣
║                                                                              ║
║  You selected the '{algo}' algorithm, which is part of Powell's              ║
║  derivative-free optimization methods (BOBYQA, NEWUOA, LINCOA).              ║
║                                                                              ║
║  These algorithms are provided by the 'pdfo' package (BSD-3-Clause),         ║
║  which contains compiled Fortran modules built against NumPy 1.x.            ║
║  Your environment uses NumPy {numpy_ver}, which is incompatible with         ║
║  these pre-compiled binaries.                                                ║
║                                                                              ║
║  The solver will proceed using COBYLA instead. COBYLA is the most            ║
║  general of Powell's methods and supports all constraint types,              ║
║  so your problem will be solved correctly. However, the specific             ║
║  algorithmic advantages of {algo_upper} (e.g. trust-region geometry,         ║
║  quadratic models, or linear constraint handling) will not be utilised.      ║
║                                                                              ║
║  WHEN WILL THIS BE FIXED?                                                    ║
║                                                                              ║
║  The PRIMA project (libprima/prima on GitHub) is preparing a pure-Python     ║
║  translation of all Powell methods with proper NumPy 2.x wheels.             ║
║  The consolidation PR is currently in draft status:                          ║
║                                                                              ║
║    https://github.com/libprima/prima/pull/277                                ║
║                                                                              ║
║  Once that PR is merged and wheels are published to PyPI,                    ║
║  'pip install prima' will provide BOBYQA, NEWUOA, LINCOA, UOBYQA,            ║
║  and COBYLA — all compatible with NumPy 2.x.                                 ║
║                                                                              ║
║  Until then, the fallback to COBYLA ensures correct results.                 ║
╚══════════════════════════════════════════════════════════════════════════════╝
"""


def _print_pdfo_fallback_notice(algorithm: str) -> None:
    """Print a detailed notice when a pdfo algorithm falls back to COBYLA."""
    import sys
    notice = _PDFO_FALLBACK_NOTICE.format(
        algo=algorithm,
        algo_upper=algorithm.upper(),
        numpy_ver=np.__version__,
    )
    print(notice, file=sys.stderr)
    logger.warning("Algorithm %s falling back to COBYLA (pdfo incompatible with NumPy %s)",
                   algorithm, np.__version__)


def solve(problem, session, cancellation_token, limits=None):
    """Dispatch to the appropriate scipy solver based on problem.algorithm."""
    limits = limits or {}
    algorithm = problem.algorithm
    maxiter = int(limits.get("max_iterations", 1000))
    max_wall_time = float(limits.get("max_wall_time_seconds", 300.0))
    deadline = time.time() + max_wall_time if max_wall_time > 0 else None

    if algorithm == "linprog":
        if problem.options.get("problem_class") != "linear":
            raise SolverException(SOLVER_NONLINEAR_MODEL_REJECTED_BY_LP_BACKEND,
                                  "linprog requires problem_class='linear' declaration")
        return solve_linprog(problem, session, cancellation_token, deadline=deadline)

    if algorithm == "auto":
        if problem.options.get("problem_class") == "linear":
            algorithm = "linprog"
        else:
            algorithm = "cobyla"

    if algorithm in ("slsqp", "trust-constr", "nelder-mead", "powell"):
        has_ineq = any(c.constraint_type in ("upper", "lower", "range") for c in problem.constraints)
        has_eq = any(c.constraint_type == "equality" for c in problem.constraints)
        if algorithm in ("nelder-mead", "powell") and (has_ineq or has_eq):
            raise SolverException(SOLVER_CONSTRAINT_UNSUPPORTED, f"Algorithm {algorithm} does not support constraints")
        scipy_method = {"slsqp": "SLSQP", "trust-constr": "trust-constr", "nelder-mead": "Nelder-Mead", "powell": "Powell"}[algorithm]
        return solve_gradient_nlp(problem, session, cancellation_token,
                                   method=scipy_method, maxiter=maxiter,
                                   tol=float(limits.get("tol", 1e-6)), deadline=deadline)

    if algorithm not in ("cobyla", "bobyqa", "newuoa", "lincoa"):
        raise SolverException(SOLVER_BACKEND_FAILED, f"Unknown algorithm: {algorithm}")

    has_ineq = any(c.constraint_type in ("upper", "lower", "range") for c in problem.constraints)
    has_eq = any(c.constraint_type == "equality" for c in problem.constraints)
    if algorithm in ("bobyqa", "newuoa") and (has_ineq or has_eq):
        raise SolverException(SOLVER_CONSTRAINT_UNSUPPORTED, f"Algorithm {algorithm} does not support constraints")
    if algorithm == "lincoa" and has_eq:
        raise SolverException(SOLVER_CONSTRAINT_UNSUPPORTED, "Algorithm lincoa does not support equality constraints")

    if algorithm != "cobyla":
        _print_pdfo_fallback_notice(algorithm)

    return solve_cobyla(problem, session, cancellation_token, maxiter=maxiter,
                        rhobeg=float(limits.get("rhobeg", 1.0)),
                        tol=float(limits.get("tol", 1e-6)), deadline=deadline)
