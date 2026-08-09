"""Command handlers for solver commands: run_solver, cancel_solver, apply_solver_result.

These handlers are registered in CommandRegistry and delegate to
SolverService.  They receive an ExecutionContext and kwargs.
"""

from __future__ import annotations

from typing import Any

from lib_runtime.solver.solver_errors import SolverException


def _get_solver_service(ctx: Any) -> Any:
    """Retrieve the SolverService from the context's services."""
    if ctx.services is None:
        raise RuntimeError("No services available in execution context")
    service = getattr(ctx.services, "solver_service", None)
    if service is None:
        raise RuntimeError("SolverService not available in context services")
    return service


def _get_actor_id(ctx: Any) -> str:
    """Extract actor ID from context."""
    return getattr(ctx, "actor_id", "unknown")


def _get_session_id(ctx: Any) -> str:
    """Extract session ID from context."""
    return ctx.session_id or "unknown"


def _get_workspace_id(ctx: Any) -> str:
    """Extract workspace ID from context."""
    if ctx.workspace is not None:
        return getattr(ctx.workspace, "id", "unknown")
    return "unknown"


def cmd_run_solver(
    ctx: Any,
    problem_spec: dict | None = None,
    variables: list | None = None,
    objectives: list | None = None,
    constraints: list | None = None,
    backend: str = "scipy",
    algorithm: str = "cobyla",
    limits: dict | None = None,
    expected_revision: str | None = None,
    **kwargs,
) -> dict:
    """Run a solver job asynchronously.

    Returns dict with ``job_id`` and ``status``.
    """
    service = _get_solver_service(ctx)

    # Build problem_spec from individual params if not provided directly.
    if problem_spec is None:
        problem_spec = {
            "backend": backend,
            "algorithm": algorithm,
            "variables": variables or [],
            "objectives": objectives or [],
            "constraints": constraints or [],
            "limits": limits or {},
        }

    return service.run_solver(
        workspace_id=_get_workspace_id(ctx),
        actor_id=_get_actor_id(ctx),
        session_id=_get_session_id(ctx),
        problem_spec=problem_spec,
        expected_revision=expected_revision,
        limits=limits,
    )


def cmd_cancel_solver(
    ctx: Any,
    job_id: str,
    **kwargs,
) -> dict:
    """Cancel a running solver job."""
    service = _get_solver_service(ctx)
    return service.cancel_solver(
        job_id=job_id,
        actor_id=_get_actor_id(ctx),
        session_id=_get_session_id(ctx),
    )


def cmd_apply_solver_result(
    ctx: Any,
    job_id: str,
    apply_request_id: str | None = None,
    pareto_index: int | None = None,
    allow_nonoptimal: bool = False,
    **kwargs,
) -> dict:
    """Apply solver result values to the workspace."""
    service = _get_solver_service(ctx)

    if apply_request_id is None:
        import uuid
        apply_request_id = f"apply_{uuid.uuid4().hex[:12]}"

    result = service.apply_solver_result(
        job_id=job_id,
        actor_id=_get_actor_id(ctx),
        session_id=_get_session_id(ctx),
        apply_request_id=apply_request_id,
        pareto_index=pareto_index,
        allow_nonoptimal=allow_nonoptimal,
    )

    ctx.refresh()

    return result


def cmd_export_solver(
    ctx: Any,
    job_id: str,
    file_path: str | None = None,
    **kwargs,
) -> dict:
    """Export a solver job diagnostic report as JSON.

    If file_path is provided, writes to that path atomically (temp file +
    rename) with path traversal prevention.  Always returns the report
    dict.
    """
    import json
    import os
    import tempfile

    service = _get_solver_service(ctx)
    report = service.export_job(
        job_id=job_id,
        actor_id=_get_actor_id(ctx),
        session_id=_get_session_id(ctx),
    )

    if file_path is not None:
        # Path traversal prevention: reject paths with .. components.
        normalized = os.path.normpath(file_path)
        parts = normalized.split(os.sep)
        if ".." in parts:
            raise SolverException("SOLVER_EXPORT_PATH_TRAVERSAL", f"Path {file_path} contains '..' traversal")
        target = os.path.realpath(file_path)
        # Atomic write: temp file + rename.
        dir_ = os.path.dirname(target) or "."
        fd, tmp_path = tempfile.mkstemp(dir=dir_, suffix=".json", prefix=".solver_export_")
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(report, f, indent=2, sort_keys=True)
            os.replace(tmp_path, target)
        except Exception:
            os.unlink(tmp_path)
            raise
        report["_exported_to"] = target

    return report
