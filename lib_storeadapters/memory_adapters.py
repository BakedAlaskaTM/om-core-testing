"""In-memory workspace persistence adapters for tests and isolation.

These adapters do not touch the filesystem. ``InMemoryAdapter`` deep-copies the
workspace object graph. ``InMemoryJsonAdapter`` round-trips through the same
JSON payload used by ``JsonFileAdapter``.
"""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from lib_openm.model import Workspace
from lib_storeadapters.json_file_adapter import (
    workspace_from_json_string,
    workspace_to_json_string,
)
from lib_storeadapters.ports import (
    BranchId,
    BranchInfo,
    SnapshotId,
    SnapshotInfo,
    SnapshotStoreAdapter,
    SnapshotType,
    WorkspaceId,
    WorkspacePersistenceAdapter,
)


class InMemoryAdapter(WorkspacePersistenceAdapter):
    """Stateless in-memory adapter that deep-copies workspaces on save/load."""

    def __init__(self) -> None:
        self._storage: dict[str, Workspace] = {}

    def save_workspace(self, path: str | Path, workspace: Workspace) -> None:
        """Persist a deep copy of the workspace in memory."""
        self._storage[str(path)] = deepcopy(workspace)

    def load_workspace(self, path: str | Path) -> Workspace:
        """Return a deep copy of the previously saved workspace."""
        return deepcopy(self._storage[str(path)])

    def load_workspace_profiled(
        self, path: str | Path
    ) -> tuple[Workspace, dict[str, Any]]:
        """Load and return a synthetic diagnostic profile."""
        ws = self.load_workspace(path)
        profile = {
            "path": str(path),
            "timings_ms": {},
            "counts": {
                "dimensions": len(ws.dimensions),
                "cubes": len(ws.cubes),
                "views": len(ws.views),
                "rules": len(ws.rules),
            },
            "legacy_ui_state": {},
        }
        return ws, profile


class InMemoryJsonAdapter(WorkspacePersistenceAdapter):
    """In-memory adapter that round-trips workspaces through the JSON payload."""

    def __init__(self) -> None:
        self._storage: dict[str, str] = {}

    def save_workspace(self, path: str | Path, workspace: Workspace) -> None:
        """Persist the workspace as a JSON string in memory."""
        self._storage[str(path)] = workspace_to_json_string(workspace)

    def load_workspace(self, path: str | Path) -> Workspace:
        """Load a workspace from the stored JSON string."""
        return workspace_from_json_string(self._storage[str(path)])

    def load_workspace_profiled(
        self, path: str | Path
    ) -> tuple[Workspace, dict[str, Any]]:
        """Load and return a synthetic diagnostic profile.

        The profile is intentionally minimal because the JSON round-trip is
        not file-based. The returned shape matches ``JsonFileAdapter`` for
        command-layer compatibility.
        """
        ws = self.load_workspace(path)
        profile = {
            "path": str(path),
            "timings_ms": {},
            "counts": {
                "dimensions": len(ws.dimensions),
                "cubes": len(ws.cubes),
                "views": len(ws.views),
                "rules": len(ws.rules),
            },
            "legacy_ui_state": {},
        }
        return ws, profile

    def replace_payload(self, path: str | Path, payload: str) -> None:
        """Replace a stored payload for deterministic corruption tests."""
        key = str(path)
        if key not in self._storage:
            raise KeyError(key)
        self._storage[key] = payload

    def payload(self, path: str | Path) -> str:
        """Return the exact serialized payload stored for *path*."""
        return self._storage[str(path)]


