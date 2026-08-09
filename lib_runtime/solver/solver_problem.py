"""Runtime-resolved solver problem: validation and fingerprinting.

Takes a raw problem spec (from the command layer) and resolves it into
a ``SolverProblem`` with canonical cell IDs, validated bounds, and
fingerprints.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any

from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_VARIABLE_BOUNDS_INVALID,
    SOLVER_VARIABLE_MISSING_INITIAL,
    SOLVER_CELL_ERROR,
)
from lib_runtime.solver.solver_types import (
    SolverProblem,
    SolverVariable,
    SolverObjective,
    SolverConstraint,
)


def validate_variable(var: SolverVariable) -> None:
    """Validate a single resolved variable."""
    if var.lower_bound is not None and var.upper_bound is not None:
        if var.lower_bound > var.upper_bound:
            raise SolverException(
                SOLVER_VARIABLE_BOUNDS_INVALID,
                f"Variable {var.display_ref}: lower_bound {var.lower_bound} "
                f"> upper_bound {var.upper_bound}",
            )
    if var.lower_bound is not None and var.initial_value < var.lower_bound:
        raise SolverException(
            SOLVER_VARIABLE_BOUNDS_INVALID,
            f"Variable {var.display_ref}: initial_value {var.initial_value} "
            f"< lower_bound {var.lower_bound}",
        )
    if var.upper_bound is not None and var.initial_value > var.upper_bound:
        raise SolverException(
            SOLVER_VARIABLE_BOUNDS_INVALID,
            f"Variable {var.display_ref}: initial_value {var.initial_value} "
            f"> upper_bound {var.upper_bound}",
        )


def validate_problem(problem: SolverProblem) -> None:
    """Validate a resolved solver problem."""
    if not problem.variables:
        raise SolverException(
            SOLVER_VARIABLE_MISSING_INITIAL,
            "Problem has no decision variables",
        )
    if not problem.objectives:
        raise SolverException(
            SOLVER_CELL_ERROR,
            "Problem has no objectives",
        )

    # Check for duplicate variable cells.
    seen_cells: set[tuple[str, tuple[str, ...]]] = set()
    for var in problem.variables:
        key = (var.cube_id, var.addr)
        if key in seen_cells:
            raise SolverException(
                SOLVER_VARIABLE_BOUNDS_INVALID,
                f"Duplicate variable cell: {var.display_ref}",
            )
        seen_cells.add(key)
        validate_variable(var)


def compute_model_fingerprint(problem: SolverProblem) -> str:
    """Compute a structural fingerprint of the solver problem.

    The fingerprint captures the variable/objective/constraint cell
    identities, bounds, and directions — enough to detect structural
    changes between solve and apply.
    """
    parts: list[str] = []

    for var in sorted(problem.variables, key=lambda v: (v.cube_id, v.addr)):
        parts.append(
            f"v:{var.cube_id}:{','.join(var.addr)}:"
            f"{var.lower_bound}:{var.upper_bound}"
        )

    for obj in sorted(problem.objectives, key=lambda o: (o.cube_id, o.addr)):
        parts.append(f"o:{obj.cube_id}:{','.join(obj.addr)}:{obj.direction}")

    for con in sorted(problem.constraints, key=lambda c: (c.cube_id, c.addr)):
        parts.append(
            f"c:{con.cube_id}:{','.join(con.addr)}:{con.constraint_type}:"
            f"{con.bound_value}:{con.lower_bound_value}:{con.upper_bound_value}"
        )

    fingerprint_str = "|".join(parts)
    return hashlib.sha256(fingerprint_str.encode()).hexdigest()[:16]


def compute_solve_fingerprint(
    problem: SolverProblem,
    base_revision: str,
    limits: dict[str, Any],
) -> str:
    """Compute a solve-specific fingerprint including revision and limits."""
    model_fp = compute_model_fingerprint(problem)
    limits_str = json.dumps(limits, sort_keys=True)
    solve_str = f"{model_fp}:{base_revision}:{limits_str}"
    return hashlib.sha256(solve_str.encode()).hexdigest()[:16]


class ResolvedSolverEvaluationSession:
    """Bundle returned by ``begin_solver_evaluation``.

    Contains the resolved problem, base revision, fingerprints, and the
    live-Engine evaluation session.
    """

    def __init__(
        self,
        *,
        session: Any,
        problem: SolverProblem,
        base_revision: str,
        model_fingerprint: str,
        solve_fingerprint: str,
        telemetry: dict[str, float] | None = None,
    ) -> None:
        self.session = session
        self.problem = problem
        self.base_revision = base_revision
        self.model_fingerprint = model_fingerprint
        self.solve_fingerprint = solve_fingerprint
        self.telemetry = telemetry or {}
