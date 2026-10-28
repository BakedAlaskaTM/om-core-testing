"""Exact trace replay against the real command and engine stack."""

from __future__ import annotations

from typing import Any

from lib_command.core.message_bus import MessageBus
from lib_dst.observation import observe_workspace
from lib_dst.oracle import matches
from lib_dst.schemas import ExecutionReport, TraceOperation, TraceTranscript
from lib_dst.seeding import providers_for_seed
from lib_dst.symbols import Symbol, SymbolKind, SymbolTable
from lib_openm.model import Workspace
from lib_runtime.lifecycle import reset_runtime_state
from lib_runtime.runtime_factory import create_runtime
from lib_storeadapters.memory_adapters import InMemoryJsonAdapter, InMemorySnapshotAdapter
from lib_storeadapters.ports import SnapshotStoreAdapter, WorkspacePersistenceAdapter
from lib_utils.ids import bind_id_provider, reset_id_provider


def _symbols_from_trace(trace: TraceTranscript) -> SymbolTable:
    table = SymbolTable()
    raw_symbols = trace.metadata.get("symbols", {})
    for handle, value in raw_symbols.items():
        table.declare(
            Symbol(
                handle=handle,
                kind=SymbolKind(value["kind"]),
                name=value["name"],
                owner=value.get("owner"),
            )
        )
    return table


def _require_success(result: Any, operation: TraceOperation) -> dict[str, Any]:
    if not result.success:
        raise RuntimeError(
            f"Operation {operation.step} ({operation.kind}) failed: {result.error}"
        )
    return result.data or {}


def _actual_oracle_value(observation: Any, target: dict[str, Any]) -> Any:
    for cube in observation.state["cubes"]:
        if cube["name"] != target["cube"]:
            continue
        for entry in cube["values"]:
            if entry["address"] == target["address"]:
                return entry["value"]
    return None


def create_trace_runtime(
    trace: TraceTranscript,
    *,
    workspace: Workspace | None = None,
    workspace_adapter: WorkspacePersistenceAdapter | None = None,
    snapshot_adapter: SnapshotStoreAdapter | None = None,
):
    """Construct a deterministic runtime for a trace without executing it."""
    master_seed = trace.metadata.get("master_seed", 0)
    providers = providers_for_seed(master_seed)
    if workspace is None:
        token = bind_id_provider(providers.ids)
        try:
            workspace = Workspace.create(f"DST seed {master_seed}")
        finally:
            reset_id_provider(token)
    return create_runtime(
        workspace=workspace,
        bus=MessageBus(),
        providers=providers,
        workspace_adapter=workspace_adapter or InMemoryJsonAdapter(),
        snapshot_adapter=snapshot_adapter or InMemorySnapshotAdapter(),
        enable_solver=False,
        persistence_mode="manual",
    )


def execute_operations(runtime: Any, trace: TraceTranscript) -> SymbolTable:
    """Replay recorded operations in an existing runtime."""
    symbols = _symbols_from_trace(trace)
    for operation in trace.operations:
        args = operation.args
        if operation.kind == "configure_serial":
            result = runtime.session.execute(
                "set_multithread_recompute", enabled=bool(args["enabled"])
            )
        elif operation.kind == "create_dimension":
            result = runtime.session.execute(
                "create_dimension", name=args["name"], dim_type=args["dim_type"]
            )
            symbols.bind(
                operation.target,
                _require_success(result, operation)["id"],
                SymbolKind.DIMENSION,
            )
            continue
        elif operation.kind == "create_dimension_item":
            result = runtime.session.execute(
                "create_dimension_item",
                dim_id=symbols.runtime_id(args["dimension"], SymbolKind.DIMENSION),
                name=args["name"],
            )
            symbols.bind(
                operation.target,
                _require_success(result, operation)["id"],
                SymbolKind.ITEM,
            )
            continue
        elif operation.kind == "create_cube":
            result = runtime.session.execute(
                "create_cube",
                name=args["name"],
                dimension_ids=[
                    symbols.runtime_id(handle, SymbolKind.DIMENSION)
                    for handle in args["dimensions"]
                ],
            )
            symbols.bind(
                operation.target,
                _require_success(result, operation)["id"],
                SymbolKind.CUBE,
            )
            continue
        elif operation.kind == "set_cell_hardvalue":
            result = runtime.session.execute(
                "set_cell_hardvalue_by_addr",
                cube_id=symbols.runtime_id(operation.target, SymbolKind.CUBE),
                addr=[
                    "@.value",
                    *[
                        symbols.runtime_id(handle, SymbolKind.ITEM)
                        for handle in args["address"]
                    ],
                ],
                value=args["value"],
            )
        elif operation.kind == "set_rule":
            result = runtime.session.execute(
                "set_rule",
                cube_id=symbols.runtime_id(operation.target, SymbolKind.CUBE),
                targets=args["targets"],
                expression=args["expression"],
                is_anchored=bool(args["is_anchored"]),
            )
        elif operation.kind == "run_recalculation":
            result = runtime.session.execute(
                "run_recalculation", scope=args.get("scope", "all")
            )
        else:
            raise ValueError(f"Unsupported DST operation: {operation.kind}")
        _require_success(result, operation)
    return symbols


def report_runtime(runtime: Any, trace: TraceTranscript) -> ExecutionReport:
    """Build the canonical Phase 1 report for an executed runtime."""
    observation = observe_workspace(runtime.workspace, trace.observe)
    oracle = trace.metadata["oracle"]
    actual = _actual_oracle_value(observation, oracle["target"])
    expected = oracle["expected"]
    return ExecutionReport(
        trace_sha256=trace.sha256(),
        observation=observation,
        oracle_expected=expected,
        oracle_actual=actual,
        oracle_passed=matches(expected, actual),
    )


def execute_trace(trace: TraceTranscript) -> ExecutionReport:
    runtime = create_trace_runtime(trace)
    try:
        execute_operations(runtime, trace)
        return report_runtime(runtime, trace)
    finally:
        reset_runtime_state(runtime)
