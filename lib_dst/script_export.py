"""Deterministic OpenM script export for supported DST traces."""

from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

from lib_dst.ast import from_dict, render
from lib_dst.schemas import TraceOperation, TraceTranscript
from lib_dst.validation import TraceValidationError, validate_trace
from lib_openm.rule_eval import _Parser, _tokenise


class ScriptExportError(ValueError):
    """The trace cannot be represented faithfully by the OpenM script subset."""


_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def export_openm_script(trace: TraceTranscript) -> str:
    """Render *trace* as deterministic, standalone OpenM script text."""

    try:
        validate_trace(trace)
    except TraceValidationError as exc:
        raise ScriptExportError(f"Trace is not exportable: {exc}") from exc

    symbols = trace.metadata.get("symbols")
    if not isinstance(symbols, dict):
        raise ScriptExportError("Trace is missing its logical symbol table")

    ast_value = trace.metadata.get("ast")
    if not isinstance(ast_value, dict):
        raise ScriptExportError("Script export requires a serialized harness AST")
    expression = render(from_dict(ast_value))
    try:
        _Parser(_tokenise(expression)).parse()
    except (SyntaxError, TypeError, ValueError) as exc:
        raise ScriptExportError(f"Rendered AST is not valid OpenM syntax: {exc}") from exc

    rule_operations = [operation for operation in trace.operations if operation.kind == "set_rule"]
    if len(rule_operations) != 1:
        raise ScriptExportError("Script export currently requires exactly one AST-backed rule")
    if rule_operations[0].args.get("expression") != expression:
        raise ScriptExportError("Serialized AST does not equal the recorded rule expression")

    lines = [
        "# Generated deterministic simulation trace",
        f"# Trace SHA-256: {trace.sha256()}",
        f"# Master seed: {trace.metadata.get('master_seed', 0)}",
        "# Canonical JSON trace remains the replay authority.",
    ]
    _append_harness_metadata(lines, trace)
    lines.append("")

    for operation in trace.operations:
        lines.append(
            f"# Trace step {operation.step}: {operation.kind} ({operation.target})"
        )
        lines.append(_render_operation(operation, symbols, expression))

    lines.extend(_presentation_lines(trace, symbols))
    return "\n".join(lines) + "\n"


def write_openm_script(trace: TraceTranscript, path: str | Path) -> Path:
    """Write a deterministic trace export to an ``.openm`` file."""

    output = Path(path)
    if output.suffix.lower() != ".openm":
        raise ScriptExportError("OpenM script exports must use the .openm extension")
    output.write_text(export_openm_script(trace), encoding="utf-8", newline="\n")
    return output


def _append_harness_metadata(lines: list[str], trace: TraceTranscript) -> None:
    faults = trace.metadata.get("faults")
    scheduler = trace.metadata.get("scheduler")
    if faults:
        lines.append(
            "# Harness-only persistence faults (not simulated): "
            + json.dumps(faults, sort_keys=True, separators=(",", ":"))
        )
    if scheduler:
        lines.append(
            "# Harness-only scheduler policy (not simulated): "
            + json.dumps(scheduler, sort_keys=True, separators=(",", ":"))
        )


def _render_operation(
    operation: TraceOperation, symbols: dict[str, Any], expression: str
) -> str:
    args = operation.args
    if operation.kind == "configure_serial":
        if args.get("enabled") is not False:
            raise ScriptExportError("Only serial recalculation can be exported")
        return "set_multithread_recompute off"
    if operation.kind == "create_dimension":
        name = _identifier(args.get("name"), "dimension name")
        dim_type = str(args.get("dim_type"))
        if dim_type not in {"set", "seq"}:
            raise ScriptExportError(f"Unsupported dimension type: {dim_type}")
        return f"dimension create {name} --{dim_type}"
    if operation.kind == "create_dimension_item":
        dimension = _symbol(symbols, args.get("dimension"), "dimension")
        item = _symbol(symbols, operation.target, "item")
        return f"dimension item {dimension['name']} {item['name']}"
    if operation.kind == "create_cube":
        name = _identifier(args.get("name"), "cube name")
        dimensions = [
            _symbol(symbols, handle, "dimension")["name"]
            for handle in args.get("dimensions", ())
        ]
        if not dimensions:
            raise ScriptExportError("A script cube requires at least one dimension")
        return "cube create " + " ".join([name, *dimensions])
    if operation.kind == "set_cell_hardvalue":
        cube = _symbol(symbols, operation.target, "cube")["name"]
        selectors = []
        for handle in args.get("address", ()):
            item = _symbol(symbols, handle, "item")
            owner = _symbol(symbols, item.get("owner"), "dimension")
            selectors.append(f"{owner['name']}.{item['name']}")
        if not selectors:
            raise ScriptExportError("A hard value requires a logical cell address")
        return f"hval ${cube}::{':'.join(selectors)} = {_literal(args.get('value'))}"
    if operation.kind == "set_rule":
        cube = _symbol(symbols, operation.target, "cube")["name"]
        targets = args.get("targets")
        if not isinstance(targets, list) or not targets:
            raise ScriptExportError("A rule requires at least one logical target")
        selectors = []
        for target in targets:
            if not isinstance(target, list) or len(target) != 2:
                raise ScriptExportError(f"Unsupported rule target: {target!r}")
            selectors.append(
                f"{_identifier(target[0], 'target dimension')}."
                f"{_identifier(target[1], 'target item')}"
            )
        anchor = "$" if bool(args.get("is_anchored")) else ""
        return f"rule {anchor}{cube}::{':'.join(selectors)} = {expression}"
    if operation.kind == "run_recalculation":
        if args.get("scope", "all") != "all":
            raise ScriptExportError("Only full recalculation can be exported")
        return "calc"
    raise ScriptExportError(f"Unsupported trace operation: {operation.kind}")


