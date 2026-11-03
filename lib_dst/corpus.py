"""Coordinator-owned, content-addressed corpus storage and selection."""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lib_dst.schemas import TraceTranscript
from lib_dst.seeding import SeedStreams


CORPUS_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class CorpusEntry:
    trace_sha256: str
    size_bytes: int
    coverage: tuple[str, ...]
    features: tuple[str, ...]
    failure_signature: str | None
    discovered_generation: int
    last_selected_generation: int
    selection_count: int = 0
    parent_sha256: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_sha256": self.trace_sha256,
            "size_bytes": self.size_bytes,
            "coverage": list(self.coverage),
            "features": list(self.features),
            "failure_signature": self.failure_signature,
            "discovered_generation": self.discovered_generation,
            "last_selected_generation": self.last_selected_generation,
            "selection_count": self.selection_count,
            "parent_sha256": self.parent_sha256,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "CorpusEntry":
        return cls(
            trace_sha256=str(value["trace_sha256"]),
            size_bytes=int(value["size_bytes"]),
            coverage=tuple(str(item) for item in value.get("coverage", ())),
            features=tuple(str(item) for item in value.get("features", ())),
            failure_signature=(
                str(value["failure_signature"])
                if value.get("failure_signature") is not None
                else None
            ),
            discovered_generation=int(value["discovered_generation"]),
            last_selected_generation=int(value["last_selected_generation"]),
            selection_count=int(value.get("selection_count", 0)),
            parent_sha256=value.get("parent_sha256"),
        )


@dataclass(frozen=True)
class RetentionDecision:
    retained: bool
    trace_sha256: str
    new_coverage: tuple[str, ...] = ()
    new_features: tuple[str, ...] = ()
    new_failure: bool = False


