"""Runtime-internal solver types: capabilities, enums, and dataclasses.

These types are internal to the runtime solver bundle.  They are not part
of the public command/query/event contract.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class CancellationCapability(Enum):
    """What cancellation mechanism the backend supports."""

    NONE = "none"
    COOPERATIVE_CALLBACK = "cooperative_callback"


class TerminationStatus(Enum):
    """How a solver job ended."""

    OPTIMAL = "optimal"
    FEASIBLE = "feasible"
    INFEASIBLE = "infeasible"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    FAILED = "failed"
    LIMIT_EXCEEDED = "limit_exceeded"


@dataclass(frozen=True)
class BackendCapability:
    """Static capability descriptor for a solver backend."""

    backend_id: str
    algorithms: list[str]
    supports_bounds: bool
    supports_inequality_constraints: bool
    supports_equality_constraints: bool
    requires_derivatives: bool
    cancellation: CancellationCapability


@dataclass
class SolverVariable:
    """A resolved decision variable."""

    cube_id: str
    addr: tuple[str, ...]
    initial_value: float
    lower_bound: float | None = None
    upper_bound: float | None = None
    display_ref: str = ""

    @property
    def has_bounds(self) -> bool:
        return self.lower_bound is not None or self.upper_bound is not None


@dataclass
class SolverObjective:
    """A resolved objective cell."""

    cube_id: str
    addr: tuple[str, ...]
    direction: str  # "minimize" or "maximize"
    display_ref: str = ""


@dataclass
class SolverConstraint:
    """A resolved constraint cell."""

    cube_id: str
    addr: tuple[str, ...]
    constraint_type: str  # "lower", "upper", "range", "equality"
    bound_value: float | None = None
    lower_bound_value: float | None = None
    upper_bound_value: float | None = None
    display_ref: str = ""


@dataclass
class SolverProblem:
    """Fully resolved solver problem ready for backend dispatch."""

    variables: list[SolverVariable]
    objectives: list[SolverObjective]
    constraints: list[SolverConstraint]
    backend_id: str
    algorithm: str
    limits: dict[str, Any] = field(default_factory=dict)
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def n_variables(self) -> int:
        return len(self.variables)

    @property
    def n_objectives(self) -> int:
        return len(self.objectives)

    @property
    def n_constraints(self) -> int:
        return len(self.constraints)


@dataclass
class SolverPoint:
    """A single solution point in objective space.

    Used for both single-objective results (``SolverResult.solution``)
    and multi-objective Pareto fronts (``SolverResult.pareto_front``).
    """

    variable_values: list[float]
    objective_values: list[float]
    constraint_values: list[float] | None = None


@dataclass
class SolverResult:
    """Result returned by a backend after optimization completes.

    For single-objective: ``solution`` is set, ``pareto_front`` is None.
    For multi-objective:  ``pareto_front`` is set, ``solution`` is None.
    """

    termination_status: TerminationStatus
    n_evaluations: int = 0
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    solution: SolverPoint | None = None
    pareto_front: list[SolverPoint] | None = None


# --- Algorithm capability -------------------------------------------------

@dataclass(frozen=True)
class AlgorithmCapability:
    """Static capability descriptor for a specific algorithm within a backend."""

    algorithm_id: str
    backend_id: str
    supports_bounds: bool
    supports_inequality_constraints: bool
    supports_equality_constraints: bool
    requires_derivatives: bool
    cancellation: CancellationCapability
    linear: bool = False


# --- Job state and lifecycle -----------------------------------------------

class JobState(Enum):
    """Lifecycle states of a solver job."""

    RUNNING = "running"
    CANCELLING = "cancelling"
    FINISHED = "finished"


class ApplyState(Enum):
    """Apply lifecycle for a solver result."""

    UNAPPLIED = "unapplied"
    APPLYING = "applying"
    APPLIED = "applied"


@dataclass
class SolverJob:
    """In-memory solver job record."""

    job_id: str
    workspace_id: str
    actor_id: str
    session_id: str
    state: JobState = JobState.RUNNING
    termination_status: TerminationStatus | None = None
    created_at: float = 0.0
    finished_at: float | None = None
    message: str = ""
    n_evaluations: int = 0
    backend_metadata: dict[str, Any] = field(default_factory=dict)
    # Telemetry
    admission_wait_ms: float = 0.0
    lease_acquisition_ms: float = 0.0
    reference_resolution_ms: float = 0.0
    restoration_capture_ms: float = 0.0
    fingerprint_ms: float = 0.0
    worker_launch_ms: float = 0.0


@dataclass
class ClaimedSolverResult:
    """A solver result that has been claimed for apply."""

    job_id: str
    workspace_id: str
    actor_id: str
    termination_status: TerminationStatus
    base_revision: str
    model_fingerprint: str
    solve_fingerprint: str
    solution: SolverPoint | None = None
    pareto_front: list[SolverPoint] | None = None
    apply_state: ApplyState = ApplyState.UNAPPLIED
    apply_request_id: str | None = None
    apply_receipt: "SolverApplyReceipt | None" = None
    created_at: float = 0.0
    expires_at: float | None = None

    @property
    def applicable(self) -> bool:
        """Whether this result can be applied."""
        return self.termination_status in (
            TerminationStatus.OPTIMAL,
            TerminationStatus.FEASIBLE,
        )

    @property
    def is_multi_objective(self) -> bool:
        """True if this result contains a Pareto front."""
        return self.pareto_front is not None and len(self.pareto_front) > 0


@dataclass
class SolverApplyReceipt:
    """Receipt stored after a successful apply."""

    apply_request_id: str
    job_id: str
    committed_revision: str
    applied_at: float
    variable_values: list[float] = field(default_factory=list)


# --- Runtime policy --------------------------------------------------------

@dataclass(frozen=True)
class SolverRuntimePolicy:
    """Static policy limits enforced by SolverService."""

    max_concurrent_jobs: int = 1
    max_evaluations: int = 10000
    max_wall_time_seconds: float = 300.0
    result_ttl_seconds: float = 600.0
    max_retained_results: int = 50
    allow_nonoptimal_apply: bool = False
    use_subprocess: bool = False
