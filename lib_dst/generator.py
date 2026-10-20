"""Deterministic generator for the Phase 1 supported OM Core subset."""

from __future__ import annotations

import random
from typing import Any

from lib_dst.ast import (
    Binary,
    Call,
    Comparison,
    Negate,
    Number,
    NumericExpr,
    Reference,
    render,
    to_dict,
)
from lib_dst.oracle import canonical, evaluate
from lib_dst.schemas import (
    EnvironmentDescriptor,
    ObservationSpec,
    TraceOperation,
    TraceTranscript,
)
from lib_dst.seeding import SeedStreams
from lib_dst.symbols import Symbol, SymbolKind, SymbolTable
from lib_openm.rule_eval import _Parser, _tokenise


DIMENSION = "dimension:line"
INPUT_A = "item:line/input-a"
INPUT_B = "item:line/input-b"
RESULT = "item:line/result"
CUBE = "cube:calc"


def _symbols() -> SymbolTable:
    symbols = SymbolTable()
    symbols.declare(Symbol(DIMENSION, SymbolKind.DIMENSION, "Line"))
    symbols.declare(Symbol(INPUT_A, SymbolKind.ITEM, "InputA", DIMENSION))
    symbols.declare(Symbol(INPUT_B, SymbolKind.ITEM, "InputB", DIMENSION))
    symbols.declare(Symbol(RESULT, SymbolKind.ITEM, "Result", DIMENSION))
    symbols.declare(Symbol(CUBE, SymbolKind.CUBE, "Calc"))
    return symbols


def _reference(handle: str, item_name: str) -> Reference:
    return Reference(handle, "Calc", "Line", item_name)


def _leaf(rng: random.Random) -> NumericExpr:
    choice = rng.randrange(3)
    if choice == 0:
        return _reference(INPUT_A, "InputA")
    if choice == 1:
        return _reference(INPUT_B, "InputB")
    return Number(rng.randint(-9, 9))


def _expression(rng: random.Random, depth: int) -> NumericExpr:
    if depth <= 0 or rng.random() < 0.28:
        return _leaf(rng)
    if rng.random() < 0.12:
        return Negate(_expression(rng, depth - 1))
    operator = rng.choice(("+", "-", "*", "/"))
    right = Number(rng.randint(1, 9)) if operator == "/" else _expression(rng, depth - 1)
    return Binary(operator, _expression(rng, depth - 1), right)


def _operation(
    operations: list[TraceOperation], kind: str, target: str, args: dict[str, Any]
) -> None:
    operations.append(TraceOperation(len(operations), kind, target, args))


def generate_trace(master_seed: int) -> TraceTranscript:
    streams = SeedStreams(master_seed)
    value_rng = streams.random("workload.values")
    expression_rng = streams.random("workload.expression")
    feature_rng = streams.random("workload.features")

    input_a = value_rng.randint(-20, 20)
    input_b = value_rng.randint(-20, 20)
    feature = feature_rng.randrange(16)
    if feature == 0:
        input_b = 0
        expression: NumericExpr = Binary(
            "/",
            _reference(INPUT_A, "InputA"),
            _reference(INPUT_B, "InputB"),
        )
        feature_name = "division-error"
    elif feature == 1:
        expression = Call(
            "IF",
            (
                Comparison(">", _reference(INPUT_A, "InputA"), _reference(INPUT_B, "InputB")),
                _expression(expression_rng, 1),
                _expression(expression_rng, 1),
            ),
        )
        feature_name = "comparison-if"
    elif feature == 2:
        expression = Call("SUM", tuple(_leaf(expression_rng) for _ in range(3)))
        feature_name = "sum"
    elif feature == 3:
        expression = Call("AVG", tuple(_leaf(expression_rng) for _ in range(3)))
        feature_name = "avg"
    elif feature == 4:
        expression = Call(
            "ROUND",
            (Binary("/", _expression(expression_rng, 1), Number(3)), Number(2)),
        )
        feature_name = "round"
    else:
        expression = _expression(expression_rng, 2)
        feature_name = "numeric"

    expression_text = render(expression)
    _Parser(_tokenise(expression_text)).parse()
    expected = canonical(evaluate(expression, {INPUT_A: input_a, INPUT_B: input_b}))

    symbols = _symbols()
    operations: list[TraceOperation] = []
    _operation(operations, "configure_serial", "runtime", {"enabled": False})
    _operation(
        operations,
        "create_dimension",
        DIMENSION,
        {"name": "Line", "dim_type": "set"},
    )
    for handle, name in ((INPUT_A, "InputA"), (INPUT_B, "InputB"), (RESULT, "Result")):
        _operation(
            operations,
            "create_dimension_item",
            handle,
            {"dimension": DIMENSION, "name": name},
        )
    _operation(
        operations,
        "create_cube",
        CUBE,
        {"name": "Calc", "dimensions": [DIMENSION]},
    )
    for handle, value in ((INPUT_A, input_a), (INPUT_B, input_b)):
        _operation(
            operations,
            "set_cell_hardvalue",
            CUBE,
            {"address": [handle], "value": value},
        )
    _operation(
        operations,
        "set_rule",
        CUBE,
        {
            "targets": [["Line", "Result"]],
            "expression": expression_text,
            "is_anchored": True,
        },
    )
    _operation(operations, "run_recalculation", "runtime", {"scope": "all"})

    domains = (
        "workload.values",
        "workload.expression",
        "workload.features",
        "runtime.clock",
        "runtime.ids",
        "runtime.random",
    )
    return TraceTranscript(
        environment=EnvironmentDescriptor.current(),
        operations=tuple(operations),
        observe=ObservationSpec(dimensions=("Line",), cubes=("Calc",)),
        metadata={
            "generator": "phase1.numeric.v1",
            "master_seed": master_seed,
            "subseeds": {domain: f"{streams.seed(domain):032x}" for domain in domains},
            "symbols": symbols.to_dict(),
            "ast": to_dict(expression),
            "oracle": {
                "target": {"cube": "Calc", "address": ["@.value", "Result"]},
                "expected": expected,
            },
            "features": [feature_name],
        },
    )
