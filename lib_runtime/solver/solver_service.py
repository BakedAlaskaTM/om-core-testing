"""SolverService: orchestration, job/result lifecycle.

Owns job management, worker thread lifecycle, result claiming and
receipt storage, bounded retention with tombstones, and admission
reservation.

Receives ``SolverEvaluationPort``, ``SolverCanonicalApplyPort``,
``SolverEventPort``, and ``SolverRuntimePolicy`` via constructor
injection.  Does not import Engine, MessageBus, or ClientSession.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any

from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_WORKSPACE_BUSY,
    SOLVER_JOB_NOT_FOUND,
    SOLVER_UNAUTHORIZED,
    SOLVER_RESULT_STALE,
    SOLVER_LIMIT_EXCEEDED,
    SOLVER_RESTORATION_FAILED,
    SOLVER_RESULT_NOT_APPLICABLE,
)
from lib_runtime.solver.solver_types import (
    SolverJob,
    JobState,
    TerminationStatus,
    ClaimedSolverResult,
    SolverApplyReceipt,
    ApplyState,
    SolverRuntimePolicy,
    SolverResult,
    SolverPoint,
)
from lib_runtime.solver.solver_job import SolverJobRunner
from lib_runtime.solver.solver_problem import ResolvedSolverEvaluationSession
from lib_runtime.solver.solver_ports import (
    SolverEvaluationPort,
    SolverCanonicalApplyPort,
    SolverEventPort,
    SolverApplyContext,
)
from lib_runtime.solver.backends.scipy.scipy_backend import solve as scipy_solve


def _resolve_backend_solve_fn(backend_id: str):
    """Return the solve function for the given backend_id."""
    if backend_id == "pymoo":
        from lib_runtime.solver.backends.pymoo.pymoo_backend import solve as pymoo_solve
        return pymoo_solve
    return scipy_solve

logger = logging.getLogger(__name__)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class SolverService:
    """Central solver orchestration service.

    Manages solver jobs, worker threads, results, and apply lifecycle.
    """

    def __init__(
        self,
        evaluation_port: SolverEvaluationPort,
        apply_port: SolverCanonicalApplyPort,
        event_port: SolverEventPort,
        policy: SolverRuntimePolicy | None = None,
    ) -> None:
        self._eval_port = evaluation_port
        self._apply_port = apply_port
        self._event_port = event_port
        self._policy = policy or SolverRuntimePolicy()

        self._jobs: dict[str, SolverJob] = {}
        self._runners: dict[str, SolverJobRunner] = {}
        self._results: dict[str, ClaimedSolverResult] = {}
        self._tombstones: set[str] = set()
        self._lock = threading.Lock()

    # --- Public API: run_solver -------------------------------------------

    def run_solver(
        self,
        *,
        workspace_id: str,
        actor_id: str,
        session_id: str,
        problem_spec: dict,
        expected_revision: str | None = None,
        limits: dict | None = None,
    ) -> dict:
        """Start a solver job.

        Returns dict with ``job_id`` and ``status``.
        """
        limits = limits or problem_spec.get("limits", {}) or {}

        # Validate limits against policy.
        self._validate_limits(limits)

        # Check concurrency limit.
        with self._lock:
            active_count = sum(
                1 for j in self._jobs.values()
                if j.state == JobState.RUNNING
            )
            if active_count >= self._policy.max_concurrent_jobs:
                raise SolverException(
                    SOLVER_WORKSPACE_BUSY,
                    f"Concurrency limit reached ({self._policy.max_concurrent_jobs})",
                )

        # Begin evaluation session (acquires workspace lease).
        from lib_runtime.solver.cancellation_token import CancellationToken
        cancellation_token = CancellationToken()

        resolved = self._eval_port.begin_solver_evaluation(
            workspace_id=workspace_id,
            problem_spec=problem_spec,
            expected_revision=expected_revision,
            limits=limits,
            cancellation_token=cancellation_token,
        )

        # Use resolved limits (cell refs resolved to numbers) for the runner.
        resolved_limits = resolved.problem.limits or limits

        # Create job record.
        job_id = _new_id("solver")
        job = SolverJob(
            job_id=job_id,
            workspace_id=workspace_id,
            actor_id=actor_id,
            session_id=session_id,
            created_at=time.time(),
        )
        # Copy telemetry from resolved session.
        job.admission_wait_ms = 0.0
        job.lease_acquisition_ms = resolved.telemetry.get("lease_acquisition_ms", 0.0)
        job.reference_resolution_ms = resolved.telemetry.get("reference_resolution_ms", 0.0)
        job.restoration_capture_ms = resolved.telemetry.get("restoration_capture_ms", 0.0)
        job.fingerprint_ms = resolved.telemetry.get("fingerprint_ms", 0.0)

        # Create runner — subprocess or in-process thread.
        if self._policy.use_subprocess:
            from lib_runtime.solver.subprocess_worker import SubprocessSolverRunner
            runner = SubprocessSolverRunner(
                job=job,
                resolved=resolved,
                policy=self._policy,
                limits=resolved_limits,
                on_finished=self._on_job_finished,
            )
        else:
            backend_id = problem_spec.get("backend", "scipy")
            solve_fn = _resolve_backend_solve_fn(backend_id)
            runner = SolverJobRunner(
                job=job,
                resolved=resolved,
                policy=self._policy,
                limits=resolved_limits,
                backend_solve_fn=solve_fn,
                on_finished=self._on_job_finished,
            )

        with self._lock:
            self._jobs[job_id] = job
            self._runners[job_id] = runner

        # Publish started event.
        self._event_port.publish_event({
            "event_id": "event.solver_job.started",
            "job_id": job_id,
            "workspace_id": workspace_id,
            "actor_id": actor_id,
        })

        # Start worker thread.
        t0 = time.time()
        runner.start()
        job.worker_launch_ms = (time.time() - t0) * 1000.0

        return {
            "job_id": job_id,
            "status": "running",
        }

    # --- Public API: cancel_solver ----------------------------------------

    def cancel_solver(
        self,
        *,
        job_id: str,
        actor_id: str,
        session_id: str,
    ) -> dict:
        """Request cancellation of a running solver job."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                if job_id in self._tombstones:
                    raise SolverException(
                        SOLVER_JOB_NOT_FOUND,
                        f"Job {job_id} not found (expired)",
                    )
                raise SolverException(
                    SOLVER_JOB_NOT_FOUND,
                    f"Job {job_id} not found",
                )

            # Authorization.
            if job.actor_id != actor_id:
                raise SolverException(
                    SOLVER_UNAUTHORIZED,
                    f"Actor {actor_id} is not the job owner",
                )

            if job.state == JobState.FINISHED:
                return {
                    "job_id": job_id,
                    "status": "finished",
                    "termination_status": job.termination_status.value if job.termination_status else "unknown",
                }

            runner = self._runners.get(job_id)
            if runner is None:
                return {
                    "job_id": job_id,
                    "status": job.state.value,
                }

        # Request cancellation.
        runner.cancel()

        # Publish cancellation requested event.
        self._event_port.publish_event({
            "event_id": "event.solver_job.cancellation_requested",
            "job_id": job_id,
            "workspace_id": job.workspace_id,
        })

        return {
            "job_id": job_id,
            "status": "cancelling",
        }

    # --- Public API: solver_status ----------------------------------------

    def solver_status(
        self,
        *,
        job_id: str,
        actor_id: str,
        session_id: str,
    ) -> dict:
        """Query solver job status."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                if job_id in self._tombstones:
                    raise SolverException(
                        SOLVER_JOB_NOT_FOUND,
                        f"Job {job_id} not found (expired)",
                    )
                raise SolverException(
                    SOLVER_JOB_NOT_FOUND,
                    f"Job {job_id} not found",
                )

            if job.actor_id != actor_id:
                raise SolverException(
                    SOLVER_UNAUTHORIZED,
                    f"Actor {actor_id} is not the job owner",
                )

            return {
                "job_id": job_id,
                "status": job.state.value,
                "termination_status": job.termination_status.value if job.termination_status else None,
                "message": job.message,
                "n_evaluations": job.n_evaluations,
                "created_at": job.created_at,
                "finished_at": job.finished_at,
                "telemetry": {
                    "admission_wait_ms": job.admission_wait_ms,
                    "lease_acquisition_ms": job.lease_acquisition_ms,
                    "reference_resolution_ms": job.reference_resolution_ms,
                    "restoration_capture_ms": job.restoration_capture_ms,
                    "fingerprint_ms": job.fingerprint_ms,
                    "worker_launch_ms": job.worker_launch_ms,
                },
            }

    # --- Public API: solver_result ----------------------------------------

    def solver_result(
        self,
        *,
        job_id: str,
        actor_id: str,
        session_id: str,
    ) -> dict:
        """Query solver result."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                if job_id in self._tombstones:
                    raise SolverException(
                        SOLVER_JOB_NOT_FOUND,
                        f"Job {job_id} not found (expired)",
                    )
                raise SolverException(
                    SOLVER_JOB_NOT_FOUND,
                    f"Job {job_id} not found",
                )

            if job.actor_id != actor_id:
                raise SolverException(
                    SOLVER_UNAUTHORIZED,
                    f"Actor {actor_id} is not the job owner",
                )

            if job.state != JobState.FINISHED:
                return {
                    "job_id": job_id,
                    "status": job.state.value,
                    "result": None,
                }

            result = self._results.get(job_id)
            if result is None:
                # Check if result was evicted (tombstone).
                if job_id in self._tombstones:
                    raise SolverException(
                        SOLVER_JOB_NOT_FOUND,
                        f"Job {job_id} result has expired or been evicted",
                    )
                # Job finished but no result (e.g. failure).
                return {
                    "job_id": job_id,
                    "status": "finished",
                    "termination_status": job.termination_status.value if job.termination_status else "failed",
                    "result": None,
                    "message": job.message,
                }

            # Check expiry.
            if result.expires_at is not None and time.time() > result.expires_at:
                self._results.pop(job_id, None)
                self._tombstones.add(job_id)
                raise SolverException(
                    SOLVER_JOB_NOT_FOUND,
                    f"Job {job_id} result has expired",
                )

            return {
                "job_id": job_id,
                "status": "finished",
                "termination_status": result.termination_status.value,
                "is_multi_objective": result.is_multi_objective,
                "solution": (
                    {
                        "variable_values": result.solution.variable_values,
                        "objective_values": result.solution.objective_values,
                        "constraint_values": result.solution.constraint_values,
                    }
                    if result.solution is not None
                    else None
                ),
                "pareto_front": (
                    [
                        {
                            "variable_values": p.variable_values,
                            "objective_values": p.objective_values,
                            "constraint_values": p.constraint_values,
                        }
                        for p in result.pareto_front
                    ]
                    if result.pareto_front is not None
                    else None
                ),
                "base_revision": result.base_revision,
                "model_fingerprint": result.model_fingerprint,
                "solve_fingerprint": result.solve_fingerprint,
                "applicable": result.applicable,
                "apply_state": result.apply_state.value,
            }

    # --- Public API: apply_solver_result ----------------------------------

    def apply_solver_result(
        self,
        *,
        job_id: str,
        actor_id: str,
        session_id: str,
        apply_request_id: str,
        pareto_index: int | None = None,
        allow_nonoptimal: bool = False,
    ) -> dict:
        """Apply solver result values to the workspace."""
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise SolverException(
                    SOLVER_JOB_NOT_FOUND,
                    f"Job {job_id} not found",
                )

            if job.actor_id != actor_id:
                raise SolverException(
                    SOLVER_UNAUTHORIZED,
                    f"Actor {actor_id} is not the job owner",
                )

            result = self._results.get(job_id)
            if result is None:
                raise SolverException(
                    SOLVER_JOB_NOT_FOUND,
                    f"Job {job_id} has no result",
                )

            # Check expiry.
            if result.expires_at is not None and time.time() > result.expires_at:
                self._results.pop(job_id, None)
                self._tombstones.add(job_id)
                raise SolverException(
                    SOLVER_JOB_NOT_FOUND,
                    f"Job {job_id} result has expired",
                )

            # Check idempotency — if already applied with same request ID.
            if result.apply_request_id == apply_request_id and result.apply_receipt is not None:
                return {
                    "job_id": job_id,
                    "apply_request_id": apply_request_id,
                    "committed_revision": result.apply_receipt.committed_revision,
                    "already_applied": True,
                }

            # Check applicability.
            if not result.applicable and not allow_nonoptimal:
                raise SolverException(
                    SOLVER_RESULT_NOT_APPLICABLE,
                    f"Result termination_status is {result.termination_status.value}, "
                    f"not applicable without allow_nonoptimal=True",
                )

            # For multi-objective results, require a pareto_index.
            if result.is_multi_objective:
                if pareto_index is None:
                    n_solutions = len(result.pareto_front)
                    raise SolverException(
                        SOLVER_RESULT_NOT_APPLICABLE,
                        f"Job {job_id} contains {n_solutions} Pareto solutions. "
                        f"Specify a solution index: solver apply {job_id} <index>",
                    )
                if pareto_index < 0 or pareto_index >= len(result.pareto_front):
                    raise SolverException(
                        SOLVER_RESULT_NOT_APPLICABLE,
                        f"Pareto index {pareto_index} out of range "
                        f"(0..{len(result.pareto_front) - 1})",
                    )
                selected_point = result.pareto_front[pareto_index]
            else:
                if pareto_index is not None:
                    raise SolverException(
                        SOLVER_RESULT_NOT_APPLICABLE,
                        f"Job {job_id} is single-objective; "
                        f"pareto_index is not applicable",
                    )
                selected_point = result.solution

            if selected_point is None:
                raise SolverException(
                    SOLVER_RESULT_NOT_APPLICABLE,
                    f"Job {job_id} has no solution to apply",
                )

            # Transition to applying.
            result.apply_state = ApplyState.APPLYING
            result.apply_request_id = apply_request_id

        # Build apply context.
        context = SolverApplyContext(
            actor_id=actor_id,
            session_id=session_id,
            correlation_id=apply_request_id,
            command_id="apply_solver_result",
            job_id=job_id,
            apply_request_id=apply_request_id,
            workspace_id=job.workspace_id,
        )

        # Build postcheck spec with variable cells.
        runner = self._runners.get(job_id)
        if runner is None:
            raise SolverException(
                SOLVER_JOB_NOT_FOUND,
                f"Job {job_id} runner not found",
            )

        variable_cells = [
            (var.cube_id, var.addr)
            for var in runner._resolved.problem.variables
        ]

        postcheck_spec = {
            "variable_cells": variable_cells,
        }

        # Execute canonical apply.
        try:
            apply_result = self._apply_port.commit_solver_result_values(
                context=context,
                variable_values=tuple(selected_point.variable_values),
                base_revision=result.base_revision,
                postcheck_spec=postcheck_spec,
            )
        except Exception:
            # Roll back apply state.
            with self._lock:
                result.apply_state = ApplyState.UNAPPLIED
                result.apply_request_id = None
            raise

        # Store receipt.
        receipt = SolverApplyReceipt(
            apply_request_id=apply_request_id,
            job_id=job_id,
            committed_revision=apply_result["committed_revision"],
            applied_at=time.time(),
            variable_values=list(selected_point.variable_values),
        )

        with self._lock:
            result.apply_state = ApplyState.APPLIED
            result.apply_receipt = receipt

        # Publish applied event.
        self._event_port.publish_event({
            "event_id": "event.solver_result.applied",
            "job_id": job_id,
            "workspace_id": job.workspace_id,
            "apply_request_id": apply_request_id,
            "committed_revision": receipt.committed_revision,
        })

        return {
            "job_id": job_id,
            "apply_request_id": apply_request_id,
            "committed_revision": receipt.committed_revision,
            "already_applied": False,
        }

    # --- Public API: list backends / algorithms ---------------------------

    def list_jobs(self) -> list[dict]:
        """List all known solver jobs with their current status."""
        import time as _time
        with self._lock:
            out = []
            for job_id, job in self._jobs.items():
                out.append({
                    "job_id": job_id,
                    "status": job.state.value if hasattr(job.state, 'value') else str(job.state),
                    "termination_status": job.termination_status.value if job.termination_status and hasattr(job.termination_status, 'value') else (str(job.termination_status) if job.termination_status else None),
                    "created_at": job.created_at,
                    "finished_at": job.finished_at,
                    "n_evaluations": job.n_evaluations,
                    "message": job.message,
                    "has_result": job_id in self._results,
                })
            # Include tombstoned (evicted) jobs
            for job_id in self._tombstones:
                if job_id not in self._jobs:
                    out.append({
                        "job_id": job_id,
                        "status": "evicted",
                        "termination_status": None,
                        "created_at": 0.0,
                        "finished_at": None,
                        "n_evaluations": 0,
                        "message": "Result evicted",
                        "has_result": False,
                    })
            return out

    def list_backends(self) -> list[dict]:
        from lib_runtime.solver.backend_registry import list_backends
        return [
            {
                "backend_id": b.backend_id,
                "algorithms": b.algorithms,
                "supports_bounds": b.supports_bounds,
                "supports_inequality_constraints": b.supports_inequality_constraints,
                "supports_equality_constraints": b.supports_equality_constraints,
                "requires_derivatives": b.requires_derivatives,
                "cancellation": b.cancellation.value,
            }
            for b in list_backends()
        ]

    def list_algorithms(self, backend_id: str) -> list[dict]:
        from lib_runtime.solver.backend_registry import list_algorithms
        return [
            {
                "algorithm_id": a.algorithm_id,
                "backend_id": a.backend_id,
                "supports_bounds": a.supports_bounds,
                "supports_inequality_constraints": a.supports_inequality_constraints,
                "supports_equality_constraints": a.supports_equality_constraints,
                "requires_derivatives": a.requires_derivatives,
                "cancellation": a.cancellation.value,
                "linear": a.linear,
            }
            for a in list_algorithms(backend_id)
        ]

    # --- Public API: export diagnostic report -----------------------------

    def export_job(self, job_id, *, actor_id, session_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise SolverException(SOLVER_JOB_NOT_FOUND, f"Job {job_id} not found")
            result = self._results.get(job_id)
        report = {
            "schema_version": "1.0",
            "exported_at": time.time(),
            "job": {
                "job_id": job.job_id, "workspace_id": job.workspace_id,
                "actor_id": job.actor_id, "session_id": job.session_id,
                "state": job.state.value,
                "termination_status": job.termination_status.value if job.termination_status else None,
                "created_at": job.created_at, "finished_at": job.finished_at,
                "message": job.message, "n_evaluations": job.n_evaluations,
                "backend_metadata": job.backend_metadata,
                "telemetry": {
                    "admission_wait_ms": job.admission_wait_ms,
                    "lease_acquisition_ms": job.lease_acquisition_ms,
                    "reference_resolution_ms": job.reference_resolution_ms,
                    "restoration_capture_ms": job.restoration_capture_ms,
                    "fingerprint_ms": job.fingerprint_ms,
                    "worker_launch_ms": job.worker_launch_ms,
                },
            },
        }
        if result is not None:
            report["result"] = {
                "termination_status": result.termination_status.value,
                "is_multi_objective": result.is_multi_objective,
                "solution": (
                    {
                        "variable_values": result.solution.variable_values,
                        "objective_values": result.solution.objective_values,
                    }
                    if result.solution is not None
                    else None
                ),
                "pareto_front_size": len(result.pareto_front) if result.pareto_front else 0,
                "base_revision": result.base_revision,
                "model_fingerprint": result.model_fingerprint,
                "solve_fingerprint": result.solve_fingerprint,
                "created_at": result.created_at,
                "expires_at": result.expires_at,
                "apply_state": result.apply_state.value if result.apply_state else None,
            }
        return report

    # --- Internal: worker completion hook ---------------------------------

    def _on_job_finished(self, job_id: str) -> None:
        """Called when a job's worker thread finishes.

        Stores the result and releases the workspace lease.
        """
        with self._lock:
            runner = self._runners.get(job_id)
            if runner is None:
                return

            job = self._jobs.get(job_id)
            if job is None:
                return

            # Store result if we have one.
            if runner.result is not None:
                result = runner.result
                claimed = ClaimedSolverResult(
                    job_id=job_id,
                    workspace_id=job.workspace_id,
                    actor_id=job.actor_id,
                    termination_status=result.termination_status,
                    solution=result.solution,
                    pareto_front=result.pareto_front,
                    base_revision=runner._resolved.base_revision,
                    model_fingerprint=runner._resolved.model_fingerprint,
                    solve_fingerprint=runner._resolved.solve_fingerprint,
                    created_at=time.time(),
                    expires_at=time.time() + self._policy.result_ttl_seconds,
                )
                self._results[job_id] = claimed

            # Release workspace lease.
            if hasattr(self._eval_port, "release_lease"):
                self._eval_port.release_lease(job.workspace_id)

        # Publish finished event.
        self._event_port.publish_event({
            "event_id": "event.solver_job.finished",
            "job_id": job_id,
            "workspace_id": job.workspace_id,
            "termination_status": job.termination_status.value if job.termination_status else "failed",
        })

    # --- Internal: limit validation --------------------------------------

    def _validate_limits(self, limits: dict) -> None:
        """Validate client-supplied limits against runtime policy.

        Skips values that are cell-ref dicts (resolved later by the evaluation adapter).
        """
        max_eval = limits.get("max_iterations")
        if max_eval is not None and not isinstance(max_eval, dict) and int(max_eval) > self._policy.max_evaluations:
            raise SolverException(
                SOLVER_LIMIT_EXCEEDED,
                f"max_iterations {max_eval} exceeds policy limit "
                f"{self._policy.max_evaluations}",
                detail={"allowed_maximum": self._policy.max_evaluations},
            )

        max_wall = limits.get("max_wall_time_seconds")
        if max_wall is not None and not isinstance(max_wall, dict) and float(max_wall) > self._policy.max_wall_time_seconds:
            raise SolverException(
                SOLVER_LIMIT_EXCEEDED,
                f"max_wall_time_seconds {max_wall} exceeds policy limit "
                f"{self._policy.max_wall_time_seconds}",
                detail={"allowed_maximum": self._policy.max_wall_time_seconds},
            )

    # --- Internal: cleanup ------------------------------------------------

    def cleanup_expired(self) -> int:
        """Remove expired results. Returns count of removed results."""
        now = time.time()
        removed = 0
        with self._lock:
            to_remove = []
            for job_id, result in self._results.items():
                if result.expires_at is not None and now > result.expires_at:
                    to_remove.append(job_id)

            for job_id in to_remove:
                self._results.pop(job_id, None)
                self._tombstones.add(job_id)
                removed += 1

            # LRU eviction.
            if len(self._results) > self._policy.max_retained_results:
                sorted_results = sorted(
                    self._results.items(),
                    key=lambda x: x[1].created_at,
                )
                while len(self._results) > self._policy.max_retained_results:
                    job_id, _ = sorted_results.pop(0)
                    self._results.pop(job_id, None)
                    self._tombstones.add(job_id)
                    removed += 1

        return removed
