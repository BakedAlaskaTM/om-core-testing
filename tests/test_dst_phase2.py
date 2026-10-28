from __future__ import annotations

from dataclasses import replace

import pytest

from lib_dst.failures import FailureSignature, capture_failure
from lib_dst.faults import (
    FaultDecision,
    FaultInjectingWorkspaceAdapter,
    PersistenceFaultError,
    decisions_from_trace,
    with_faults,
)
from lib_dst.generator import generate_trace
from lib_dst.invariants import execute_convergence
from lib_dst.runner import run_failure_in_worker
from lib_dst.schemas import TraceTranscript
from lib_dst.shrinker import shrink_failure
from lib_dst.validation import TraceValidationError, validate_trace
from lib_openm.model import Workspace
from lib_runtime.lifecycle import reset_runtime_state
from lib_storeadapters.memory_adapters import InMemoryJsonAdapter


@pytest.fixture(autouse=True)
def clean_runtime_state():
    reset_runtime_state()
    yield
    reset_runtime_state()


@pytest.mark.parametrize("seed", (0, 1, 3, 7, 19, 31))
def test_cache_snapshot_and_fresh_engine_converge(seed: int) -> None:
    report = execute_convergence(generate_trace(seed))

    assert report.passed, report.to_json()
    assert [check.name for check in report.checks] == [
        "cache-first-recalculation",
        "cache-second-recalculation",
        "snapshot-roundtrip",
        "fresh-engine-convergence",
    ]
    assert all(check.expected_sha256 == check.actual_sha256 for check in report.checks)


def test_canonical_observation_includes_logical_rule_precedence() -> None:
    trace = generate_trace(5)
    report = execute_convergence(trace)
    expression = next(
        operation.args["expression"]
        for operation in trace.operations
        if operation.kind == "set_rule"
    )

    rules = report.execution.observation.state["rules"]
    assert rules == [
        {
            "cube": "Calc",
            "targets": [["Line", "Result"]],
            "expression": expression,
            "is_anchored": True,
            "precedence": 0,
        }
    ]


def test_fault_decisions_round_trip_in_trace_metadata() -> None:
    decision = FaultDecision("persistence", "load", 1, "malformed_json")
    trace = with_faults(generate_trace(2), [decision])

    restored = TraceTranscript.from_json(trace.to_json())

    assert decisions_from_trace(restored) == (decision,)
    validate_trace(restored)


def test_passing_convergence_report_repeats_across_clean_workers() -> None:
    trace = generate_trace(11)

    first = run_failure_in_worker(trace, python_hash_seed="8801")
    second = run_failure_in_worker(trace, python_hash_seed="8801")

    assert first == second
    assert first["failed"] is False
    assert first["report"]["passed"] is True


def test_nth_persistence_fault_triggers_only_on_recorded_occurrence() -> None:
    delegate = InMemoryJsonAdapter()
    adapter = FaultInjectingWorkspaceAdapter(
        delegate,
        (FaultDecision("persistence", "save", 2, "raise"),),
    )
    workspace = Workspace.create("Nth fault")

    adapter.save_workspace("model", workspace)
    with pytest.raises(PersistenceFaultError) as caught:
        adapter.save_workspace("model", workspace)

    assert caught.value.code == "persistence.save.injected"


@pytest.mark.parametrize(
    ("decision", "expected"),
    (
        (
            FaultDecision("persistence", "save", 1, "raise"),
            FailureSignature(
                "persistence", "persistence.save", "persistence.save.injected"
            ),
        ),
        (
            FaultDecision("persistence", "load", 1, "raise"),
            FailureSignature(
                "persistence", "persistence.load", "persistence.load.injected"
            ),
        ),
        (
            FaultDecision("persistence", "load", 1, "malformed_json"),
            FailureSignature(
                "persistence",
                "persistence.load",
                "persistence.load.malformed-json",
            ),
        ),
    ),
)
def test_each_persistence_failure_shrinks_and_reproduces_cleanly(
    decision: FaultDecision, expected: FailureSignature
) -> None:
    original = with_faults(generate_trace(7), [decision])

    outcome = capture_failure(original)
    shrunk = shrink_failure(original)
    restored = TraceTranscript.from_json(shrunk.minimized.to_json())
    worker = run_failure_in_worker(restored, python_hash_seed="202602")

    assert outcome.signature == expected
    assert shrunk.signature == expected
    assert len(shrunk.minimized.to_json()) < len(original.to_json())
    assert capture_failure(restored).signature == expected
    assert worker["signature"] == expected.to_dict()


def test_validation_rejects_ast_reference_after_required_value_is_removed() -> None:
    trace = generate_trace(7)
    operations = tuple(
        operation
        for operation in trace.operations
        if not (
            operation.kind == "set_cell_hardvalue"
            and operation.args["address"] == ["item:line/input-a"]
        )
    )
    broken = replace(
        trace,
        operations=tuple(
            replace(operation, step=index)
            for index, operation in enumerate(operations)
        ),
    )

    with pytest.raises(TraceValidationError, match="without hard values"):
        validate_trace(broken)


def test_validation_normalizes_parser_errors() -> None:
    trace = generate_trace(3)
    operations = []
    for operation in trace.operations:
        if operation.kind == "set_rule":
            args = dict(operation.args)
            args["expression"] = "("
            operation = replace(operation, args=args)
        operations.append(operation)
    broken = replace(trace, operations=tuple(operations))

    with pytest.raises(TraceValidationError):
        validate_trace(broken)
