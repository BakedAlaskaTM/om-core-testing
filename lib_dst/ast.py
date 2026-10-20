"""Harness-owned typed AST for the Phase 1 numeric grammar."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, TypeAlias


class ValueType(str, Enum):
    NUMBER = "number"
    BOOLEAN = "boolean"


@dataclass(frozen=True)
class Number:
    value: int
    output_type: ValueType = field(default=ValueType.NUMBER, init=False)


@dataclass(frozen=True)
class Reference:
    handle: str
    cube_name: str
    dimension_name: str
    item_name: str
    output_type: ValueType = field(default=ValueType.NUMBER, init=False)


@dataclass(frozen=True)
class Negate:
    operand: "NumericExpr"
    output_type: ValueType = field(default=ValueType.NUMBER, init=False)

    def __post_init__(self) -> None:
        if self.operand.output_type is not ValueType.NUMBER:
            raise TypeError("Negation requires a numeric operand")


@dataclass(frozen=True)
class Binary:
    operator: str
    left: "NumericExpr"
    right: "NumericExpr"
    output_type: ValueType = field(default=ValueType.NUMBER, init=False)

    def __post_init__(self) -> None:
        if self.operator not in {"+", "-", "*", "/"}:
            raise ValueError(f"Unsupported numeric operator: {self.operator}")
        if any(
            operand.output_type is not ValueType.NUMBER
            for operand in (self.left, self.right)
        ):
            raise TypeError("Arithmetic operators require numeric operands")


@dataclass(frozen=True)
class Comparison:
    operator: str
    left: "NumericExpr"
    right: "NumericExpr"
    output_type: ValueType = field(default=ValueType.BOOLEAN, init=False)

    def __post_init__(self) -> None:
        if self.operator not in {">", "<", ">=", "<=", "==", "!="}:
            raise ValueError(f"Unsupported comparison operator: {self.operator}")
        if any(
            operand.output_type is not ValueType.NUMBER
            for operand in (self.left, self.right)
        ):
            raise TypeError("Comparisons require numeric operands")


@dataclass(frozen=True)
class Call:
    function: str
    arguments: tuple["NumericExpr", ...]
    output_type: ValueType = field(default=ValueType.NUMBER, init=False)

    def __post_init__(self) -> None:
        function = self.function.upper()
        object.__setattr__(self, "function", function)
        limits = {"IF": (3, 3), "SUM": (1, 4), "AVG": (1, 4), "ROUND": (1, 2)}
        if function not in limits:
            raise ValueError(f"Unsupported numeric function: {function}")
        lower, upper = limits[function]
        if not lower <= len(self.arguments) <= upper:
            raise ValueError(f"Invalid argument count for {function}")
        if function == "IF":
            if self.arguments[0].output_type is not ValueType.BOOLEAN:
                raise TypeError("IF requires a boolean condition")
            numeric_arguments = self.arguments[1:]
        else:
            numeric_arguments = self.arguments
        if any(argument.output_type is not ValueType.NUMBER for argument in numeric_arguments):
            raise TypeError(f"{function} requires numeric value arguments")


NumericExpr: TypeAlias = Number | Reference | Negate | Binary | Comparison | Call


def render(expression: NumericExpr) -> str:
    if isinstance(expression, Number):
        return str(expression.value)
    if isinstance(expression, Reference):
        return (
            f"{expression.cube_name}::"
            f"[{expression.dimension_name}.{expression.item_name}]"
        )
    if isinstance(expression, Negate):
        return f"(-{render(expression.operand)})"
    if isinstance(expression, Comparison):
        return (
            f"({render(expression.left)} {expression.operator} "
            f"{render(expression.right)})"
        )
    if isinstance(expression, Call):
        return (
            f"{expression.function}("
            + ", ".join(render(argument) for argument in expression.arguments)
            + ")"
        )
    return (
        f"({render(expression.left)} {expression.operator} "
        f"{render(expression.right)})"
    )


def to_dict(expression: NumericExpr) -> dict[str, Any]:
    if isinstance(expression, Number):
        return {"type": "number", "value": expression.value}
    if isinstance(expression, Reference):
        return {
            "type": "reference",
            "handle": expression.handle,
            "cube": expression.cube_name,
            "dimension": expression.dimension_name,
            "item": expression.item_name,
        }
    if isinstance(expression, Negate):
        return {"type": "negate", "operand": to_dict(expression.operand)}
    if isinstance(expression, Comparison):
        return {
            "type": "comparison",
            "operator": expression.operator,
            "left": to_dict(expression.left),
            "right": to_dict(expression.right),
        }
    if isinstance(expression, Call):
        return {
            "type": "call",
            "function": expression.function,
            "arguments": [to_dict(argument) for argument in expression.arguments],
        }
    return {
        "type": "binary",
        "operator": expression.operator,
        "left": to_dict(expression.left),
        "right": to_dict(expression.right),
    }


def from_dict(value: dict[str, Any]) -> NumericExpr:
    node_type = value["type"]
    if node_type == "number":
        return Number(int(value["value"]))
    if node_type == "reference":
        return Reference(
            handle=str(value["handle"]),
            cube_name=str(value["cube"]),
            dimension_name=str(value["dimension"]),
            item_name=str(value["item"]),
        )
    if node_type == "negate":
        return Negate(from_dict(value["operand"]))
    if node_type == "comparison":
        return Comparison(
            str(value["operator"]),
            from_dict(value["left"]),
            from_dict(value["right"]),
        )
    if node_type == "call":
        return Call(
            str(value["function"]),
            tuple(from_dict(argument) for argument in value["arguments"]),
        )
    if node_type == "binary":
        return Binary(
            str(value["operator"]),
            from_dict(value["left"]),
            from_dict(value["right"]),
        )
    raise ValueError(f"Unknown AST node type: {node_type}")
