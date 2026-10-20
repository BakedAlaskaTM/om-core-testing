"""Independent exact-arithmetic oracle for the Phase 1 grammar."""

from __future__ import annotations

import math
from dataclasses import dataclass
from fractions import Fraction
from typing import Any, Mapping

from lib_dst.ast import Binary, Call, Comparison, Negate, Number, NumericExpr, Reference


@dataclass(frozen=True)
class OracleError:
    code: str


OracleValue = Fraction | OracleError


def evaluate(expression: NumericExpr, values: Mapping[str, int]) -> OracleValue:
    if isinstance(expression, Number):
        return Fraction(expression.value)
    if isinstance(expression, Reference):
        return Fraction(values[expression.handle])
    if isinstance(expression, Negate):
        operand = evaluate(expression.operand, values)
        return operand if isinstance(operand, OracleError) else -operand
    if isinstance(expression, Comparison):
        left = evaluate(expression.left, values)
        if isinstance(left, OracleError):
            return left
        right = evaluate(expression.right, values)
        if isinstance(right, OracleError):
            return right
        comparisons = {
            ">": left > right,
            "<": left < right,
            ">=": left >= right,
            "<=": left <= right,
            "==": left == right,
            "!=": left != right,
        }
        return Fraction(int(comparisons[expression.operator]))
    if isinstance(expression, Call):
        if expression.function == "IF":
            condition = evaluate(expression.arguments[0], values)
            if isinstance(condition, OracleError):
                return condition
            branch = expression.arguments[1] if condition else expression.arguments[2]
            return evaluate(branch, values)
        arguments = [evaluate(argument, values) for argument in expression.arguments]
        error = next((value for value in arguments if isinstance(value, OracleError)), None)
        if error is not None:
            return error
        numbers = [value for value in arguments if isinstance(value, Fraction)]
        if expression.function == "SUM":
            return sum(numbers, Fraction())
        if expression.function == "AVG":
            return sum(numbers, Fraction()) / len(numbers)
        value = numbers[0]
        places = int(numbers[1]) if len(numbers) == 2 else 0
        places = max(0, places)
        factor = 10**places
        scaled = value * factor
        sign = -1 if scaled < 0 else 1
        absolute = abs(scaled)
        rounded = (2 * absolute.numerator + absolute.denominator) // (
            2 * absolute.denominator
        )
        return Fraction(sign * rounded, factor)

    left = evaluate(expression.left, values)
    if isinstance(left, OracleError):
        return left
    right = evaluate(expression.right, values)
    if isinstance(right, OracleError):
        return right
    if expression.operator == "+":
        return left + right
    if expression.operator == "-":
        return left - right
    if expression.operator == "*":
        return left * right
    if right == 0:
        return OracleError("#DIV/0!")
    return left / right


def canonical(value: OracleValue) -> dict[str, Any]:
    if isinstance(value, OracleError):
        return {"error": value.code}
    return {"number": float(value)}


def matches(expected: dict[str, Any], actual: Any) -> bool:
    if "error" in expected:
        return actual == {"error": expected["error"]}
    if not isinstance(actual, (int, float)) or isinstance(actual, bool):
        return False
    return math.isclose(
        float(expected["number"]),
        float(actual),
        rel_tol=1e-9,
        abs_tol=1e-12,
    )
