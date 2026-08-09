"""Query handlers for solver queries: solver_status, solver_result,
solver_backend_list, solver_algorithm_list, resolve_solver_cell_ref.

These handlers are registered with QueryService and delegate to
SolverService.  They receive an ExecutionContext and kwargs.
"""

from __future__ import annotations

from typing import Any

from lib_command.commands.solver_commands import (
    _get_solver_service,
    _get_actor_id,
    _get_session_id,
    _get_workspace_id,
)


def query_solver_status(ctx: Any, job_id: str, **kwargs) -> dict:
    """Query solver job status."""
    service = _get_solver_service(ctx)
    return service.solver_status(
        job_id=job_id,
        actor_id=_get_actor_id(ctx),
        session_id=_get_session_id(ctx),
    )


def query_solver_result(ctx: Any, job_id: str, **kwargs) -> dict:
    """Query solver result."""
    service = _get_solver_service(ctx)
    return service.solver_result(
        job_id=job_id,
        actor_id=_get_actor_id(ctx),
        session_id=_get_session_id(ctx),
    )


def query_solver_backend_list(ctx: Any, **kwargs) -> list[dict]:
    """List available solver backends."""
    service = _get_solver_service(ctx)
    return service.list_backends()


def query_solver_job_list(ctx: Any, **kwargs) -> list[dict]:
    """List all solver jobs."""
    service = _get_solver_service(ctx)
    return service.list_jobs()


def query_solver_algorithm_list(ctx: Any, backend_id: str = "scipy", **kwargs) -> list[dict]:
    """List available algorithms for a backend."""
    service = _get_solver_service(ctx)
    return service.list_algorithms(backend_id)


def query_resolve_solver_cell_ref(
    ctx: Any,
    cube_name: str,
    selectors: dict | None = None,
    channel: str = "value",
    **kwargs,
) -> dict:
    """Resolve a cell reference to canonical IDs."""
    service = _get_solver_service(ctx)
    cell_ref = {
        "cube_name": cube_name,
        "selectors": selectors or {},
        "channel": channel,
    }
    # Use the evaluation port's resolve_cell_reference.
    eval_port = getattr(service, "_eval_port", None)
    if eval_port is None:
        raise RuntimeError("SolverService has no evaluation port")
    return eval_port.resolve_cell_reference(
        _get_workspace_id(ctx), cell_ref
    )
