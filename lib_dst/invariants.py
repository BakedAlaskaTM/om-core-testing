"""Phase 2 persistence, cache, snapshot, and cold-engine invariants."""

from __future__ import annotations

import hashlib
import re
from typing import Any

from lib_dst.executor import (
    create_trace_runtime,
    execute_operations,
    report_runtime,
)
from lib_dst.faults import (
    FaultInjectingWorkspaceAdapter,
    PersistenceFaultError,
    decisions_from_trace,
)
from lib_dst.observation import observe_workspace
from lib_dst.schemas import (
    CanonicalObservation,
    ConvergenceReport,
    InvariantCheck,
    TraceTranscript,
)
from lib_runtime.lifecycle import reset_runtime_state
from lib_storeadapters.memory_adapters import InMemoryJsonAdapter, InMemorySnapshotAdapter
from lib_storeadapters.ports import SnapshotStoreAdapter, SnapshotType, WorkspaceId


PERSISTENCE_PATH = "memory://dst/workspace.openm"


class Phase2StageError(RuntimeError):
    def __init__(self, stage: str, code: str, detail: str) -> None:
        self.stage = stage
        self.code = code
        self.detail = detail
        super().__init__(f"{stage}: {code}: {detail}")


def _digest(observation: CanonicalObservation) -> str:
    return hashlib.sha256(observation.to_json().encode("utf-8")).hexdigest()


def _check(
    name: str,
    expected: CanonicalObservation,
    actual: CanonicalObservation,
) -> InvariantCheck:
    return InvariantCheck(
        name=name,
        passed=expected == actual,
        expected_sha256=_digest(expected),
        actual_sha256=_digest(actual),
        detail=None if expected == actual else "canonical observations differ",
    )


def _command(runtime: Any, stage: str, command: str, **params: Any) -> dict[str, Any]:
    result = runtime.session.execute(command, **params)
    if result.success:
        return result.data or {}
    detail = result.error or "command failed"
    match = re.search(r"DSTFAULT\[([^]]+)\]", detail)
    code = match.group(1) if match else "command-failed"
    raise Phase2StageError(stage, code, detail)


def _raise_stage(stage: str, exc: Exception) -> None:
    if isinstance(exc, (PersistenceFaultError, Phase2StageError)):
        raise exc
    raise Phase2StageError(stage, type(exc).__name__, str(exc)) from exc


def execute_convergence(
    trace: TraceTranscript,
    *,
    workspace_adapter: InMemoryJsonAdapter | None = None,
    snapshot_adapter: SnapshotStoreAdapter | None = None,
) -> ConvergenceReport:
    """Execute all Phase 2 invariants, using a genuinely fresh cold engine."""
    storage = workspace_adapter or InMemoryJsonAdapter()
    snapshots = snapshot_adapter or InMemorySnapshotAdapter()
    decisions = decisions_from_trace(trace)
    adapter = (
        FaultInjectingWorkspaceAdapter(storage, decisions) if decisions else storage
    )

    runtime_a = create_trace_runtime(
        trace,
        workspace_adapter=adapter,
        snapshot_adapter=snapshots,
    )
    try:
        execute_operations(runtime_a, trace)
        execution = report_runtime(runtime_a, trace)
        incremental = execution.observation

        _command(runtime_a, "cache.recalculate.first", "run_recalculation", scope="all")
        after_first = observe_workspace(runtime_a.workspace, trace.observe)
        _command(runtime_a, "cache.recalculate.second", "run_recalculation", scope="all")
        after_second = observe_workspace(runtime_a.workspace, trace.observe)

        workspace_id = WorkspaceId(runtime_a.workspace.id)
        try:
            snapshot_id = snapshots.create_snapshot(
                workspace_id,
                runtime_a.workspace,
                "DST Phase 2 invariant",
                SnapshotType.MANUAL,
            )
            restored = snapshots.restore_snapshot(workspace_id, snapshot_id)
            snapshot_observation = observe_workspace(restored, trace.observe)
        except Exception as exc:
            _raise_stage("persistence.snapshot", exc)

        _command(
            runtime_a,
            "persistence.save",
            "save_workspace",
            path=PERSISTENCE_PATH,
        )
    finally:
        reset_runtime_state(runtime_a)

    try:
        loaded_workspace = adapter.load_workspace(PERSISTENCE_PATH)
    except Exception as exc:
        _raise_stage("persistence.load", exc)

    runtime_b = create_trace_runtime(
        trace,
        workspace=loaded_workspace,
        workspace_adapter=adapter,
        snapshot_adapter=snapshots,
    )
    try:
        _command(runtime_b, "cold.configure", "set_multithread_recompute", enabled=False)
        try:
            runtime_b.engine.bootstrap_dependency_graph()
        except Exception as exc:
            _raise_stage("cold.bootstrap", exc)
        _command(runtime_b, "cold.recalculate", "run_recalculation", scope="all")
        cold = observe_workspace(runtime_b.workspace, trace.observe)
    finally:
        reset_runtime_state(runtime_b)

    checks = (
        _check("cache-first-recalculation", incremental, after_first),
        _check("cache-second-recalculation", incremental, after_second),
        _check("snapshot-roundtrip", incremental, snapshot_observation),
        _check("fresh-engine-convergence", incremental, cold),
    )
    return ConvergenceReport(trace.sha256(), execution, checks)
