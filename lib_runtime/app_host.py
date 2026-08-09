"""lib_runtime.app_host — runtime composition root.

Constructs and wires Engine, MessageBus, CommandService, QueryService,
SessionGateway/SessionManager, and client sessions.

This is the canonical composition root for the OpenModeling runtime.
No client code should import from this module.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from lib_openm.api import Engine
from lib_openm.model import Workspace, demo_workspace
from lib_command.core.engine_event_publisher import BusEventPublisher
from lib_runtime.engine_factory import create_engine
from lib_storeadapters.json_file_adapter import JsonFileAdapter
from lib_storeadapters.ports import SnapshotType
from lib_storeadapters.sqlite_snapshot_adapter import SQLiteSnapshotStoreAdapter
from lib_storeadapters.timeline_aware_workspace_adapter import TimelineAwareWorkspaceAdapter
from lib_command.core.bootstrap import init_command_services
from lib_command.core.message_bus import MessageBus, get_message_bus
from lib_command.core.executor import CommandExecutor, get_executor
from lib_command.core.session import CommandSession
from lib_command.core.session_gateway import SessionGateway, get_session_gateway
from lib_command.core.session_manager import SessionManager, get_session_manager
from lib_runtime.timeline_service import TimelineService
from lib_utils.config import engine as engine_conf
from lib_utils.paths import OM_SESSIONS_DIR


def _read_persistence_mode() -> str:
    """Read [persistence] mode from om-engine.conf; default to 'manual'."""
    mode = engine_conf("persistence", "mode", "manual")
    if isinstance(mode, str):
        return mode.strip().lower()
    return "manual"


@dataclass
class RuntimeServices:
    """Runtime-wired services accessible to command handlers via ctx.services."""

    timeline: Any = None  # lib_runtime.timeline_service.TimelineService
    solver_service: Any = None  # lib_runtime.solver.solver_service.SolverService


@dataclass
class RuntimeHostContext:
    """Host-internal runtime context. Never crosses into clients."""

    engine: Engine
    workspace: Workspace
    bus: MessageBus
    executor: CommandExecutor
    session_gateway: SessionGateway
    session_mgr: SessionManager
    command_session: CommandSession
    services: RuntimeServices

    @property
    def session(self) -> CommandSession:
        """Convenience alias for command_session."""
        return self.command_session


def create_runtime_context(
    workspace: Workspace | None = None,
    engine_type: str = "python",
    enable_dep_tracking: bool = True,
) -> RuntimeHostContext:
    """Create a fully wired runtime context.

    Returns RuntimeHostContext for host consumption only.
    Clients must not receive this object.
    """
    if workspace is None:
        workspace = demo_workspace()

    engine = create_engine(
        workspace,
        enable_dependency_tracking=enable_dep_tracking,
    )

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
        client_type="gui",
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

    # Create a Session Start snapshot only in auto mode. In manual mode,
    # the timeline starts empty ("(empty)") and snapshots are created
    # only by explicit user command.
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


def create_server_session(
    workspace: Workspace | None = None,
    engine_type: str = "python",
    enable_dep_tracking: bool = True,
) -> CommandSession:
    """Create a runtime-internal local CommandSession.

    Used by runtime internals and focused tests only.
    Launched application clients receive RemoteCommandSession, not this.
    """
    ctx = create_runtime_context(
        workspace=workspace,
        engine_type=engine_type,
        enable_dep_tracking=enable_dep_tracking,
    )
    return ctx.command_session
