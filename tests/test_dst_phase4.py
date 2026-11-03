from __future__ import annotations

import contextlib
import io
from dataclasses import replace
from pathlib import Path

import pytest

from lib_dst.corpus import CorpusStore
from lib_dst.evolution import EvolutionCoordinator
from lib_dst.executor import execute_trace
from lib_dst.failures import capture_failure
from lib_dst.faults import FaultDecision, with_faults
from lib_dst.generator import generate_trace
from lib_dst.mutation import (
    MutationDecision,
    apply_mutation_decision,
    available_mutation_kinds,
    mutate_trace,
    mutation_decisions_from_trace,
)
from lib_dst.runner import run_candidate_in_worker
from lib_dst.schemas import TraceTranscript
from lib_dst.scheduling import generate_scheduler_scenario, with_scheduler
from lib_dst.validation import validate_trace


def test_clean_worker_branch_coverage_is_normalized_and_repeatable() -> None:
    trace = generate_trace(0)

    first = run_candidate_in_worker(trace, python_hash_seed="404")
    second = run_candidate_in_worker(trace, python_hash_seed="404")

    assert first == second
    arcs = first["coverage"]["arcs"]
    assert len(arcs) > 100
    assert arcs == sorted(set(arcs))
    assert any(arc.startswith("lib_openm/") for arc in arcs)
    assert any(arc.startswith("lib_command/") for arc in arcs)
    assert all(not arc.startswith(("lib_dst/", "lib_gui/", "tests/")) for arc in arcs)
    assert all(arc.rsplit(":", 1)[-1] in {"L", "R", "B"} for arc in arcs)
    assert not first["outcome"]["failed"]


@pytest.mark.parametrize(
    ("kind", "trace"),
    (
        ("hardvalue", generate_trace(0)),
        ("ast_literal", generate_trace(0)),
        ("ast_operator", generate_trace(0)),
        ("ast_reference", generate_trace(0)),
        ("recalculation", generate_trace(0)),
        (
            "scheduler_delay",
            with_scheduler(generate_trace(1), generate_scheduler_scenario(1, "delay")),
        ),
        (
            "fault",
            with_faults(
                generate_trace(2),
                [FaultDecision("persistence", "load", 1, "raise")],
            ),
        ),
    ),
)
def test_mutation_is_valid_recorded_and_exactly_replayable(kind, trace) -> None:
    mutated, decision = mutate_trace(trace, 1234, 7, kind=kind)
    replayed = apply_mutation_decision(trace, decision)
    restored = TraceTranscript.from_json(mutated.to_json())

    validate_trace(mutated)
    assert replayed.to_json() == mutated.to_json()
    assert restored.to_json() == mutated.to_json()
    assert mutation_decisions_from_trace(restored) == (decision,)
    assert decision.parent_sha256 == trace.sha256()
    assert f"mutation:{kind}" in mutated.metadata["features"]

    if kind in {
        "hardvalue",
        "ast_literal",
        "ast_operator",
        "ast_reference",
        "recalculation",
    }:
        with contextlib.redirect_stdout(io.StringIO()):
            assert execute_trace(mutated).oracle_passed


def test_mutation_generation_is_deterministic_and_domain_isolated() -> None:
    trace = generate_trace(8)

    first, first_decision = mutate_trace(trace, 99, 3)
    second, second_decision = mutate_trace(trace, 99, 3)
    later, later_decision = mutate_trace(trace, 99, 4)

    assert first.to_json() == second.to_json()
    assert first_decision == second_decision
    assert first_decision.mutation_seed != later_decision.mutation_seed
    assert later.sha256() != trace.sha256()


def test_mutation_rejects_a_different_parent() -> None:
    trace = generate_trace(0)
    _, decision = mutate_trace(trace, 5, 0, kind="hardvalue")

    with pytest.raises(ValueError, match="parent"):
        apply_mutation_decision(generate_trace(1), decision)


