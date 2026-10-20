"""Canonical composition root for the local OpenModeling runtime."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from lib_command.core.bootstrap import init_command_services
from lib_command.core.engine_event_publisher import BusEventPublisher
from lib_command.core.executor import CommandExecutor, get_executor
from lib_command.core.message_bus import MessageBus, get_message_bus, install_message_bus
from lib_command.core.session import CommandSession
from lib_command.core.session_gateway import SessionGateway, get_session_gateway
from lib_command.core.session_manager import SessionManager, get_session_manager
from lib_openm.api import Engine
from lib_openm.model import Workspace, demo_workspace
from lib_openm.ports import WorkspacePersistenceAdapter
from lib_runtime.engine_factory import create_engine
from lib_runtime.providers import RuntimeProviders
from lib_runtime.timeline_service import TimelineService
from lib_storeadapters.json_file_adapter import JsonFileAdapter
from lib_storeadapters.ports import SnapshotStoreAdapter, SnapshotType
from lib_storeadapters.sqlite_snapshot_adapter import SQLiteSnapshotStoreAdapter
from lib_storeadapters.timeline_aware_workspace_adapter import TimelineAwareWorkspaceAdapter
from lib_utils.config import engine as engine_conf
from lib_utils.ids import bind_id_provider, reset_id_provider
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

    timeline: Any = None
    solver_service: Any = None


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
    providers: RuntimeProviders = field(default_factory=RuntimeProviders.system)
    _restore_callback: Any = field(default=None, repr=False)
    _id_provider_token: Any = field(default=None, repr=False)
    _closed: bool = field(default=False, init=False, repr=False)

    @property
    def session(self) -> CommandSession:
        """Convenience alias for command_session."""
        return self.command_session

    def close(self) -> None:
        """Release resources owned directly by this runtime context."""
        if self._closed:
            return
        self._closed = True
        if self._restore_callback is not None:
            self.bus.unsubscribe(
                "command.restore_checkpoint.succeeded",
                self._restore_callback,
            )
        self.session_mgr.close_session(self.command_session.session_id)
        self.engine.shutdown_engine()
        if self._id_provider_token is not None:
            reset_id_provider(self._id_provider_token)
            self._id_provider_token = None


def _build_solver_service(engine: Engine) -> Any:
    from lib_runtime.solver.solver_service import SolverService
    from lib_runtime.solver.solver_types import SolverRuntimePolicy
    from lib_runtime.solver_adapters.engine_solver_apply import EngineSolverApplyAdapter
    from lib_runtime.solver_adapters.engine_solver_evaluation import EngineSolverEvaluationAdapter
    from lib_runtime.solver_adapters.runtime_event_publisher import RuntimeEventPublisher

    return SolverService(
        evaluation_port=EngineSolverEvaluationAdapter(engine),
        apply_port=EngineSolverApplyAdapter(engine),
        event_port=RuntimeEventPublisher(event_publisher=None),
        policy=SolverRuntimePolicy(),
    )


def create_runtime_context(
    workspace: Workspace | None = None,
    engine_type: str = "python",
    enable_dep_tracking: bool = True,
    *,
    bus: MessageBus | None = None,
    providers: RuntimeProviders | None = None,
    workspace_adapter: WorkspacePersistenceAdapter | None = None,
    snapshot_adapter: SnapshotStoreAdapter | None = None,
    services: RuntimeServices | None = None,
    enable_solver: bool = True,
    persistence_mode: str | None = None,
    client_type: str = "gui",
) -> RuntimeHostContext:
    """Create a fully wired runtime context.

    Existing callers retain production defaults. Injected dependencies are keyword-only.
    A supplied ``services`` object is authoritative and cannot be combined with a
    separately supplied snapshot adapter.
    """
    del engine_type  # Preserved compatibility parameter; remote hosts use engine_factory.
    if workspace is None:
        workspace = demo_workspace()
    if services is not None and snapshot_adapter is not None:
        raise ValueError(
            "snapshot_adapter cannot be supplied with authoritative RuntimeServices"
        )

    providers = providers or RuntimeProviders.system()
    if bus is None:
        bus = get_message_bus()
    else:
        install_message_bus(bus)

    engine = create_engine(
        workspace,
        enable_dependency_tracking=enable_dep_tracking,
        event_publisher=BusEventPublisher(bus=bus),
    )
    workspace_id = getattr(workspace, "id", None)

    if workspace_adapter is None:
        workspace_adapter = TimelineAwareWorkspaceAdapter(JsonFileAdapter())

    init_command_services(persistence_adapter=workspace_adapter, bus=bus)
    executor = get_executor()
    session_gateway = get_session_gateway()
    session_mgr = get_session_manager()

    command_session = session_gateway.create_session(
        client_type=client_type,
        engine=engine,
        workspace=workspace,
        undo_manager=engine.undo_manager,
        providers=providers,
    )
    ctx = command_session.context
    ctx.providers = providers

    if services is None:
        if snapshot_adapter is None:
            OM_SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            snapshot_adapter = SQLiteSnapshotStoreAdapter(OM_SESSIONS_DIR)
        services = RuntimeServices(
            timeline=TimelineService(
                snapshot_adapter=snapshot_adapter,
                workspace_provider=lambda: ctx.workspace,
                workspace_consumer=lambda ws: (
                    ctx.engine.replace_workspace(ws),
                    setattr(ctx, "workspace", ws),
                )[0],
            ),
            solver_service=_build_solver_service(engine) if enable_solver else None,
        )
    ctx.services = services

    restore_callback = lambda _event: ctx.engine.undo_manager.clear()
    bus.subscribe("command.restore_checkpoint.succeeded", restore_callback)

    if services.timeline is not None and workspace_id is not None:
        services.timeline.set_workspace_id(workspace_id)

    resolved_persistence_mode = persistence_mode or _read_persistence_mode()
    if (
        resolved_persistence_mode == "auto"
        and services.timeline is not None
        and workspace_id is not None
    ):
        snapshots = services.timeline.load_snapshots()
        has_session_start = any(
            getattr(snapshot, "description", None) == "Session Start"
            for snapshot in snapshots
        )
        if not has_session_start:
            services.timeline.create_snapshot(
                "Session Start", snapshot_type=SnapshotType.SESSION_START
            )

    id_provider_token = bind_id_provider(providers.ids)
    return RuntimeHostContext(
        engine=engine,
        workspace=workspace,
        bus=bus,
        executor=executor,
        session_gateway=session_gateway,
        session_mgr=session_mgr,
        command_session=command_session,
        services=services,
        providers=providers,
        _restore_callback=restore_callback,
        _id_provider_token=id_provider_token,
    )


def create_server_session(
    workspace: Workspace | None = None,
    engine_type: str = "python",
    enable_dep_tracking: bool = True,
) -> CommandSession:
    """Create a runtime-internal local CommandSession."""
    runtime = create_runtime_context(
        workspace=workspace,
        engine_type=engine_type,
        enable_dep_tracking=enable_dep_tracking,
    )
    return runtime.command_session
