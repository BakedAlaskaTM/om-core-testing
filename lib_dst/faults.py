"""Trace-recorded persistence fault contracts for Phase 2."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from lib_dst.schemas import TraceTranscript
from lib_openm.ports import WorkspacePersistenceAdapter


@dataclass(frozen=True)
class FaultDecision:
    layer: str
    operation: str
    occurrence: int
    outcome: str
    delivery: str = "single-call"
    expected_recovery: str = "command-failure"
    state_loss_allowed: bool = False

    def __post_init__(self) -> None:
        if self.layer != "persistence":
            raise ValueError("Phase 2 supports only persistence faults")
        if self.operation not in {"save", "load"}:
            raise ValueError(f"Unsupported persistence operation: {self.operation}")
        if self.occurrence < 1:
            raise ValueError("Fault occurrence must be positive")
        if self.outcome not in {"raise", "malformed_json"}:
            raise ValueError(f"Unsupported persistence outcome: {self.outcome}")
        if self.outcome == "malformed_json" and self.operation != "load":
            raise ValueError("malformed_json is valid only for load")

    @property
    def code(self) -> str:
        suffix = "malformed-json" if self.outcome == "malformed_json" else "injected"
        return f"persistence.{self.operation}.{suffix}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "layer": self.layer,
            "operation": self.operation,
            "occurrence": self.occurrence,
            "outcome": self.outcome,
            "delivery": self.delivery,
            "expected_recovery": self.expected_recovery,
            "state_loss_allowed": self.state_loss_allowed,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "FaultDecision":
        return cls(
            layer=str(value["layer"]),
            operation=str(value["operation"]),
            occurrence=int(value["occurrence"]),
            outcome=str(value["outcome"]),
            delivery=str(value.get("delivery", "single-call")),
            expected_recovery=str(value.get("expected_recovery", "command-failure")),
            state_loss_allowed=bool(value.get("state_loss_allowed", False)),
        )


class PersistenceFaultError(RuntimeError):
    def __init__(self, decision: FaultDecision) -> None:
        self.decision = decision
        self.stage = f"persistence.{decision.operation}"
        self.code = decision.code
        super().__init__(
            f"DSTFAULT[{self.code}] occurrence={decision.occurrence} "
            f"recovery={decision.expected_recovery}"
        )


def decisions_from_trace(trace: TraceTranscript) -> tuple[FaultDecision, ...]:
    return tuple(
        FaultDecision.from_dict(value)
        for value in trace.metadata.get("faults", ())
    )


def with_faults(
    trace: TraceTranscript, decisions: tuple[FaultDecision, ...] | list[FaultDecision]
) -> TraceTranscript:
    metadata = dict(trace.metadata)
    metadata["faults"] = [decision.to_dict() for decision in decisions]
    return replace(trace, metadata=metadata)


class FaultInjectingWorkspaceAdapter(WorkspacePersistenceAdapter):
    """Decorator that consumes only fault decisions recorded in a trace."""

    def __init__(
        self,
        delegate: WorkspacePersistenceAdapter,
        decisions: tuple[FaultDecision, ...],
    ) -> None:
        self.delegate = delegate
        self.decisions = decisions
        self._counts = {"save": 0, "load": 0}

    def _decision(self, operation: str) -> FaultDecision | None:
        self._counts[operation] += 1
        occurrence = self._counts[operation]
        return next(
            (
                decision
                for decision in self.decisions
                if decision.operation == operation
                and decision.occurrence == occurrence
            ),
            None,
        )

    def save_workspace(self, path: str | Path, workspace: Any) -> None:
        decision = self._decision("save")
        if decision is not None:
            raise PersistenceFaultError(decision)
        self.delegate.save_workspace(path, workspace)

    def _before_load(self, path: str | Path) -> None:
        decision = self._decision("load")
        if decision is None:
            return
        if decision.outcome == "raise":
            raise PersistenceFaultError(decision)
        replace_payload = getattr(self.delegate, "replace_payload", None)
        if replace_payload is None:
            raise TypeError("malformed_json requires a mutable JSON adapter")
        replace_payload(path, "{malformed-json")
        try:
            self.delegate.load_workspace(path)
        except Exception as exc:
            raise PersistenceFaultError(decision) from exc
        raise AssertionError("Malformed JSON unexpectedly loaded successfully")

    def load_workspace(self, path: str | Path) -> Any:
        self._before_load(path)
        return self.delegate.load_workspace(path)

    def load_workspace_profiled(self, path: str | Path) -> tuple[Any, dict]:
        self._before_load(path)
        return self.delegate.load_workspace_profiled(path)