def test_corpus_is_content_addressed_atomic_and_coordinator_owned(tmp_path) -> None:
    store = CorpusStore(tmp_path / "corpus")
    trace = generate_trace(0)

    retained = store.add_candidate(trace, ("arc:a",), generation=0)

    assert retained.retained
    assert (store.traces_dir / f"{trace.sha256()}.json").is_file()
    assert store.load_trace(trace.sha256()).to_json() == trace.to_json()
    assert store.index_path.read_text(encoding="utf-8") == store.canonical_index_json()
    assert not list(store.root.rglob("*.tmp"))

    duplicate_semantics = replace(
        trace,
        metadata={**trace.metadata, "non_semantic_probe": True},
    )
    rejected = store.add_candidate(
        duplicate_semantics, ("arc:a",), generation=1
    )
    assert not rejected.retained

    store._owner_pid = -1
    with pytest.raises(RuntimeError, match="coordinator"):
        store.add_candidate(generate_trace(1), ("arc:b",), generation=2)


def test_corpus_retains_new_coverage_feature_and_failure(tmp_path) -> None:
    store = CorpusStore(tmp_path / "corpus")
    base = generate_trace(0)
    feature_trace, _ = mutate_trace(base, 10, 0, kind="hardvalue")
    failure_trace = with_faults(
        generate_trace(2),
        [FaultDecision("persistence", "save", 1, "raise")],
    )

    first = store.add_candidate(base, ("arc:a",), generation=0)
    coverage = store.add_candidate(generate_trace(1), ("arc:b",), generation=1)
    feature = store.add_candidate(feature_trace, ("arc:a",), generation=2)
    failure = store.add_candidate(
        failure_trace,
        ("arc:a",),
        generation=3,
        failure_signature="persistence|persistence.save|persistence.save.injected",
    )

    assert first.retained
    assert coverage.new_coverage == ("arc:b",)
    assert any(value == "mutation:hardvalue" for value in feature.new_features)
    assert failure.new_failure


def test_selection_weights_include_rarity_age_and_size_and_are_deterministic(tmp_path) -> None:
    first_store = CorpusStore(tmp_path / "first")
    second_store = CorpusStore(tmp_path / "second")
    traces = (generate_trace(0), generate_trace(1), generate_trace(2))
    coverages = (("common", "rare"), ("common", "second"), ("common", "third"))
    for store in (first_store, second_store):
        for generation, (trace, coverage) in enumerate(zip(traces, coverages)):
            store.add_candidate(trace, coverage, generation=generation)

    weights = first_store.selection_weights(10)
    assert all(weight > 0 for weight in weights.values())
    assert len(set(weights.values())) > 1

    first, first_weights = first_store.select(77, 10)
    second, second_weights = second_store.select(77, 10)
    assert first.trace_sha256 == second.trace_sha256
    assert first_weights == second_weights
    assert first_store.canonical_index_json() == second_store.canonical_index_json()


def test_evolution_is_deterministic_across_clean_corpus_directories(tmp_path) -> None:
    first = EvolutionCoordinator(tmp_path / "first", 2026, python_hash_seed="31337")
    second = EvolutionCoordinator(tmp_path / "second", 2026, python_hash_seed="31337")

    first_report = first.run(initial_seeds=(0, 1), iterations=4)
    second_report = second.run(initial_seeds=(0, 1), iterations=4)

    assert first_report.to_json() == second_report.to_json()
    assert first_report.sha256() == second_report.sha256()
    assert first.store.canonical_index_json() == second.store.canonical_index_json()
    assert len(first_report.mutations) == 4
    assert first_report.corpus


def test_evolution_discovers_and_minimizes_seeded_persistence_failure(tmp_path) -> None:
    failing = with_faults(
        generate_trace(7),
        [FaultDecision("persistence", "save", 1, "raise")],
    )
    coordinator = EvolutionCoordinator(tmp_path / "corpus", 11)

    with contextlib.redirect_stdout(io.StringIO()):
        report = coordinator.run(
            initial_seeds=(), initial_traces=(failing,), iterations=0
        )

    assert len(report.failures) == 1
    signature, digest = report.failures[0]
    assert signature == "persistence|persistence.save|persistence.save.injected"
    path = next(coordinator.store.failures_dir.rglob(f"{digest}.json"))
    minimized = TraceTranscript.from_json(path.read_text(encoding="utf-8"))
    assert len(minimized.to_json()) < len(failing.to_json())
    assert capture_failure(minimized).signature == capture_failure(failing).signature