def _presentation_lines(
    trace: TraceTranscript, symbols: dict[str, Any]
) -> list[str]:
    cube_name = str(trace.metadata.get("oracle", {}).get("target", {}).get("cube", ""))
    cube_operation = next(
        (
            operation
            for operation in trace.operations
            if operation.kind == "create_cube" and operation.args.get("name") == cube_name
        ),
        None,
    )
    if cube_operation is None:
        raise ScriptExportError("Oracle cube has no corresponding create operation")
    dimension_names = [
        _symbol(symbols, handle, "dimension")["name"]
        for handle in cube_operation.args.get("dimensions", ())
    ]
    if not dimension_names:
        raise ScriptExportError("Presentation view requires an observed cube dimension")

    view_name = f"DST_Trace_{trace.sha256()[:12]}"
    oracle = trace.metadata["oracle"]
    target = oracle["target"]
    expected = oracle["expected"]
    address = list(target.get("address", ()))
    item_names = [value for value in address if value != "@.value"]
    if len(item_names) != len(dimension_names):
        raise ScriptExportError("Oracle address does not match the cube dimensions")
    oracle_ref = f"{cube_name}::@.value:" + ":".join(
        f"{dimension}.{_identifier(item, 'oracle item')}"
        for dimension, item in zip(dimension_names, item_names)
    )

    lines = [
        "",
        "# Presentation-only GUI view; not part of canonical trace observation.",
        f"view {view_name} = {cube_name}::" + ":".join(dimension_names),
        "# Expected oracle: "
        + json.dumps(expected, sort_keys=True, separators=(",", ":")),
    ]
    if isinstance(expected, dict) and set(expected) == {"number"}:
        expected_number = float(expected["number"])
        tolerance = max(abs(expected_number) * 1e-9, 1e-12)
        lines.extend(
            (
                f"# Numeric checks use the harness oracle tolerance ({tolerance!r}).",
                f'assert {oracle_ref} >= {_literal(expected_number - tolerance)} '
                '"DST trace oracle below tolerance"',
                f'assert {oracle_ref} <= {_literal(expected_number + tolerance)} '
                '"DST trace oracle above tolerance"',
            )
        )
    elif isinstance(expected, dict) and set(expected) == {"error"}:
        lines.append(
            f"# Visually confirm {oracle_ref} displays {expected['error']}; "
            "script assertions cannot compare CellError values."
        )
    else:
        raise ScriptExportError(f"Unsupported oracle expectation: {expected!r}")
    return lines


def _symbol(symbols: dict[str, Any], handle: Any, kind: str) -> dict[str, Any]:
    value = symbols.get(str(handle))
    if not isinstance(value, dict) or value.get("kind") != kind:
        raise ScriptExportError(f"Missing {kind} symbol: {handle}")
    result = dict(value)
    result["name"] = _identifier(result.get("name"), f"{kind} name")
    return result


def _identifier(value: Any, label: str) -> str:
    text = str(value)
    if not _IDENTIFIER.fullmatch(text):
        raise ScriptExportError(f"Unsupported {label}: {text!r}")
    return text


def _literal(value: Any) -> str:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ScriptExportError(f"Unsupported script value: {value!r}")
    if isinstance(value, float) and not math.isfinite(value):
        raise ScriptExportError(f"Non-finite script value: {value!r}")
    return repr(value)