class InMemorySnapshotAdapter(SnapshotStoreAdapter):
    """Process-local snapshot store for isolated runtimes and DST workers."""

    def __init__(self) -> None:
        self._snapshots: dict[WorkspaceId, dict[SnapshotId, SnapshotInfo]] = {}
        self._payloads: dict[tuple[WorkspaceId, SnapshotId], Workspace] = {}
        self._branches: dict[WorkspaceId, dict[BranchId, BranchInfo]] = {}
        self._current_branches: dict[WorkspaceId, BranchId] = {}
        self._next_snapshot = 1

    def _ensure_main(self, workspace_id: WorkspaceId) -> BranchId:
        branches = self._branches.setdefault(workspace_id, {})
        main = BranchId("main")
        if main not in branches:
            branches[main] = BranchInfo(
                branch_id=main,
                workspace_id=workspace_id,
                name="main",
                head_snapshot_id=None,
                created_at=datetime.now(timezone.utc),
            )
        self._current_branches.setdefault(workspace_id, main)
        return main

    def list_snapshots(self, workspace_id: WorkspaceId) -> list[SnapshotInfo]:
        return list(self._snapshots.get(workspace_id, {}).values())

    def create_snapshot(
        self,
        workspace_id: WorkspaceId,
        workspace: Workspace,
        description: str,
        snapshot_type: SnapshotType = SnapshotType.MANUAL,
        branch_id: BranchId | None = None,
        parent_id: SnapshotId | None = None,
        content_hash: str | None = None,
    ) -> SnapshotId:
        branch_id = branch_id or self._current_branches.get(workspace_id)
        if branch_id is None:
            branch_id = self._ensure_main(workspace_id)
        if branch_id not in self._branches.setdefault(workspace_id, {}):
            self.create_branch(workspace_id, str(branch_id))

        snapshot_id = SnapshotId(f"memory-{self._next_snapshot}")
        self._next_snapshot += 1
        info = SnapshotInfo(
            snapshot_id=snapshot_id,
            workspace_id=workspace_id,
            description=description,
            snapshot_type=snapshot_type,
            created_at=datetime.now(timezone.utc),
            content_hash=content_hash,
            branch_id=branch_id,
            parent_id=parent_id,
        )
        self._snapshots.setdefault(workspace_id, {})[snapshot_id] = info
        self._payloads[(workspace_id, snapshot_id)] = deepcopy(workspace)
        branch = self._branches[workspace_id][branch_id]
        self._branches[workspace_id][branch_id] = replace(
            branch, head_snapshot_id=snapshot_id
        )
        return snapshot_id

    def restore_snapshot(
        self, workspace_id: WorkspaceId, snapshot_id: SnapshotId
    ) -> Workspace:
        return deepcopy(self._payloads[(workspace_id, snapshot_id)])

    def delete_snapshot(
        self, workspace_id: WorkspaceId, snapshot_id: SnapshotId
    ) -> bool:
        snapshots = self._snapshots.get(workspace_id, {})
        info = snapshots.pop(snapshot_id, None)
        if info is None:
            return False
        self._payloads.pop((workspace_id, snapshot_id), None)
        for branch_id, branch in list(self._branches.get(workspace_id, {}).items()):
            if branch.head_snapshot_id == snapshot_id:
                self._branches[workspace_id][branch_id] = replace(
                    branch, head_snapshot_id=None
                )
        return True

    def rename_snapshot(
        self,
        workspace_id: WorkspaceId,
        snapshot_id: SnapshotId,
        new_description: str,
    ) -> bool:
        info = self._snapshots.get(workspace_id, {}).get(snapshot_id)
        if info is None:
            return False
        self._snapshots[workspace_id][snapshot_id] = replace(
            info, description=new_description
        )
        return True

    def get_latest_snapshot(
        self,
        workspace_id: WorkspaceId,
        snapshot_type: SnapshotType | None = None,
    ) -> SnapshotInfo | None:
        snapshots = self.list_snapshots(workspace_id)
        if snapshot_type is not None:
            snapshots = [s for s in snapshots if s.snapshot_type == snapshot_type]
        return snapshots[-1] if snapshots else None

    def list_branches(self, workspace_id: WorkspaceId) -> list[BranchInfo]:
        self._ensure_main(workspace_id)
        return list(self._branches[workspace_id].values())

    def create_branch(
        self,
        workspace_id: WorkspaceId,
        branch_name: str,
        snapshot_id: SnapshotId | None = None,
    ) -> BranchId:
        branch_id = BranchId(branch_name)
        branches = self._branches.setdefault(workspace_id, {})
        if branch_id in branches:
            raise ValueError(f"Branch already exists: {branch_name}")
        branches[branch_id] = BranchInfo(
            branch_id=branch_id,
            workspace_id=workspace_id,
            name=branch_name,
            head_snapshot_id=snapshot_id,
            created_at=datetime.now(timezone.utc),
        )
        return branch_id

    def get_current_branch(self, workspace_id: WorkspaceId) -> BranchId | None:
        return self._current_branches.get(workspace_id) or self._ensure_main(workspace_id)

    def switch_branch(
        self, workspace_id: WorkspaceId, branch_id: BranchId
    ) -> bool:
        if branch_id not in self._branches.get(workspace_id, {}):
            return False
        self._current_branches[workspace_id] = branch_id
        return True

    def update_snapshot_branch(
        self,
        workspace_id: WorkspaceId,
        snapshot_id: SnapshotId,
        branch_id: BranchId,
    ) -> bool:
        info = self._snapshots.get(workspace_id, {}).get(snapshot_id)
        if info is None or branch_id not in self._branches.get(workspace_id, {}):
            return False
        self._snapshots[workspace_id][snapshot_id] = replace(
            info, branch_id=branch_id
        )
        return True

    def update_snapshot_parent(
        self,
        workspace_id: WorkspaceId,
        snapshot_id: SnapshotId,
        parent_id: SnapshotId | None,
    ) -> bool:
        info = self._snapshots.get(workspace_id, {}).get(snapshot_id)
        if info is None:
            return False
        self._snapshots[workspace_id][snapshot_id] = replace(
            info, parent_id=parent_id
        )
        return True
