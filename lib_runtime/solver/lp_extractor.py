"""LP coefficient extraction via numerical probing.

Constructs an affine representation (``c``, ``A_ub``, ``b_ub``,
``A_eq``, ``b_eq``, bounds) from the model by evaluating at a base
point and N perturbed points.  This is an **inferred** representation,
not a structural one.

The extraction is only performed when the caller explicitly declares
``problem_class = "linear"``.  The extracted matrix is validated
against live Engine evaluations at additional sample points.

If extraction fails, ``SOLVER_LP_EXTRACTION_FAILED`` is raised.
If the extracted matrix is inconsistent with live Engine evaluations,
``SOLVER_LP_REPRESENTATION_INCONSISTENT`` is raised.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_LP_EXTRACTION_FAILED,
    SOLVER_LP_REPRESENTATION_INCONSISTENT,
)
from lib_runtime.solver.solver_evaluation_session import SolverEvaluationSession
from lib_runtime.solver.solver_types import SolverProblem, SolverConstraint

logger = logging.getLogger(__name__)

_LP_MATRIX_VERSION = "1.0"


@dataclass
class LPMatrix:
    """Versioned LP matrix representation.

    Standard form: min c^T x  s.t.  A_ub x <= b_ub,  A_eq x = b_eq,
    lb <= x <= ub.
    """

    c: np.ndarray  # Objective coefficients (n_vars,)
    A_ub: np.ndarray | None  # Inequality constraint matrix (n_ineq, n_vars)
    b_ub: np.ndarray | None  # Inequality RHS (n_ineq,)
    A_eq: np.ndarray | None  # Equality constraint matrix (n_eq, n_vars)
    b_eq: np.ndarray | None  # Equality RHS (n_eq,)
    lb: np.ndarray  # Lower bounds (n_vars,)
    ub: np.ndarray  # Upper bounds (n_vars,)
    version: str = _LP_MATRIX_VERSION
    source: str = "numerical_probe"
    n_evaluations: int = 0
    max_validation_error: float = 0.0
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def n_vars(self) -> int:
        return len(self.c)

    @property
    def n_ineq(self) -> int:
        return 0 if self.A_ub is None else self.A_ub.shape[0]

    @property
    def n_eq(self) -> int:
        return 0 if self.A_eq is None else self.A_eq.shape[0]


def extract_lp_matrix(
    problem: SolverProblem,
    session: SolverEvaluationSession,
    *,
    perturbation_step: float = 1e-4,
    tolerance: float = 1e-6,
    n_validation_points: int = 5,
) -> LPMatrix:
    """Extract LP matrix representation via numerical probing.

    Args:
        problem: Resolved solver problem with ``problem_class = "linear"``.
        session: Live-Engine evaluation session.
        perturbation_step: Step size for gradient estimation.
        tolerance: Tolerance for validation check.
        n_validation_points: Number of random validation points.

    Returns:
        LPMatrix with extracted coefficients.

    Raises:
        SolverException: If extraction fails or matrix is inconsistent.
    """
    n_vars = problem.n_variables
    n_obj = problem.n_objectives
    n_con = problem.n_constraints

    if n_vars == 0:
        raise SolverException(
            SOLVER_LP_EXTRACTION_FAILED,
            "Cannot extract LP matrix: 0 variables",
        )

    if n_obj != 1:
        raise SolverException(
            SOLVER_LP_EXTRACTION_FAILED,
            f"LP requires exactly 1 objective, got {n_obj}",
        )

    # Base point.
    x0 = np.array([v.initial_value for v in problem.variables], dtype=float)

    # Evaluate at base point.
    try:
        base_result = session.evaluate_candidate(list(x0))
    except Exception as e:
        raise SolverException(
            SOLVER_LP_EXTRACTION_FAILED,
            f"Base point evaluation failed: {e}",
        ) from e

    base_obj = float(base_result["objectives"][0])
    base_con = np.array(base_result["constraints"], dtype=float) if n_con > 0 else np.array([], dtype=float)
    n_evals = 1

    # Estimate objective gradient (c vector).
    c = np.zeros(n_vars, dtype=float)
    for i in range(n_vars):
        x_pert = x0.copy()
        x_pert[i] += perturbation_step
        try:
            pert_result = session.evaluate_candidate(list(x_pert))
        except Exception as e:
            raise SolverException(
                SOLVER_LP_EXTRACTION_FAILED,
                f"Objective gradient estimation failed at variable {i}: {e}",
            ) from e
        pert_obj = float(pert_result["objectives"][0])
        c[i] = (pert_obj - base_obj) / perturbation_step
        n_evals += 1

    # Objective intercept: f(x) ≈ c^T x + intercept
    obj_intercept = base_obj - c @ x0

    # Estimate constraint Jacobian.
    con_jacobian = np.zeros((n_con, n_vars), dtype=float)
    con_intercepts = np.zeros(n_con, dtype=float)

    if n_con > 0:
        for i in range(n_vars):
            x_pert = x0.copy()
            x_pert[i] += perturbation_step
            # Reuse the perturbed evaluation from above if available.
            # We need constraint values at the perturbed point.
            try:
                pert_result = session.evaluate_candidate(list(x_pert))
            except Exception as e:
                raise SolverException(
                    SOLVER_LP_EXTRACTION_FAILED,
                    f"Constraint gradient estimation failed at variable {i}: {e}",
                ) from e
            pert_con = np.array(pert_result["constraints"], dtype=float)
            con_jacobian[:, i] = (pert_con - base_con) / perturbation_step
            # Note: we already counted this eval for the objective gradient,
            # but we're re-evaluating here. To avoid double-counting, we
            # don't increment n_evals again.

        con_intercepts = base_con - con_jacobian @ x0

    # Build bounds.
    lb = np.array([
        v.lower_bound if v.lower_bound is not None else -np.inf
        for v in problem.variables
    ], dtype=float)
    ub = np.array([
        v.upper_bound if v.upper_bound is not None else np.inf
        for v in problem.variables
    ], dtype=float)

    # Build constraint matrices from SolverConstraint types.
    # COBYLA convention: constraint_type is "lower", "upper", "range", "equality".
    #   lower:  con_value >= bound_value  =>  -con_value <= -bound_value
    #   upper:  con_value <= bound_value  =>   con_value <=  bound_value
    #   equality: con_value == bound_value
    #
    # Since con_value ≈ con_jacobian @ x + con_intercept, we have:
    #   upper:  con_jacobian @ x + con_intercept <= bound_value
    #           => con_jacobian @ x <= bound_value - con_intercept
    #   lower:  con_jacobian @ x + con_intercept >= bound_value
    #           => -con_jacobian @ x <= con_intercept - bound_value
    #   equality: con_jacobian @ x = bound_value - con_intercept

    A_ub_rows: list[np.ndarray] = []
    b_ub_vals: list[float] = []
    A_eq_rows: list[np.ndarray] = []
    b_eq_vals: list[float] = []

    for idx, con in enumerate(problem.constraints):
        row = con_jacobian[idx, :]
        intercept = con_intercepts[idx]

        if con.constraint_type == "upper":
            # con_value <= bound_value
            bv = con.bound_value if con.bound_value is not None else con.upper_bound_value
            if bv is not None:
                A_ub_rows.append(row)
                b_ub_vals.append(bv - intercept)

        elif con.constraint_type == "lower":
            # con_value >= bound_value  =>  -con_value <= -bound_value
            bv = con.bound_value if con.bound_value is not None else con.lower_bound_value
            if bv is not None:
                A_ub_rows.append(-row)
                b_ub_vals.append(intercept - bv)

        elif con.constraint_type == "range":
            # lower_bound <= con_value <= upper_bound
            lbv = con.lower_bound_value
            ubv = con.upper_bound_value
            if ubv is not None:
                A_ub_rows.append(row)
                b_ub_vals.append(ubv - intercept)
            if lbv is not None:
                A_ub_rows.append(-row)
                b_ub_vals.append(intercept - lbv)

        elif con.constraint_type == "equality":
            bv = con.bound_value
            if bv is not None:
                A_eq_rows.append(row)
                b_eq_vals.append(bv - intercept)

    A_ub = np.array(A_ub_rows, dtype=float) if A_ub_rows else None
    b_ub = np.array(b_ub_vals, dtype=float) if b_ub_vals else None
    A_eq = np.array(A_eq_rows, dtype=float) if A_eq_rows else None
    b_eq = np.array(b_eq_vals, dtype=float) if b_eq_vals else None

    matrix = LPMatrix(
        c=c,
        A_ub=A_ub,
        b_ub=b_ub,
        A_eq=A_eq,
        b_eq=b_eq,
        lb=lb,
        ub=ub,
        n_evaluations=n_evals,
        metadata={
            "obj_intercept": float(obj_intercept),
            "con_intercepts": con_intercepts.tolist() if n_con > 0 else [],
            "perturbation_step": perturbation_step,
        },
    )

    # Validate against live Engine evaluations at random points.
    rng = np.random.default_rng(seed=123)
    max_err = 0.0

    for _ in range(n_validation_points):
        x_val = np.array([
            rng.uniform(
                v.lower_bound if v.lower_bound is not None else -1.0,
                v.upper_bound if v.upper_bound is not None else 1.0,
            )
            for v in problem.variables
        ], dtype=float)

        try:
            val_result = session.evaluate_candidate(list(x_val))
        except Exception as e:
            raise SolverException(
                SOLVER_LP_REPRESENTATION_INCONSISTENT,
                f"Validation evaluation failed: {e}",
            ) from e

        val_obj = float(val_result["objectives"][0])
        predicted_obj = c @ x_val + obj_intercept
        obj_err = abs(val_obj - predicted_obj) / max(abs(val_obj), 1.0)
        max_err = max(max_err, obj_err)

        if n_con > 0:
            val_con = np.array(val_result["constraints"], dtype=float)
            predicted_con = con_jacobian @ x_val + con_intercepts
            con_errs = np.abs(val_con - predicted_con) / np.maximum(np.abs(val_con), 1.0)
            max_err = max(max_err, float(np.max(con_errs)))

        n_evals += 1

    matrix.n_evaluations = n_evals
    matrix.max_validation_error = max_err

    if max_err > tolerance:
        raise SolverException(
            SOLVER_LP_REPRESENTATION_INCONSISTENT,
            f"LP matrix validation failed: max error {max_err:.2e} > tolerance {tolerance:.2e}",
            detail={"max_error": max_err, "tolerance": tolerance},
        )

    logger.info(
        "LP matrix extracted: %d vars, %d ineq, %d eq, max_validation_error=%.2e",
        n_vars, matrix.n_ineq, matrix.n_eq, max_err,
    )

    return matrix
