"""lib_runtime.runtime_factory — default local runtime factory.

Creates the canonical Engine + Bus + SessionGateway + CommandSession wiring
used by GUI, local REPL, CLI, runner, and tests.

This module must not import lib_repl or lib_gui.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from lib_openm.api import Engine
from lib_openm.model import Workspace, demo_workspace
from lib_command.core.engine_event_publisher import BusEventPublisher
from lib_storeadapters.json_file_adapter import JsonFileAdapter
from lib_storeadapters.ports import SnapshotType
from lib_storeadapters.sqlite_snapshot_adapter import SQLiteSnapshotStoreAdapter
from lib_storeadapters.timeline_aware_workspace_adapter import TimelineAwareWorkspaceAdapter
from lib_command.core.bootstrap import init_command_services
from lib_command.core.message_bus import get_message_bus
from lib_command.core.executor import get_executor
from lib_command.core.session_gateway import get_session_gateway
from lib_command.core.session_manager import get_session_manager
from lib_runtime.app_host import RuntimeHostContext, RuntimeServices
from lib_runtime.timeline_service import TimelineService
from lib_utils.config import engine as engine_conf
from lib_utils.paths import OM_SESSIONS_DIR


def _read_persistence_mode() -> str:
    """Read [persistence] mode from om-engine.conf; default to 'manual'."""
    mode = engine_conf("persistence", "mode", "manual")
    if isinstance(mode, str):
        return mode.strip().lower()
    return "manual"


def create_runtime(
    workspace: Workspace | None = None,
    engine_type: str = "python",
    enable_dep_tracking: bool = True,
) -> RuntimeHostContext:
    """Create the default local runtime.

    Returns RuntimeHostContext for host consumption only.
    Clients must not receive this object.

    Timeline service is always wired, but the initial "Session Start"
    snapshot is created only when persistence mode is ``auto``.
    """
    if workspace is None:
        workspace = demo_workspace()

    engine = Engine(workspace, event_publisher=BusEventPublisher())
    engine.enable_dependency_tracking(enable_dep_tracking)

    # --- Timeline wiring (always present, snapshot creation is conditional) ---
    OM_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    workspace_id = getattr(workspace, "id", None)

    file_adapter = JsonFileAdapter()
    snapshot_adapter = SQLiteSnapshotStoreAdapter(OM_SESSIONS_DIR)

    # Timeline-aware workspace adapter: file save remains canonical and does not
    # create timeline snapshots. Snapshots are created only via the checkpoint command.
    workspace_adapter = TimelineAwareWorkspaceAdapter(file_adapter)

    # Idempotent safety net — registers commands and starts CommandService
    init_command_services(persistence_adapter=workspace_adapter)

    bus = get_message_bus()
    executor = get_executor()
    session_gateway = get_session_gateway()
    session_mgr = get_session_manager()

    command_session = session_gateway.create_session(
        client_type="local",
        engine=engine,
        workspace=workspace,
        undo_manager=engine.undo_manager,
    )
    _current_session_id = command_session.context.session_id

    ctx = command_session.context
    from lib_runtime.solver.solver_service import SolverService
    from lib_runtime.solver.solver_types import SolverRuntimePolicy
    from lib_runtime.solver_adapters.engine_solver_evaluation import EngineSolverEvaluationAdapter
    from lib_runtime.solver_adapters.engine_solver_apply import EngineSolverApplyAdapter
    from lib_runtime.solver_adapters.runtime_event_publisher import RuntimeEventPublisher

    solver_service = SolverService(
        evaluation_port=EngineSolverEvaluationAdapter(engine),
        apply_port=EngineSolverApplyAdapter(engine),
        event_port=RuntimeEventPublisher(event_publisher=None),
        policy=SolverRuntimePolicy(),
    )

    services = RuntimeServices(
        timeline=TimelineService(
            snapshot_adapter=snapshot_adapter,
            workspace_provider=lambda: ctx.workspace,
            workspace_consumer=lambda ws: (
                ctx.engine.replace_workspace(ws),
                setattr(ctx, "workspace", ws),
            )[0],
        ),
        solver_service=solver_service,
    )
    command_session.context.services = services

    # Undo manager must not carry history across a restore; clear it when the
    # restore command succeeds.
    bus.subscribe(
        "command.restore_checkpoint.succeeded",
        lambda _event: ctx.engine.undo_manager.clear(),
    )

    if workspace_id is not None:
        services.timeline.set_workspace_id(workspace_id)

    persistence_mode = _read_persistence_mode()
    if persistence_mode == "auto" and workspace_id is not None:
        snapshots = services.timeline.load_snapshots()
        has_session_start = any(
            getattr(s, "description", None) == "Session Start" for s in snapshots
        )
        if not has_session_start:
            services.timeline.create_snapshot(
                "Session Start", snapshot_type=SnapshotType.SESSION_START
            )

    return RuntimeHostContext(
        engine=engine,
        workspace=workspace,
        bus=bus,
        executor=executor,
        session_gateway=session_gateway,
        session_mgr=session_mgr,
        command_session=command_session,
        services=services,
    )
