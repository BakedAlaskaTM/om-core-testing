"""Coverage-guided deterministic corpus evolution."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from lib_dst.corpus import CorpusStore, RetentionDecision
from lib_dst.coverage import BranchCoverage, CoverageSnapshot
from lib_dst.failures import FailureOutcome, capture_failure
from lib_dst.generator import generate_trace
from lib_dst.mutation import MutationDecision, mutate_trace
from lib_dst.runner import run_candidate_in_worker
from lib_dst.schemas import TraceTranscript
from lib_dst.shrinker import shrink_failure


@dataclass(frozen=True)
class CandidateExecution:
    trace_sha256: str
    coverage: CoverageSnapshot
    outcome: FailureOutcome

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_sha256": self.trace_sha256,
            "coverage": self.coverage.to_dict(),
            "outcome": self.outcome.to_dict(),
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class EvolutionReport:
    master_seed: int | str
    initial_count: int
    iterations: int
    retained: int
    corpus: tuple[str, ...]
    mutations: tuple[dict[str, Any], ...]
    failures: tuple[tuple[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "master_seed": self.master_seed,
            "initial_count": self.initial_count,
            "iterations": self.iterations,
            "retained": self.retained,
            "corpus": list(self.corpus),
            "mutations": list(self.mutations),
            "failures": [
                {"signature": signature, "minimized_trace_sha256": digest}
                for signature, digest in self.failures
            ],
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


def execute_candidate(trace: TraceTranscript) -> CandidateExecution:
    """Execute one candidate with production-only branch monitoring."""

    with BranchCoverage() as monitor:
        outcome = capture_failure(trace)
    return CandidateExecution(trace.sha256(), monitor.snapshot, outcome)


class EvolutionCoordinator:
    def __init__(
        self,
        corpus_root: str | Path,
        master_seed: int | str,
        *,
        python_hash_seed: str = "1",
        minimize_failures: bool = True,
    ) -> None:
        self.store = CorpusStore(corpus_root)
        self.master_seed = master_seed
        self.python_hash_seed = python_hash_seed
        self.minimize_failures = minimize_failures
        self._mutations: list[MutationDecision] = []
        self._retained = 0

    def run(
        self,
        *,
        initial_seeds: Iterable[int] = (0,),
        initial_traces: Iterable[TraceTranscript] = (),
        iterations: int = 0,
    ) -> EvolutionReport:
        if iterations < 0:
            raise ValueError("Evolution iterations cannot be negative")
        seeds = tuple(initial_seeds)
        traces = tuple(initial_traces)
        generation = 0
        for trace in (*[generate_trace(seed) for seed in seeds], *traces):
            self._evaluate(trace, generation=generation, parent_sha256=None)
            generation += 1

        if not self.store.entries:
            raise RuntimeError("Evolution requires at least one retained initial trace")

        for mutation_index in range(iterations):
            selected, _weights = self.store.select(self.master_seed, generation)
            parent = self.store.load_trace(selected.trace_sha256)
            candidate, decision = mutate_trace(
                parent, self.master_seed, mutation_index
            )
            self._mutations.append(decision)
            self._evaluate(
                candidate,
                generation=generation,
                parent_sha256=selected.trace_sha256,
            )
            generation += 1

        return EvolutionReport(
            master_seed=self.master_seed,
            initial_count=len(seeds) + len(traces),
            iterations=iterations,
            retained=self._retained,
            corpus=tuple(sorted(self.store.entries)),
            mutations=tuple(decision.to_dict() for decision in self._mutations),
            failures=tuple(sorted(self.store.failures.items())),
        )

    def _evaluate(
        self,
        trace: TraceTranscript,
        *,
        generation: int,
        parent_sha256: str | None,
    ) -> RetentionDecision:
        evidence = run_candidate_in_worker(
            trace, python_hash_seed=self.python_hash_seed
        )
        outcome = evidence["outcome"]
        signature_value = outcome.get("signature")
        signature = _signature_key(signature_value) if signature_value else None
        decision = self.store.add_candidate(
            trace,
            evidence["coverage"]["arcs"],
            generation=generation,
            failure_signature=signature,
            parent_sha256=parent_sha256,
        )
        if decision.retained:
            self._retained += 1
        if decision.new_failure and signature is not None and self.minimize_failures:
            shrunk = shrink_failure(trace)
            self.store.store_minimized_failure(signature, shrunk.minimized)
        return decision


def _signature_key(value: dict[str, Any]) -> str:
    return "|".join(
        (str(value["category"]), str(value["stage"]), str(value["code"]))
    )
