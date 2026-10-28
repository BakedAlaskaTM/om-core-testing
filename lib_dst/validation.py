"""Structural validation for replayable and shrinkable DST traces."""

from __future__ import annotations

from lib_dst.ast import Binary, Call, Comparison, Negate, Reference, from_dict, render
from lib_dst.faults import decisions_from_trace
from lib_dst.schemas import TraceTranscript
from lib_dst.symbols import SymbolKind
from lib_openm.rule_eval import _Parser, _tokenise


class TraceValidationError(ValueError):
    pass


def validate_trace(trace: TraceTranscript) -> None:
    try:
        _validate_trace(trace)
    except TraceValidationError:
        raise
    except (KeyError, TypeError, ValueError, SyntaxError) as exc:
        raise TraceValidationError(str(exc)) from exc


def _validate_trace(trace: TraceTranscript) -> None:
    symbols = trace.metadata.get("symbols", {})
    created: set[str] = set()
    dimension_names: set[str] = set()
    cube_names: set[str] = set()
    configured_serial = False
    rule_expressions: list[str] = []
    hardvalue_handles: set[str] = set()

    for operation in trace.operations:
        args = operation.args
        if operation.kind == "configure_serial":
            if args.get("enabled") is not False:
                raise TraceValidationError("DST traces must force serial recalculation")
            configured_serial = True
        elif operation.kind == "create_dimension":
            _require_symbol(symbols, operation.target, SymbolKind.DIMENSION)
            _require_new(operation.target, created)
            dimension_names.add(str(args["name"]))
            created.add(operation.target)
        elif operation.kind == "create_dimension_item":
            _require_symbol(symbols, operation.target, SymbolKind.ITEM)
            _require_created(str(args["dimension"]), created)
            _require_new(operation.target, created)
            created.add(operation.target)
        elif operation.kind == "create_cube":
            _require_symbol(symbols, operation.target, SymbolKind.CUBE)
            _require_new(operation.target, created)
            for handle in args["dimensions"]:
                _require_created(str(handle), created)
            cube_names.add(str(args["name"]))
            created.add(operation.target)
        elif operation.kind == "set_cell_hardvalue":
            _require_created(operation.target, created)
            for handle in args["address"]:
                _require_created(str(handle), created)
                hardvalue_handles.add(str(handle))
        elif operation.kind == "set_rule":
            _require_created(operation.target, created)
            expression = str(args["expression"])
            _Parser(_tokenise(expression)).parse()
            rule_expressions.append(expression)
        elif operation.kind == "run_recalculation":
            continue
        else:
            raise TraceValidationError(f"Unsupported operation: {operation.kind}")

    if not configured_serial:
        raise TraceValidationError("Trace is missing serial recalculation configuration")
    if not set(trace.observe.dimensions).issubset(dimension_names):
        raise TraceValidationError("Observation references an uncreated dimension")
    if not set(trace.observe.cubes).issubset(cube_names):
        raise TraceValidationError("Observation references an uncreated cube")

    ast_value = trace.metadata.get("ast")
    if ast_value is not None:
        expression = from_dict(ast_value)
        rendered = render(expression)
        if rendered not in rule_expressions:
            raise TraceValidationError("Serialized AST does not match a rule expression")
        missing_values = _references(expression) - hardvalue_handles
        if missing_values:
            raise TraceValidationError(
                f"AST references inputs without hard values: {sorted(missing_values)}"
            )

    oracle = trace.metadata.get("oracle", {})
    target = oracle.get("target", {})
    if target.get("cube") not in trace.observe.cubes:
        raise TraceValidationError("Oracle target cube is not observed")
    if "expected" not in oracle:
        raise TraceValidationError("Trace is missing its oracle expectation")

    decisions = decisions_from_trace(trace)
    identities = [(decision.operation, decision.occurrence) for decision in decisions]
    if len(identities) != len(set(identities)):
        raise TraceValidationError("Duplicate fault decision occurrence")


def _require_symbol(symbols: dict, handle: str, kind: SymbolKind) -> None:
    value = symbols.get(handle)
    if value is None or value.get("kind") != kind.value:
        raise TraceValidationError(f"Missing {kind.value} symbol: {handle}")


def _require_created(handle: str, created: set[str]) -> None:
    if handle not in created:
        raise TraceValidationError(f"Symbol used before creation: {handle}")


def _require_new(handle: str, created: set[str]) -> None:
    if handle in created:
        raise TraceValidationError(f"Symbol created twice: {handle}")


def _references(expression) -> set[str]:
    if isinstance(expression, Reference):
        return {expression.handle}
    if isinstance(expression, Negate):
        return _references(expression.operand)
    if isinstance(expression, (Binary, Comparison)):
        return _references(expression.left) | _references(expression.right)
    if isinstance(expression, Call):
        result: set[str] = set()
        for argument in expression.arguments:
            result.update(_references(argument))
        return result
    return set()
