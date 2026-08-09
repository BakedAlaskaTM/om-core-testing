"""Cancellable solver job with limits, state transitions, and runtime policy.

A ``SolverJob`` is the in-memory record.  The ``SolverJobRunner`` owns
the worker thread lifecycle, evaluation session, and result production.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

from lib_runtime.solver.cancellation_token import CancellationToken
from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_RESTORATION_FAILED,
    SOLVER_LIMIT_EXCEEDED,
)
from lib_runtime.solver.solver_types import (
    SolverJob,
    JobState,
    SolverResult,
    TerminationStatus,
    SolverRuntimePolicy,
)
from lib_runtime.solver.solver_problem import ResolvedSolverEvaluationSession

logger = logging.getLogger(__name__)


class SolverJobRunner:
    """Runs a solver job on a background thread.

    Holds the cancellation token, worker thread, and result.  The
    ``SolverService`` creates one runner per job.
    """

    def __init__(
        self,
        job: SolverJob,
        resolved: ResolvedSolverEvaluationSession,
        policy: SolverRuntimePolicy,
        limits: dict[str, Any],
        backend_solve_fn: Any,
        on_finished: Any = None,
    ) -> None:
        self._job = job
        self._resolved = resolved
        self._policy = policy
        self._limits = limits
        self._backend_solve_fn = backend_solve_fn
        self._on_finished = on_finished
        self._cancellation_token = CancellationToken()
        self._thread: threading.Thread | None = None
        self._result: SolverResult | None = None
        self._error: Exception | None = None
        self._lock = threading.Lock()

    @property
    def job(self) -> SolverJob:
        return self._job

    @property
    def cancellation_token(self) -> CancellationToken:
        return self._cancellation_token

    @property
    def result(self) -> SolverResult | None:
        with self._lock:
            return self._result

    @property
    def error(self) -> Exception | None:
        with self._lock:
            return self._error

    def start(self) -> None:
        """Launch the worker thread."""
        self._thread = threading.Thread(
            target=self._run,
            name=f"solver-{self._job.job_id}",
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        """Request cancellation."""
        self._cancellation_token.cancel()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker thread to finish. Returns True if finished."""
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    def _run(self) -> None:
        """Worker thread entry point."""
        session = self._resolved.session
        stop_progress = threading.Event()

        def _sync_progress():
            while not stop_progress.wait(1.0):
                try:
                    self._job.n_evaluations = getattr(session, "live_eval_count", 0)
                except Exception:
                    pass

        progress_thread = threading.Thread(
            target=_sync_progress, name=f"solver-progress-{self._job.job_id}", daemon=True
        )
        progress_thread.start()

        try:
            result = self._backend_solve_fn(
                self._resolved.problem,
                session,
                self._cancellation_token,
                self._limits,
            )
            with self._lock:
                self._result = result
            self._job.termination_status = result.termination_status
            self._job.n_evaluations = result.n_evaluations
            self._job.backend_metadata = result.backend_metadata
            self._job.message = result.message
        except Exception as e:
            logger.exception("Solver job %s failed", self._job.job_id)
            with self._lock:
                self._error = e
            self._job.termination_status = TerminationStatus.FAILED
            self._job.message = str(e)
        finally:
            stop_progress.set()
            # Close the evaluation session (restores original values).
            try:
                session.close()
            except Exception as e:
                logger.exception(
                    "Restoration failed for job %s", self._job.job_id
                )
                self._job.termination_status = TerminationStatus.FAILED
                self._job.message = f"Restoration failed: {e}"

            self._job.state = JobState.FINISHED
            self._job.finished_at = time.time()

            # Notify the service that the job has finished.
            if self._on_finished is not None:
                try:
                    self._on_finished(self._job.job_id)
                except Exception:
                    logger.exception(
                        "on_finished callback failed for job %s",
                        self._job.job_id,
                    )
