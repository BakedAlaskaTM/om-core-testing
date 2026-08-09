"""Subprocess-isolated solver worker for crash containment and hard cancellation.

The subprocess runs the SciPy optimizer. Evaluation requests are sent to the
parent process via stdin/stdout JSON-lines protocol. The parent process
evaluates candidates using the live Engine evaluation session.

Protocol (one JSON object per line)::

    Parent → Child (stdin):
        {"type": "problem", "algorithm": "...", "variables": [...], ...}
        {"type": "eval_result", "objectives": [...], "constraints": [...]}
        {"type": "cancel"}

    Child → Parent (stdout):
        {"type": "evaluate", "values": [x1, x2, ...]}
        {"type": "result", "termination": "optimal", "x": [...], ...}
        {"type": "error", "code": "...", "message": "..."}

The child reuses the existing ``scipy_backend.solve()`` dispatch with an
``_IPCSession`` that has the same ``evaluate_candidate`` interface as
``SolverEvaluationSession``. No solver logic is duplicated.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from typing import Any

from lib_runtime.solver.cancellation_token import CancellationToken
from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_BACKEND_FAILED,
)
from lib_runtime.solver.solver_types import (
    SolverJob,
    JobState,
    SolverResult,
    SolverPoint,
    TerminationStatus,
    SolverRuntimePolicy,
    SolverProblem,
    SolverVariable,
    SolverObjective,
    SolverConstraint,
)
from lib_runtime.solver.solver_problem import ResolvedSolverEvaluationSession

logger = logging.getLogger(__name__)

_WORKER_MODULE = "lib_runtime.solver.subprocess_worker"
_KILL_GRACE_SECONDS = 3.0


# ─── Parent side: SubprocessSolverRunner ────────────────────────────────────


class SubprocessSolverRunner:
    """Manages a subprocess-isolated solver worker.

    Drop-in replacement for ``SolverJobRunner``.  Spawns a subprocess that
    runs the SciPy optimizer.  Evaluation requests from the subprocess are
    fulfilled by calling ``session.evaluate_candidate`` on the live-Engine
    evaluation session in the parent process.

    Benefits over in-process thread:
    - Crash containment: native code crashes (segfaults) don't kill the parent.
    - Hard cancellation: unresponsive subprocess can be killed with SIGTERM/SIGKILL.
    - Memory isolation: solver allocations don't affect the parent process.
    """

    def __init__(
        self,
        job: SolverJob,
        resolved: ResolvedSolverEvaluationSession,
        policy: SolverRuntimePolicy,
        limits: dict[str, Any],
        on_finished: Any = None,
    ) -> None:
        self._job = job
        self._resolved = resolved
        self._policy = policy
        self._limits = limits
        self._on_finished = on_finished
        self._cancellation_token = CancellationToken()
        self._thread: threading.Thread | None = None
        self._result: SolverResult | None = None
        self._error: Exception | None = None
        self._lock = threading.Lock()
        self._process: subprocess.Popen | None = None

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
        """Launch the worker thread (which spawns the subprocess)."""
        self._thread = threading.Thread(
            target=self._run,
            name=f"solver-sub-{self._job.job_id}",
            daemon=True,
        )
        self._thread.start()

    def cancel(self) -> None:
        """Request cancellation. Sends cancel via IPC, then kills if unresponsive."""
        self._cancellation_token.cancel()

    def join(self, timeout: float | None = None) -> bool:
        """Wait for the worker to finish. Returns True if finished."""
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    # --- Internal ---

    def _build_problem_message(self) -> dict[str, Any]:
        """Build the problem message to send to the subprocess."""
        problem = self._resolved.problem
        return {
            "type": "problem",
            "algorithm": problem.algorithm,
            "backend_id": problem.backend_id,
            "variables": [
                {
                    "initial_value": v.initial_value,
                    "lower_bound": v.lower_bound,
                    "upper_bound": v.upper_bound,
                }
                for v in problem.variables
            ],
            "objectives": [
                {"direction": o.direction}
                for o in problem.objectives
            ],
            "constraints": [
                {
                    "constraint_type": c.constraint_type,
                    "bound_value": c.bound_value,
                    "lower_bound_value": c.lower_bound_value,
                    "upper_bound_value": c.upper_bound_value,
                }
                for c in problem.constraints
            ],
            "limits": self._limits,
            "options": problem.options,
        }

    def _run(self) -> None:
        """Worker thread entry point: spawn subprocess and manage IPC."""
        proc = None
        try:
            proc = subprocess.Popen(
                [sys.executable, "-m", _WORKER_MODULE],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
            self._process = proc

            problem_msg = self._build_problem_message()
            proc.stdin.write(json.dumps(problem_msg) + "\n")
            proc.stdin.flush()

            result = self._ipc_loop(proc)

            with self._lock:
                if isinstance(result, SolverResult):
                    self._result = result
                    self._job.termination_status = result.termination_status
                    self._job.n_evaluations = result.n_evaluations
                    self._job.backend_metadata = result.backend_metadata
                    self._job.message = result.message
                else:
                    err = result
                    self._error = err
                    self._job.termination_status = TerminationStatus.FAILED
                    self._job.message = str(err)

        except Exception as e:
            logger.exception("Subprocess solver job %s failed", self._job.job_id)
            with self._lock:
                self._error = e
            self._job.termination_status = TerminationStatus.FAILED
            self._job.message = str(e)
        finally:
            if proc is not None:
                self._terminate(proc)
            try:
                self._resolved.session.close()
            except Exception as e:
                logger.exception("Restoration failed for job %s", self._job.job_id)
                self._job.termination_status = TerminationStatus.FAILED
                self._job.message = f"Restoration failed: {e}"

            self._job.state = JobState.FINISHED
            self._job.finished_at = time.time()

            if self._on_finished is not None:
                try:
                    self._on_finished(self._job.job_id)
                except Exception:
                    logger.exception(
                        "on_finished callback failed for job %s",
                        self._job.job_id,
                    )

    def _ipc_loop(self, proc: subprocess.Popen) -> SolverResult | Exception:
        """Main IPC loop: read child messages, respond to evaluations."""
        deadline = None
        max_wall = float(self._limits.get("max_wall_time_seconds", 0))
        if max_wall > 0:
            deadline = time.time() + max_wall

        while True:
            if self._cancellation_token.is_cancelled and proc.poll() is None:
                try:
                    proc.stdin.write(json.dumps({"type": "cancel"}) + "\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass

            if deadline is not None and time.time() > deadline and proc.poll() is None:
                self._cancellation_token.cancel()
                try:
                    proc.stdin.write(json.dumps({"type": "cancel"}) + "\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass

            line = proc.stdout.readline()
            if not line:
                if proc.poll() is not None:
                    stderr_data = proc.stderr.read() if proc.stderr else ""
                    return SolverException(
                        SOLVER_BACKEND_FAILED,
                        f"Subprocess exited (code={proc.returncode})"
                        + (f": {stderr_data[:500]}" if stderr_data else ""),
                    )
                continue

            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                logger.warning("Invalid JSON from subprocess: %s", line.strip())
                continue

            mtype = msg.get("type")

            if mtype == "evaluate":
                if self._cancellation_token.is_cancelled:
                    try:
                        proc.stdin.write(json.dumps({"type": "cancel"}) + "\n")
                        proc.stdin.flush()
                    except (BrokenPipeError, OSError):
                        pass
                    continue

                try:
                    eval_result = self._resolved.session.evaluate_candidate(
                        msg["values"]
                    )
                    response = {
                        "type": "eval_result",
                        "objectives": eval_result["objectives"],
                        "constraints": eval_result["constraints"],
                    }
                except Exception as e:
                    response = {
                        "type": "eval_error",
                        "message": str(e),
                    }
                try:
                    proc.stdin.write(json.dumps(response) + "\n")
                    proc.stdin.flush()
                except (BrokenPipeError, OSError):
                    return SolverException(
                        SOLVER_BACKEND_FAILED,
                        "Lost IPC connection to subprocess",
                    )

            elif mtype == "result":
                return self._build_result(msg)

            elif mtype == "error":
                return SolverException(
                    msg.get("code", SOLVER_BACKEND_FAILED),
                    msg.get("message", "Unknown subprocess error"),
                )

            else:
                logger.warning("Unknown message type from subprocess: %s", mtype)

    def _build_result(self, msg: dict) -> SolverResult:
        """Reconstruct SolverResult from subprocess result message."""
        term_str = msg.get("termination", "failed")
        try:
            term_status = TerminationStatus(term_str)
        except ValueError:
            term_status = TerminationStatus.FAILED

        solution = None
        pareto_front = None

        # Deserialize single solution.
        if msg.get("x") is not None:
            solution = SolverPoint(
                variable_values=[float(v) for v in msg.get("x", [])],
                objective_values=[float(v) for v in msg.get("objectives", [])],
                constraint_values=(
                    [float(v) for v in msg["constraint_residuals"]]
                    if msg.get("constraint_residuals") is not None
                    else None
                ),
            )

        # Deserialize Pareto front.
        if msg.get("pareto_front") is not None:
            pareto_front = []
            for pt in msg["pareto_front"]:
                pareto_front.append(SolverPoint(
                    variable_values=[float(v) for v in pt.get("x", [])],
                    objective_values=[float(v) for v in pt.get("objectives", [])],
                    constraint_values=(
                        [float(v) for v in pt["constraint_residuals"]]
                        if pt.get("constraint_residuals") is not None
                        else None
                    ),
                ))

        return SolverResult(
            termination_status=term_status,
            n_evaluations=int(msg.get("n_evaluations", 0)),
            backend_metadata=msg.get("backend_metadata", {}),
            message=msg.get("message", ""),
            solution=solution,
            pareto_front=pareto_front,
        )

    def _terminate(self, proc: subprocess.Popen) -> None:
        """Terminate the subprocess gracefully, then forcefully."""
        if proc.poll() is not None:
            return
        try:
            proc.terminate()
        except OSError:
            pass
        try:
            proc.wait(timeout=_KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass


# ─── Child side: IPC session and main entry point ──────────────────────────


class _IPCSession:
    """Mimics ``SolverEvaluationSession.evaluate_candidate`` via IPC.

    Sends evaluation requests to the parent process via stdout and reads
    responses from stdin.  Raises ``StopIteration`` when the parent sends
    a cancel message.
    """

    def __init__(self) -> None:
        self._closed = False

    def evaluate_candidate(self, values: list[float]) -> dict[str, Any]:
        if self._closed:
            raise SolverException(SOLVER_BACKEND_FAILED, "IPC session closed")

        request = json.dumps({"type": "evaluate", "values": list(values)})
        sys.stdout.write(request + "\n")
        sys.stdout.flush()

        line = sys.stdin.readline()
        if not line:
            raise SolverException(SOLVER_BACKEND_FAILED, "Parent closed IPC")

        msg = json.loads(line)
        mtype = msg.get("type")

        if mtype == "cancel":
            raise StopIteration
        if mtype == "eval_error":
            raise SolverException(SOLVER_BACKEND_FAILED, msg.get("message", "Evaluation error"))
        if mtype == "eval_result":
            return {
                "objectives": msg["objectives"],
                "constraints": msg["constraints"],
            }

        raise SolverException(SOLVER_BACKEND_FAILED, f"Unexpected IPC response: {mtype}")

    def close(self) -> None:
        self._closed = True


class _IPCCancellationToken(CancellationToken):
    """CancellationToken for subprocess — always reports not cancelled.

    Cancellation is handled by the parent sending a ``cancel`` response
    to the next evaluation request, which raises ``StopIteration``.
    The token itself is never set in the subprocess.
    """


def _reconstruct_problem(msg: dict) -> SolverProblem:
    """Reconstruct a SolverProblem from the problem message."""
    variables = [
        SolverVariable(
            cube_id="",
            addr=(),
            initial_value=float(v["initial_value"]),
            lower_bound=v.get("lower_bound"),
            upper_bound=v.get("upper_bound"),
        )
        for v in msg["variables"]
    ]
    objectives = [
        SolverObjective(
            cube_id="",
            addr=(),
            direction=o["direction"],
        )
        for o in msg["objectives"]
    ]
    constraints = [
        SolverConstraint(
            cube_id="",
            addr=(),
            constraint_type=c["constraint_type"],
            bound_value=c.get("bound_value"),
            lower_bound_value=c.get("lower_bound_value"),
            upper_bound_value=c.get("upper_bound_value"),
        )
        for c in msg.get("constraints", [])
    ]
    return SolverProblem(
        variables=variables,
        objectives=objectives,
        constraints=constraints,
        backend_id=msg.get("backend_id", "scipy"),
        algorithm=msg["algorithm"],
        limits=msg.get("limits", {}),
        options=msg.get("options", {}),
    )


def _main() -> None:
    """Subprocess entry point."""
    try:
        line = sys.stdin.readline()
        if not line:
            sys.exit(1)
        msg = json.loads(line)

        problem = _reconstruct_problem(msg)
        session = _IPCSession()
        cancellation_token = _IPCCancellationToken()
        limits = msg.get("limits", {})

        backend_id = msg.get("backend_id", "scipy")
        if backend_id == "pymoo":
            from lib_runtime.solver.backends.pymoo.pymoo_backend import solve as pymoo_solve
            result = pymoo_solve(problem, session, cancellation_token, limits)
        else:
            from lib_runtime.solver.backends.scipy.scipy_backend import solve as scipy_solve
            result = scipy_solve(problem, session, cancellation_token, limits)

        out = {
            "type": "result",
            "termination": result.termination_status.value,
            "n_evaluations": result.n_evaluations,
            "backend_metadata": result.backend_metadata,
            "message": result.message,
        }
        if result.solution is not None:
            out["x"] = result.solution.variable_values
            out["objectives"] = result.solution.objective_values
            out["constraint_residuals"] = result.solution.constraint_values
        if result.pareto_front is not None:
            out["pareto_front"] = [
                {
                    "x": pt.variable_values,
                    "objectives": pt.objective_values,
                    "constraint_residuals": pt.constraint_values,
                }
                for pt in result.pareto_front
            ]
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

    except StopIteration:
        out = {
            "type": "result",
            "termination": "cancelled",
            "n_evaluations": 0,
            "message": "Cancelled by user",
            "x": [],
            "objectives": [],
        }
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()

    except SolverException as e:
        out = {
            "type": "error",
            "code": e.code,
            "message": str(e),
        }
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
        sys.exit(1)

    except Exception as e:
        out = {
            "type": "error",
            "code": SOLVER_BACKEND_FAILED,
            "message": f"{type(e).__name__}: {e}",
        }
        sys.stdout.write(json.dumps(out) + "\n")
        sys.stdout.flush()
        sys.exit(1)


if __name__ == "__main__":
    _main()
