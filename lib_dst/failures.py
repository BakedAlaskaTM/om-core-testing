"""Normalized failure capture for deterministic replay and shrinking."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from lib_dst.faults import PersistenceFaultError
from lib_dst.invariants import Phase2StageError, execute_convergence
from lib_dst.schemas import ConvergenceReport, TraceTranscript
from lib_dst.validation import TraceValidationError, validate_trace


@dataclass(frozen=True)
class FailureSignature:
    category: str
    stage: str
    code: str

    def to_dict(self) -> dict[str, str]:
        return {"category": self.category, "stage": self.stage, "code": self.code}

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True)
class FailureOutcome:
    trace_sha256: str
    signature: FailureSignature | None
    report: ConvergenceReport | None = None

    @property
    def failed(self) -> bool:
        return self.signature is not None

    def to_dict(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "trace_sha256": self.trace_sha256,
            "failed": self.failed,
            "signature": self.signature.to_dict() if self.signature else None,
        }
        if self.report is not None:
            value["report"] = self.report.to_dict()
        return value

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def capture_failure(trace: TraceTranscript) -> FailureOutcome:
    try:
        validate_trace(trace)
    except TraceValidationError as exc:
        return FailureOutcome(
            trace.sha256(),
            FailureSignature("validation", "trace.validation", type(exc).__name__),
        )

    try:
        report = execute_convergence(trace)
    except PersistenceFaultError as exc:
        return FailureOutcome(
            trace.sha256(),
            FailureSignature("persistence", exc.stage, exc.code),
        )
    except Phase2StageError as exc:
        category = "persistence" if exc.stage.startswith("persistence.") else "runtime"
        return FailureOutcome(
            trace.sha256(),
            FailureSignature(category, exc.stage, exc.code),
        )
    except Exception as exc:
        return FailureOutcome(
            trace.sha256(),
            FailureSignature("unexpected", "runtime.unhandled", type(exc).__name__),
        )

    if not report.execution.oracle_passed:
        return FailureOutcome(
            trace.sha256(),
            FailureSignature("invariant", "oracle", "value-mismatch"),
            report,
        )
    failed_check = next((check for check in report.checks if not check.passed), None)
    if failed_check is not None:
        return FailureOutcome(
            trace.sha256(),
            FailureSignature("invariant", failed_check.name, "observation-mismatch"),
            report,
        )
    return FailureOutcome(trace.sha256(), None, report)
