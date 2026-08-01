"""AST node classes for parsed rule body expressions."""
from __future__ import annotations

from typing import Any

from lib_openm.xls_compat import XLS_FUNCTIONS


class _AstNum:
    def __init__(self, v: float): self.v = v


class _AstStr:
    def __init__(self, s: str): self.s = s


class _AstBinOp:
    def __init__(self, op: str, l: Any, r: Any): self.op = op; self.l = l; self.r = r


class _AstUnOp:
    def __init__(self, op: str, operand: Any): self.op = op; self.operand = operand


class _AstRef:
    """[Dim:Item] explicit reference, optionally qualified with Cube::."""

    def __init__(
        self,
        dim_name: str,
        item_name: str,
        cube_name: str | None = None,
        allow_seq_keywords: bool = False,
    ):
        self.cube_name = cube_name
        self.dim_name = dim_name
        self.item_name = item_name
        self.allow_seq_keywords = allow_seq_keywords


class _AstMultiRef:
    """[Dim1.Item1, Dim2.Item2, ...] multi-dimension override reference.

    All segments share a single optional cube qualifier, e.g.::

        Cube::[Year.1994, Quarter.Q2]
    """

    def __init__(
        self,
        pairs: list[tuple[str, str]],
        cube_name: str | None = None,
        allow_seq_keywords: bool = False,
    ):
        self.pairs = pairs
        self.cube_name = cube_name
        self.allow_seq_keywords = allow_seq_keywords


class _AstDynamicMultiRef:
    """Multi-dimension override with function calls, e.g., [DESC(A.a), B.b].

    Can contain a mix of static (dim, item) pairs and dynamic function calls
    that return lists of items. Evaluated at runtime to build cartesian product.

    All segments share a single optional cube qualifier.
    """

    def __init__(
        self,
        pairs: list[tuple[str, str]],
        dynamic_calls: list[_AstCall],
        cube_name: str | None = None,
    ):
        self.pairs = pairs  # Static (dim, item) pairs
        self.dynamic_calls = dynamic_calls  # Function calls like DESC()
        self.cube_name = cube_name


class _AstCtxRef:
    """Bare ItemName — resolved contextually at eval time."""
    def __init__(self, name: str): self.name = name


class _AstCall:
    def __init__(self, fn: str, args: list[Any]): self.fn = fn; self.args = args


# ---------------------------------------------------------------------------
# Built-in function registry
# ---------------------------------------------------------------------------

_FUNCTIONS = {
    "SUM",
    "IF",
    "MIN",
    "MAX",
    "AVG",
    "AVERAGE",
    "COUNT",
    "COUNTA",
    "COUNTIF",
    "COUNTIFS",
    "SUMIF",
    "SUMIFS",
    "ABS",
    "ROUND",
    "ROUNDUP",
    "ROUNDDOWN",
    "VALUE",
    "LABEL",
    "POS",
    "POSMAX",
    "RAND",
    "RANDBETWEEN",
    "LEN",
    "TRIM",
    "LEFT",
    "RIGHT",
    "IFERROR",
    "REPT",
    "CODE",
    "CHAR",
    "ANCE",
    "PEER",
    "SIBL",
    "DESC",
    "CHIL",
    "PARE",
    "JOIN",
    "SLICE",
    "REF",
    # Mathematical constants and functions (Excel-compatible)
    "PI",
    "LN",
    "LOG",
    "LOG10",
    "EXP",
    "SQRT",
    "POWER",
    "SIN",
    "COS",
    "TAN",
    "ASIN",
    "ACOS",
    "ATAN",
    "ATAN2",
    "RADIANS",
    "DEGREES",
    "SIGN",
    "FLOOR",
    "CEILING",
    "INT",
    "MOD",
    "QUOTIENT",
    # Logical functions (Excel-compatible)
    "AND",
    "OR",
    "NOT",
    "XOR",
    # Color functions for conditional formatting
    "COLORMAP",
    "HSV2RGB",
    "RGB",
    # Date functions
    "MONTH",
    "DAY",
    "YEAR",
    "DATE",
    "EOMONTH",
    "TODAY",
    "NOW",
    "WEEKDAY",
    "WEEKNUM",
}
_FUNCTIONS |= XLS_FUNCTIONS
