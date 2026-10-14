from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from lib_command.core.message_bus import MessageBus, get_message_bus
from lib_dst.phase0_probe import hand_authored_trace, run_trace
from lib_dst.schemas import TraceOperation, TraceTranscript
from lib_openm.model import Workspace
from lib_runtime.lifecycle import reset_runtime_state
from lib_runtime.providers import RuntimeProviders
from lib_runtime.runtime_factory import create_runtime
from lib_storeadapters.memory_adapters import (
    InMemoryJsonAdapter,
    InMemorySnapshotAdapter,
)
from lib_storeadapters.ports import SnapshotType, WorkspaceId


@pytest.fixture(autouse=True)
def clean_runtime_state():
    reset_runtime_state()
    yield
    reset_runtime_state()


def test_trace_schema_round_trips_canonically() -> None:
    trace = hand_authored_trace()

    restored = TraceTranscript.from_json(trace.to_json())

    assert restored == trace
    assert restored.to_json() == trace.to_json()


def test_trace_schema_rejects_noncontiguous_steps() -> None:
    trace = hand_authored_trace()

    with pytest.raises(ValueError, match="contiguous"):
        TraceTranscript(
            environment=trace.environment,
            operations=(TraceOperation(1, "noop", "none"),),
            observe=trace.observe,
        )


def test_runtime_accepts_injected_phase0_dependencies_and_closes() -> None:
    bus = MessageBus()
    providers = RuntimeProviders.system()
    workspace_adapter = InMemoryJsonAdapter()
    snapshot_adapter = InMemorySnapshotAdapter()
    runtime = create_runtime(
        Workspace.create("Injected"),
        "python",
        True,
        bus=bus,
        providers=providers,
        workspace_adapter=workspace_adapter,
        snapshot_adapter=snapshot_adapter,
        enable_solver=False,
        persistence_mode="manual",
    )
    session_id = runtime.session.session_id

    result = runtime.session.execute(
        "create_dimension", name="Region", dim_type="set"
    )

    assert result.success
    assert runtime.bus is bus
    assert runtime.providers is providers
    assert runtime.session.context.providers is providers
    assert runtime.session.context.persistence_adapter is workspace_adapter
    assert runtime.services.solver_service is None
    assert runtime.services.timeline._snapshot_adapter is snapshot_adapter
    assert runtime.session_mgr.get_record(session_id) is not None

    runtime.close()
    runtime.close()

    assert runtime.session_mgr.get_record(session_id) is None


def test_runtime_reset_discards_process_singletons() -> None:
    runtime = create_runtime(
        workspace=Workspace.create("Reset"),
        bus=MessageBus(),
        workspace_adapter=InMemoryJsonAdapter(),
        snapshot_adapter=InMemorySnapshotAdapter(),
        enable_solver=False,
        persistence_mode="manual",
    )
    old_bus = runtime.bus

    reset_runtime_state(runtime)

    assert get_message_bus() is not old_bus


def test_in_memory_snapshot_adapter_restores_an_isolated_workspace() -> None:
    adapter = InMemorySnapshotAdapter()
    workspace = Workspace.create("Before")
    workspace_id = WorkspaceId(workspace.id)

    snapshot_id = adapter.create_snapshot(
        workspace_id,
        workspace,
        "checkpoint",
        SnapshotType.MANUAL,
    )
    workspace.name = "After"

    restored = adapter.restore_snapshot(workspace_id, snapshot_id)

    assert restored.name == "Before"
    assert adapter.get_latest_snapshot(workspace_id).snapshot_id == snapshot_id


def test_hand_authored_trace_observation_excludes_generated_ids() -> None:
    first = run_trace(hand_authored_trace())
    second = run_trace(hand_authored_trace())

    assert first == second
    assert "_" not in first.state["dimensions"][0]["name"]
    assert first.state["cubes"][0]["values"][0]["value"] == 42


def _run_probe(hash_seed: str) -> dict:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = hash_seed
    environment["TZ"] = "UTC"
    completed = subprocess.run(
        [sys.executable, "-m", "lib_dst.phase0_probe"],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def test_two_clean_processes_produce_identical_observations() -> None:
    assert _run_probe("101") == _run_probe("202")
