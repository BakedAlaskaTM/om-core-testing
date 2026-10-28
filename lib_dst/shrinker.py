"""Dependency-aware deterministic shrinking for Phase 2 failure traces."""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Iterable

from lib_dst.ast import (
    Binary,
    Call,
    Negate,
    Number,
    NumericExpr,
    ValueType,
    from_dict,
    render,
    to_dict,
)
from lib_dst.failures import FailureSignature, capture_failure
from lib_dst.faults import decisions_from_trace, with_faults
from lib_dst.oracle import canonical, evaluate
from lib_dst.schemas import TraceOperation, TraceTranscript
from lib_dst.validation import TraceValidationError, validate_trace


@dataclass(frozen=True)
class ShrinkResult:
    original_sha256: str
    minimized: TraceTranscript
    signature: FailureSignature
    candidates_tested: int

    def to_dict(self) -> dict:
        return {
            "original_sha256": self.original_sha256,
            "minimized_sha256": self.minimized.sha256(),
            "signature": self.signature.to_dict(),
            "candidates_tested": self.candidates_tested,
            "trace": self.minimized.to_dict(),
        }


def shrink_failure(
    trace: TraceTranscript,
    signature: FailureSignature | None = None,
) -> ShrinkResult:
    initial = capture_failure(trace)
    target = signature or initial.signature
    if target is None:
        raise ValueError("Cannot shrink a passing trace")
    if initial.signature != target:
        raise ValueError("Trace does not reproduce the requested signature")

    current = trace
    tested = 0
    seen = {trace.sha256()}

    def retain(candidate: TraceTranscript) -> bool:
        nonlocal tested
        digest = candidate.sha256()
        if digest in seen or len(candidate.to_json()) >= len(current.to_json()):
            return False
        seen.add(digest)
        try:
            validate_trace(candidate)
        except TraceValidationError:
            return False
        tested += 1
        return capture_failure(candidate).signature == target

    changed = True
    while changed:
        changed = False
        for candidate in _operation_candidates(current):
            if retain(candidate):
                current = candidate
                changed = True
                break
        if changed:
            continue
        for candidate in _ast_candidates(current):
            if retain(candidate):
                current = candidate
                changed = True
                break
        if changed:
            continue
        for candidate in _value_candidates(current):
            if retain(candidate):
                current = candidate
                changed = True
                break
        if changed:
            continue
        for candidate in _fault_candidates(current):
            if retain(candidate):
                current = candidate
                changed = True
                break

    return ShrinkResult(trace.sha256(), current, target, tested)


def _renumber(operations: Iterable[TraceOperation]) -> tuple[TraceOperation, ...]:
    return tuple(
        replace(operation, step=step) for step, operation in enumerate(operations)
    )


def _operation_candidates(trace: TraceTranscript) -> Iterable[TraceTranscript]:
    operations = list(trace.operations)
    chunk = max(1, len(operations) // 2)
    while chunk >= 1:
        for start in range(0, len(operations), chunk):
            remaining = operations[:start] + operations[start + chunk :]
            yield replace(trace, operations=_renumber(remaining))
        chunk //= 2


def _direct_simplifications(expression: NumericExpr) -> Iterable[NumericExpr]:
    if isinstance(expression, Number):
        for value in (0, 1, -1):
            if value != expression.value:
                yield Number(value)
        return
    if isinstance(expression, Binary):
        yield expression.left
        yield expression.right
        for simpler in _direct_simplifications(expression.left):
            if simpler.output_type is ValueType.NUMBER:
                yield Binary(expression.operator, simpler, expression.right)
        for simpler in _direct_simplifications(expression.right):
            if simpler.output_type is ValueType.NUMBER:
                yield Binary(expression.operator, expression.left, simpler)
        return
    if isinstance(expression, Negate):
        yield expression.operand
        for simpler in _direct_simplifications(expression.operand):
            if simpler.output_type is ValueType.NUMBER:
                yield Negate(simpler)
        return
    if isinstance(expression, Call):
        if expression.function == "IF":
            yield expression.arguments[1]
            yield expression.arguments[2]
            indexes = (1, 2)
        else:
            yield from expression.arguments
            indexes = range(len(expression.arguments))
        for index in indexes:
            for simpler in _direct_simplifications(expression.arguments[index]):
                if simpler.output_type is not ValueType.NUMBER:
                    continue
                arguments = list(expression.arguments)
                arguments[index] = simpler
                yield Call(expression.function, tuple(arguments))


def _input_values(trace: TraceTranscript) -> dict[str, int]:
    values: dict[str, int] = {}
    for operation in trace.operations:
        if operation.kind == "set_cell_hardvalue":
            address = operation.args.get("address", ())
            if len(address) == 1:
                values[str(address[0])] = int(operation.args["value"])
    return values


def _rewrite_expression(
    trace: TraceTranscript,
    expression: NumericExpr,
    *,
    operations: tuple[TraceOperation, ...] | None = None,
) -> TraceTranscript:
    rewritten: list[TraceOperation] = []
    source = operations if operations is not None else trace.operations
    for operation in source:
        if operation.kind == "set_rule":
            args = dict(operation.args)
            args["expression"] = render(expression)
            operation = replace(operation, args=args)
        rewritten.append(operation)
    metadata = dict(trace.metadata)
    metadata["ast"] = to_dict(expression)
    oracle = dict(metadata["oracle"])
    candidate = replace(trace, operations=tuple(rewritten), metadata=metadata)
    oracle["expected"] = canonical(evaluate(expression, _input_values(candidate)))
    metadata["oracle"] = oracle
    return replace(candidate, metadata=metadata)


def _ast_candidates(trace: TraceTranscript) -> Iterable[TraceTranscript]:
    expression = from_dict(trace.metadata["ast"])
    for simpler in _direct_simplifications(expression):
        if simpler.output_type is ValueType.NUMBER:
            try:
                yield _rewrite_expression(trace, simpler)
            except KeyError:
                continue


def _value_candidates(trace: TraceTranscript) -> Iterable[TraceTranscript]:
    expression = from_dict(trace.metadata["ast"])
    for index, operation in enumerate(trace.operations):
        if operation.kind != "set_cell_hardvalue":
            continue
        current_value = int(operation.args["value"])
        for value in (0, 1, -1):
            if value == current_value:
                continue
            args = dict(operation.args)
            args["value"] = value
            operations = list(trace.operations)
            operations[index] = replace(operation, args=args)
            yield _rewrite_expression(trace, expression, operations=tuple(operations))


def _fault_candidates(trace: TraceTranscript) -> Iterable[TraceTranscript]:
    decisions = list(decisions_from_trace(trace))
    for index in range(len(decisions)):
        yield with_faults(trace, decisions[:index] + decisions[index + 1 :])
