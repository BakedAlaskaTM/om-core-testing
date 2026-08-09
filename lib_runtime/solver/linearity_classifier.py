"""Diagnostic linearity classifier.

Performs an N+1 point Jacobian consistency check to determine whether
the objective and constraint functions are affine (linear + constant)
over the variable space.  This is a **diagnostic only** tool — it does
not authorize LP dispatch.  LP dispatch requires explicit
``problem_class = "linear"`` declaration from the caller (Safe Policy A)
or a structural certificate from the Engine (Safe Policy B, future).

The classifier evaluates the model at a base point and N perturbed
points (one per variable).  If the Jacobian is consistent (all rows
are constant within tolerance) and the function values match the
affine prediction at additional validation points, the model is
classified as linear.

Limitations:
- Numerical probing can miss piecewise branches.
- Results are sensitive to the perturbation step size.
- The classifier is not a substitute for a structural certificate.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from lib_runtime.solver.solver_evaluation_session import SolverEvaluationSession
from lib_runtime.solver.solver_types import SolverProblem

logger = logging.getLogger(__name__)


@dataclass
class LinearityResult:
    """Result of a linearity classification."""

    is_linear: bool
    method: str  # "jacobian_consistency"
    n_points_evaluated: int = 0
    jacobian: np.ndarray | None = None  # shape (n_outputs, n_vars)
    intercepts: np.ndarray | None = None  # shape (n_outputs,)
    max_jacobian_deviation: float = 0.0
    max_prediction_error: float = 0.0
    perturbation_step: float = 1e-4
    tolerance: float = 1e-6
    details: dict[str, Any] = field(default_factory=dict)


def classify_linearity(
    problem: SolverProblem,
    session: SolverEvaluationSession,
    *,
    perturbation_step: float = 1e-4,
    tolerance: float = 1e-6,
    n_validation_points: int = 3,
) -> LinearityResult:
    """Classify whether the model is affine over the variable space.

    Evaluates at:
    - 1 base point (x0)
    - N perturbed points (x0 + step * e_i for each variable i)
    - n_validation_points random points

    Total evaluations: N + 1 + n_validation_points.

    Args:
        problem: Resolved solver problem.
        session: Live-Engine evaluation session.
        perturbation_step: Step size for Jacobian estimation.
        tolerance: Relative tolerance for linearity check.
        n_validation_points: Number of random validation points.

    Returns:
        LinearityResult with classification and diagnostics.
    """
    n_vars = problem.n_variables
    if n_vars == 0:
        return LinearityResult(
            is_linear=True,
            method="jacobian_consistency",
            n_points_evaluated=0,
            details={"reason": "no variables"},
        )

    # Determine the number of outputs (objectives + constraints).
    n_obj = problem.n_objectives
    n_con = problem.n_constraints
    n_outputs = n_obj + n_con

    if n_outputs == 0:
        return LinearityResult(
            is_linear=True,
            method="jacobian_consistency",
            n_points_evaluated=0,
            details={"reason": "no outputs"},
        )

    # Base point: use initial values.
    x0 = np.array([v.initial_value for v in problem.variables], dtype=float)

    # Evaluate at base point.
    base_result = session.evaluate_candidate(list(x0))
    base_outputs = np.array(base_result["objectives"] + base_result["constraints"], dtype=float)

    # Evaluate at perturbed points to estimate Jacobian.
    jacobian = np.zeros((n_outputs, n_vars), dtype=float)
    n_evals = 1

    for i in range(n_vars):
        x_pert = x0.copy()
        x_pert[i] += perturbation_step
        pert_result = session.evaluate_candidate(list(x_pert))
        pert_outputs = np.array(pert_result["objectives"] + pert_result["constraints"], dtype=float)
        jacobian[:, i] = (pert_outputs - base_outputs) / perturbation_step
        n_evals += 1

    # Validate at additional random points.
    rng = np.random.default_rng(seed=42)
    max_prediction_error = 0.0

    for _ in range(n_validation_points):
        # Random point within bounds (or ±1 if no bounds).
        x_val = np.array([
            rng.uniform(v.lower_bound or -1.0, v.upper_bound or 1.0)
            for v in problem.variables
        ], dtype=float)

        val_result = session.evaluate_candidate(list(x_val))
        val_outputs = np.array(val_result["objectives"] + val_result["constraints"], dtype=float)

        # Predict using affine model: f(x) ≈ f(x0) + J * (x - x0)
        predicted = base_outputs + jacobian @ (x_val - x0)

        # Compute relative prediction error.
        errors = np.abs(val_outputs - predicted)
        denom = np.maximum(np.abs(val_outputs), 1.0)
        rel_errors = errors / denom
        max_err = float(np.max(rel_errors))
        max_prediction_error = max(max_prediction_error, max_err)
        n_evals += 1

    # Check Jacobian consistency by re-estimating at a different step.
    jacobian_2 = np.zeros((n_outputs, n_vars), dtype=float)
    step2 = perturbation_step * 2.0
    for i in range(n_vars):
        x_pert = x0.copy()
        x_pert[i] += step2
        pert_result = session.evaluate_candidate(list(x_pert))
        pert_outputs = np.array(pert_result["objectives"] + pert_result["constraints"], dtype=float)
        jacobian_2[:, i] = (pert_outputs - base_outputs) / step2
        n_evals += 1

    # Jacobian deviation between two step sizes.
    jac_diff = np.abs(jacobian - jacobian_2)
    jac_denom = np.maximum(np.abs(jacobian), 1.0)
    rel_jac_diff = jac_diff / jac_denom
    max_jac_dev = float(np.max(rel_jac_diff))

    # Classify: linear if both Jacobian is consistent and predictions match.
    is_linear = max_jac_dev < tolerance and max_prediction_error < tolerance

    return LinearityResult(
        is_linear=is_linear,
        method="jacobian_consistency",
        n_points_evaluated=n_evals,
        jacobian=jacobian,
        intercepts=base_outputs - jacobian @ x0,
        max_jacobian_deviation=max_jac_dev,
        max_prediction_error=max_prediction_error,
        perturbation_step=perturbation_step,
        tolerance=tolerance,
        details={
            "n_variables": n_vars,
            "n_outputs": n_outputs,
            "n_objectives": n_obj,
            "n_constraints": n_con,
            "n_validation_points": n_validation_points,
        },
    )
