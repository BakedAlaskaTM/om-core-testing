"""Versioned Phase 0 schemas for DST environments, traces, and observations."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import sys
from dataclasses import dataclass, field
from typing import Any


TRACE_SCHEMA_VERSION = 1
OBSERVATION_SCHEMA_VERSION = 1


@dataclass(frozen=True)
class EnvironmentDescriptor:
    python: str
    implementation: str
    platform: str
    python_hash_seed: str
    timezone: str
    engine_mode: str = "python"
    recalculation: str = "serial"

    @classmethod
    def current(cls) -> "EnvironmentDescriptor":
        return cls(
            python=platform.python_version(),
            implementation=platform.python_implementation(),
            platform=sys.platform,
            python_hash_seed=os.environ.get("PYTHONHASHSEED", "random"),
            timezone=os.environ.get("TZ", "system"),
        )

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "EnvironmentDescriptor":
        return cls(**value)

    def to_dict(self) -> dict[str, Any]:
        return {
            "python": self.python,
            "implementation": self.implementation,
            "platform": self.platform,
            "python_hash_seed": self.python_hash_seed,
            "timezone": self.timezone,
            "engine_mode": self.engine_mode,
            "recalculation": self.recalculation,
        }


@dataclass(frozen=True)
class ObservationSpec:
    dimensions: tuple[str, ...] = ()
    cubes: tuple[str, ...] = ()

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "ObservationSpec":
        return cls(
            dimensions=tuple(value.get("dimensions", ())),
            cubes=tuple(value.get("cubes", ())),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "dimensions": list(self.dimensions),
            "cubes": list(self.cubes),
        }


@dataclass(frozen=True)
class TraceOperation:
    step: int
    kind: str
    target: str
    args: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TraceOperation":
        return cls(
            step=int(value["step"]),
            kind=str(value["kind"]),
            target=str(value["target"]),
            args=dict(value.get("args", {})),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "step": self.step,
            "kind": self.kind,
            "target": self.target,
            "args": self.args,
        }


@dataclass(frozen=True)
class TraceTranscript:
    environment: EnvironmentDescriptor
    operations: tuple[TraceOperation, ...]
    observe: ObservationSpec
    schema_version: int = TRACE_SCHEMA_VERSION
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.schema_version != TRACE_SCHEMA_VERSION:
            raise ValueError(f"Unsupported trace schema: {self.schema_version}")
        steps = [operation.step for operation in self.operations]
        if steps != list(range(len(steps))):
            raise ValueError("Trace operation steps must be contiguous and zero-based")

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "TraceTranscript":
        return cls(
            schema_version=int(value["schema_version"]),
            environment=EnvironmentDescriptor.from_dict(value["environment"]),
            operations=tuple(
                TraceOperation.from_dict(operation)
                for operation in value.get("operations", ())
            ),
            observe=ObservationSpec.from_dict(value.get("observe", {})),
            metadata=dict(value.get("metadata", {})),
        )

    @classmethod
    def from_json(cls, value: str) -> "TraceTranscript":
        return cls.from_dict(json.loads(value))

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "environment": self.environment.to_dict(),
            "operations": [operation.to_dict() for operation in self.operations],
            "observe": self.observe.to_dict(),
            "metadata": self.metadata,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))

    def sha256(self) -> str:
        return hashlib.sha256(self.to_json().encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class CanonicalObservation:
    state: dict[str, Any]
    schema_version: int = OBSERVATION_SCHEMA_VERSION

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "state": self.state}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class ExecutionReport:
    trace_sha256: str
    observation: CanonicalObservation
    oracle_expected: dict[str, Any]
    oracle_actual: Any
    oracle_passed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_sha256": self.trace_sha256,
            "observation": self.observation.to_dict(),
            "oracle": {
                "expected": self.oracle_expected,
                "actual": self.oracle_actual,
                "passed": self.oracle_passed,
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))
