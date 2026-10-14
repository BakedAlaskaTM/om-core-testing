"""Compatibility entry point for the canonical local runtime composition root."""

from __future__ import annotations

from lib_command.core.message_bus import MessageBus
from lib_openm.model import Workspace
from lib_openm.ports import WorkspacePersistenceAdapter
from lib_runtime.app_host import (
    RuntimeHostContext,
    RuntimeServices,
    create_runtime_context,
)
from lib_runtime.providers import RuntimeProviders
from lib_storeadapters.ports import SnapshotStoreAdapter


def create_runtime(
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
) -> RuntimeHostContext:
    """Create the default local runtime while preserving the historical API."""
    return create_runtime_context(
        workspace=workspace,
        engine_type=engine_type,
        enable_dep_tracking=enable_dep_tracking,
        bus=bus,
        providers=providers,
        workspace_adapter=workspace_adapter,
        snapshot_adapter=snapshot_adapter,
        services=services,
        enable_solver=enable_solver,
        persistence_mode=persistence_mode,
        client_type="local",
    )