class CorpusStore:
    """The sole writer for one deterministic evolution corpus."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.traces_dir = self.root / "traces"
        self.failures_dir = self.root / "failures"
        self.index_path = self.root / "index.json"
        self._owner_pid = os.getpid()
        self.traces_dir.mkdir(parents=True, exist_ok=True)
        self.failures_dir.mkdir(parents=True, exist_ok=True)
        self.entries: dict[str, CorpusEntry] = {}
        self.failures: dict[str, str] = {}
        if self.index_path.exists():
            self._load()

    def _require_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("Only the corpus coordinator process may write the corpus")

    def add_candidate(
        self,
        trace: TraceTranscript,
        coverage: tuple[str, ...] | list[str],
        *,
        generation: int,
        failure_signature: str | None = None,
        parent_sha256: str | None = None,
    ) -> RetentionDecision:
        self._require_owner()
        digest = trace.sha256()
        if digest in self.entries:
            return RetentionDecision(False, digest)

        arcs = tuple(sorted(set(str(arc) for arc in coverage)))
        features = semantic_features(trace)
        known_arcs = {arc for entry in self.entries.values() for arc in entry.coverage}
        known_features = {
            feature for entry in self.entries.values() for feature in entry.features
        }
        known_failures = {
            entry.failure_signature
            for entry in self.entries.values()
            if entry.failure_signature is not None
        }
        new_arcs = tuple(sorted(set(arcs) - known_arcs))
        new_features = tuple(sorted(set(features) - known_features))
        new_failure = (
            failure_signature is not None and failure_signature not in known_failures
        )
        retain = not self.entries or bool(new_arcs or new_features or new_failure)
        if not retain:
            return RetentionDecision(False, digest, new_arcs, new_features, new_failure)

        payload = trace.to_json()
        entry = CorpusEntry(
            trace_sha256=digest,
            size_bytes=len(payload.encode("utf-8")),
            coverage=arcs,
            features=features,
            failure_signature=failure_signature,
            discovered_generation=generation,
            last_selected_generation=generation,
            parent_sha256=parent_sha256,
        )
        self._write_text_once(self.traces_dir / f"{digest}.json", payload)
        self.entries[digest] = entry
        self._write_index()
        return RetentionDecision(True, digest, new_arcs, new_features, new_failure)

    def load_trace(self, trace_sha256: str) -> TraceTranscript:
        path = self.traces_dir / f"{trace_sha256}.json"
        trace = TraceTranscript.from_json(path.read_text(encoding="utf-8"))
        if trace.sha256() != trace_sha256:
            raise ValueError(f"Corpus trace digest mismatch: {trace_sha256}")
        return trace

    def selection_weights(self, generation: int) -> dict[str, int]:
        coverage_frequency: dict[str, int] = {}
        feature_frequency: dict[str, int] = {}
        for entry in self.entries.values():
            for arc in entry.coverage:
                coverage_frequency[arc] = coverage_frequency.get(arc, 0) + 1
            for feature in entry.features:
                feature_frequency[feature] = feature_frequency.get(feature, 0) + 1

        weights: dict[str, int] = {}
        for digest, entry in sorted(self.entries.items()):
            arc_rarity = sum(
                1_000 // coverage_frequency[arc] for arc in entry.coverage
            )
            feature_rarity = sum(
                1_000 // feature_frequency[feature] for feature in entry.features
            )
            age = max(1, generation - entry.last_selected_generation + 1)
            size_bias = max(1, 1_000_000 // max(1, entry.size_bytes))
            weights[digest] = max(
                1, (1 + arc_rarity + feature_rarity) * age * size_bias
            )
        return weights

    def select(
        self, master_seed: int | str, generation: int
    ) -> tuple[CorpusEntry, dict[str, int]]:
        self._require_owner()
        if not self.entries:
            raise ValueError("Cannot select from an empty corpus")
        weights = self.selection_weights(generation)
        total = sum(weights.values())
        rng = SeedStreams(master_seed).random(f"mutation.selection.{generation}")
        ticket = rng.randrange(total)
        selected_digest = ""
        for digest in sorted(weights):
            weight = weights[digest]
            if ticket < weight:
                selected_digest = digest
                break
            ticket -= weight
        entry = self.entries[selected_digest]
        updated = replace(
            entry,
            last_selected_generation=generation,
            selection_count=entry.selection_count + 1,
        )
        self.entries[selected_digest] = updated
        self._write_index()
        return updated, weights

    def store_minimized_failure(
        self, signature: str, trace: TraceTranscript
    ) -> str:
        self._require_owner()
        digest = trace.sha256()
        directory = self.failures_dir / _safe_signature(signature)
        directory.mkdir(parents=True, exist_ok=True)
        self._write_text_once(directory / f"{digest}.json", trace.to_json())
        self.failures[signature] = digest
        self._write_index()
        return digest

    def canonical_index_json(self) -> str:
        value = {
            "schema_version": CORPUS_SCHEMA_VERSION,
            "entries": {
                digest: self.entries[digest].to_dict()
                for digest in sorted(self.entries)
            },
            "failures": {
                signature: self.failures[signature]
                for signature in sorted(self.failures)
            },
        }
        return json.dumps(value, sort_keys=True, separators=(",", ":"))

    def _load(self) -> None:
        value = json.loads(self.index_path.read_text(encoding="utf-8"))
        if int(value.get("schema_version", 0)) != CORPUS_SCHEMA_VERSION:
            raise ValueError("Unsupported corpus index schema")
        self.entries = {
            digest: CorpusEntry.from_dict(entry)
            for digest, entry in value.get("entries", {}).items()
        }
        self.failures = {
            str(signature): str(digest)
            for signature, digest in value.get("failures", {}).items()
        }

    def _write_index(self) -> None:
        self._require_owner()
        self._atomic_write(self.index_path, self.canonical_index_json())

    def _write_text_once(self, path: Path, value: str) -> None:
        if path.exists():
            if path.read_text(encoding="utf-8") != value:
                raise ValueError(f"Content-addressed path collision: {path.name}")
            return
        self._atomic_write(path, value)

    @staticmethod
    def _atomic_write(path: Path, value: str) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        descriptor, temporary = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        try:
            with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(value)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def semantic_features(trace: TraceTranscript) -> tuple[str, ...]:
    features = {str(value) for value in trace.metadata.get("features", ())}
    features.update(f"operation:{operation.kind}" for operation in trace.operations)
    ast_value = trace.metadata.get("ast")
    if isinstance(ast_value, dict):
        for node in _walk_ast(ast_value):
            node_type = str(node.get("type"))
            features.add(f"ast:{node_type}")
            if "operator" in node:
                features.add(f"operator:{node['operator']}")
            if "function" in node:
                features.add(f"function:{node['function']}")
    expected = trace.metadata.get("oracle", {}).get("expected", {})
    features.add("oracle:error" if "error" in expected else "oracle:number")
    for fault in trace.metadata.get("faults", ()):
        features.add(
            f"fault:{fault.get('operation')}:{fault.get('outcome')}"
        )
    scheduler = trace.metadata.get("scheduler", {})
    for decision in scheduler.get("decisions", ()) if isinstance(scheduler, dict) else ():
        features.add(f"scheduler:{decision.get('mode')}")
    return tuple(sorted(features))


def _walk_ast(value: dict[str, Any]):
    yield value
    node_type = value.get("type")
    if node_type in {"binary", "comparison"}:
        yield from _walk_ast(value["left"])
        yield from _walk_ast(value["right"])
    elif node_type == "negate":
        yield from _walk_ast(value["operand"])
    elif node_type == "call":
        for argument in value["arguments"]:
            yield from _walk_ast(argument)


def _safe_signature(value: str) -> str:
    return "".join(character if character.isalnum() or character in ".-_" else "-" for character in value)
