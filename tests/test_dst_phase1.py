from __future__ import annotations

from fractions import Fraction

import pytest

from lib_dst.ast import Binary, Call, Comparison, Number, Reference, from_dict, render, to_dict
from lib_dst.executor import execute_trace
from lib_dst.generator import generate_trace
from lib_dst.oracle import OracleError, evaluate
from lib_dst.runner import compare_seed_range
from lib_dst.seeding import SeedStreams, providers_for_seed
from lib_openm.deps import DependencyGraph
from lib_runtime.lifecycle import reset_runtime_state


@pytest.fixture(autouse=True)
def clean_runtime_state():
    reset_runtime_state()
    yield
    reset_runtime_state()


def test_key_derived_streams_are_stable_and_domain_separated() -> None:
    streams = SeedStreams(123)

    assert streams.seed("schema") == SeedStreams(123).seed("schema")
    assert streams.seed("schema") != streams.seed("values")
    assert streams.random("schema").random() == streams.random("schema").random()


def test_deterministic_runtime_providers_replay() -> None:
    first = providers_for_seed(77)
    second = providers_for_seed(77)

    assert first.ids.new_id("item") == second.ids.new_id("item")
    assert first.clock.now_utc() == second.clock.now_utc()
    assert first.random.random() == second.random.random()


def test_typed_ast_round_trips_and_oracle_handles_division_by_zero() -> None:
    ref = Reference("input", "Calc", "Line", "InputA")
    expression = Binary("/", ref, Number(0))

    assert from_dict(to_dict(expression)) == expression
    assert render(expression) == "(Calc::[Line.InputA] / 0)"
    assert evaluate(expression, {"input": 9}) == OracleError("#DIV/0!")


def test_independent_oracle_uses_exact_arithmetic() -> None:
    expression = Binary("/", Number(1), Number(10))

    assert evaluate(expression, {}) == Fraction(1, 10)


def test_oracle_models_typed_functions_and_comparisons() -> None:
    expression = Call(
        "IF",
        (
            Comparison(">", Number(2), Number(1)),
            Call("AVG", (Number(1), Number(2), Number(3))),
            Number(99),
        ),
    )

    assert from_dict(to_dict(expression)) == expression
    assert evaluate(expression, {}) == Fraction(2)

    with pytest.raises(TypeError, match="numeric operands"):
        Binary("+", Comparison("==", Number(1), Number(1)), Number(2))


def test_dependency_graph_exposes_stable_key_order() -> None:
    graph = DependencyGraph()
    graph.add_edge("z", "m")
    graph.add_edge("a", "m")
    graph.mark_dirty("z")
    graph.mark_dirty("a")

    assert graph.precedents_of("m") == ["a", "z"]
    assert graph.dirty_keys() == ["a", "z"]
    assert graph.pop_dirty() == "a"


@pytest.mark.parametrize("seed", range(32))
def test_generated_trace_exactly_replays_against_oracle(seed: int) -> None:
    trace = generate_trace(seed)

    assert generate_trace(seed).to_json() == trace.to_json()
    report = execute_trace(trace)

    assert report.trace_sha256 == trace.sha256()
    assert report.oracle_passed, report.to_json()


def test_worker_process_batch_repeats_exactly() -> None:
    result = compare_seed_range(1000, 8, python_hash_seed="31337")

    assert result["count"] == 8
    assert result["oracle_failures"] == 0
