"""Narrow capability interfaces injected into SolverService.

These are runtime-internal protocol classes, not bus DTOs and not references
to the public Command Service or Query Service dispatchers.  The composition
root supplies Engine-facing adapters that implement these ports.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from lib_runtime.solver.cancellation_token import CancellationToken


@runtime_checkable
class SolverEvaluationPort(Protocol):
    """Internal evaluation capability for workspace reads and live-Engine
    temporary evaluation.

    After ``begin_solver_evaluation`` returns, candidate evaluation is
    performed by calling ``session.evaluate_candidate(...)`` directly on
    the returned session.  The port is not called again during the
    optimization loop.
    """

    def resolve_cell_reference(self, workspace_id: str, cell_ref: dict) -> dict:
        """Resolve a cell reference (cube_name, channel, selectors) to
        canonical IDs.

        Returns dict with ``workspace_id``, ``cube_id``, ``address_ids``,
        and ``display_ref``.
        """
        ...

    def begin_solver_evaluation(
        self,
        workspace_id: str,
        problem_spec: dict,
        expected_revision: str | None,
        limits: dict,
        cancellation_token: CancellationToken,
    ) -> Any:
        """Begin a solver evaluation session against the live Engine.

        Steps:
        1. Acquire exclusive workspace solver-evaluation lease.
        2. Verify expected_revision against engine.current_revision.
        3. Resolve all solver references to canonical cell IDs.
        4. Read and store original decision-variable values in a
           restoration table.
        5. Validate bounds and initial values.
        6. Validate uniqueness of decision-variable source cells.
        7. Compute model_fingerprint and solve_fingerprint.
        8. Return ResolvedSolverEvaluationSession bundle.

        Returns an object with ``session``, ``problem``, ``base_revision``,
        ``model_fingerprint``, and ``solve_fingerprint`` attributes.
        """
        ...


@runtime_checkable
class SolverCanonicalApplyPort(Protocol):
    """Internal capability for atomic canonical result application.

    Performs one Engine transaction with in-transaction revision
    revalidation, recalculation, and postcondition validation.
    """

    def commit_solver_result_values(
        self,
        context: "SolverApplyContext",
        variable_values: tuple,
        base_revision: str,
        postcheck_spec: dict,
    ) -> dict:
        """Atomic canonical apply.

        Opens an Engine transaction, revalidates revision inside the
        transaction lock, applies values, recalculates, validates
        postconditions, and commits.

        Returns dict with ``committed_revision``.

        Raises ``SolverException`` with ``SOLVER_RESULT_STALE`` if
        revision mismatch is detected inside the transaction.
        """
        ...


@runtime_checkable
class SolverEventPort(Protocol):
    """Narrow injected capability for publishing solver lifecycle and
    provenance events.

    Phase 1 choice: bounded in-memory enqueue with background drain.
    ``publish_event`` returns immediately after enqueue.  Queue overflow
    drops the event and logs a diagnostic.
    """

    def publish_event(self, event: dict) -> None:
        """Publish a solver event.

        The event dict includes ``event_id``, ``job_id``,
        ``workspace_id``, and event-specific payload.

        Returns immediately after bounded in-memory enqueue.
        """
        ...


# --- SolverApplyContext (used by SolverCanonicalApplyPort) -----------------

class SolverApplyContext:
    """Context for a canonical apply operation.

    Provides actor_id, session_id, correlation_id, command_id, job_id,
    apply_request_id, and workspace_id so the adapter can ensure correct
    actor attribution, command correlation, undo/history entry, solver
    provenance, and idempotency identity in the canonical transaction.
    """

    def __init__(
        self,
        *,
        actor_id: str,
        session_id: str,
        correlation_id: str,
        command_id: str,
        job_id: str,
        apply_request_id: str,
        workspace_id: str,
    ) -> None:
        self.actor_id = actor_id
        self.session_id = session_id
        self.correlation_id = correlation_id
        self.command_id = command_id
        self.job_id = job_id
        self.apply_request_id = apply_request_id
        self.workspace_id = workspace_id
