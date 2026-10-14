"""Clean-process proof for the Phase 0 DST composition seams."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from lib_command.core.message_bus import MessageBus
from lib_dst.observation import observe_workspace
from lib_dst.schemas import (
    CanonicalObservation,
    EnvironmentDescriptor,
    ObservationSpec,
    TraceOperation,
    TraceTranscript,
)
from lib_openm.model import Workspace
from lib_runtime.lifecycle import reset_runtime_state
from lib_runtime.providers import RuntimeProviders
from lib_runtime.runtime_factory import create_runtime
from lib_storeadapters.memory_adapters import (
    InMemoryJsonAdapter,
    InMemorySnapshotAdapter,
)


def hand_authored_trace() -> TraceTranscript:
    return TraceTranscript(
        environment=EnvironmentDescriptor.current(),
        operations=(
            TraceOperation(
                0,
                "create_dimension",
                "dimension:region",
                {"name": "Region", "dim_type": "set"},
            ),
            TraceOperation(
                1,
                "create_dimension_item",
                "item:region/nz",
                {"dimension": "dimension:region", "name": "NZ"},
            ),
            TraceOperation(
                2,
                "create_cube",
                "cube:sales",
                {"name": "Sales", "dimensions": ["dimension:region"]},
            ),
            TraceOperation(
                3,
                "set_cell_hardvalue",
                "cube:sales",
                {"address": ["item:region/nz"], "value": 42},
            ),
        ),
        observe=ObservationSpec(dimensions=("Region",), cubes=("Sales",)),
        metadata={"purpose": "phase-0-clean-process-proof"},
    )


def _require_success(result: Any, operation: TraceOperation) -> dict[str, Any]:
    if not result.success:
        raise RuntimeError(
            f"Phase 0 operation {operation.step} ({operation.kind}) failed: {result.error}"
        )
    return result.data or {}


def run_trace(trace: TraceTranscript) -> CanonicalObservation:
    runtime = create_runtime(
        workspace=Workspace.create("DST Phase 0"),
        bus=MessageBus(),
        providers=RuntimeProviders.system(),
        workspace_adapter=InMemoryJsonAdapter(),
        snapshot_adapter=InMemorySnapshotAdapter(),
        enable_solver=False,
        persistence_mode="manual",
    )
    symbols: dict[str, str] = {}
    try:
        for operation in trace.operations:
            args = operation.args
            if operation.kind == "create_dimension":
                result = runtime.session.execute(
                    "create_dimension",
                    name=args["name"],
                    dim_type=args["dim_type"],
                )
                symbols[operation.target] = _require_success(result, operation)["id"]
            elif operation.kind == "create_dimension_item":
                result = runtime.session.execute(
                    "create_dimension_item",
                    dim_id=symbols[args["dimension"]],
                    name=args["name"],
                )
                symbols[operation.target] = _require_success(result, operation)["id"]
            elif operation.kind == "create_cube":
                result = runtime.session.execute(
                    "create_cube",
                    name=args["name"],
                    dimension_ids=[symbols[handle] for handle in args["dimensions"]],
                )
                symbols[operation.target] = _require_success(result, operation)["id"]
            elif operation.kind == "set_cell_hardvalue":
                result = runtime.session.execute(
                    "set_cell_hardvalue_by_addr",
                    cube_id=symbols[operation.target],
                    addr=["@.value", *[symbols[handle] for handle in args["address"]]],
                    value=args["value"],
                )
                _require_success(result, operation)
            else:
                raise ValueError(f"Unsupported Phase 0 operation: {operation.kind}")
        return observe_workspace(runtime.workspace, trace.observe)
    finally:
        reset_runtime_state(runtime)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--trace", type=Path)
    args = parser.parse_args()
    trace = (
        TraceTranscript.from_json(args.trace.read_text(encoding="utf-8"))
        if args.trace
        else hand_authored_trace()
    )
    print(run_trace(trace).to_json())


if __name__ == "__main__":
    main()
