"""Rule body evaluation engine."""
from __future__ import annotations

import fnmatch
import itertools
import math
import random
from typing import Any, Callable

from lib_openm import cr_math

from lib_openm.xls_compat import (
    XLS_FUNCTIONS,
    eval_xls_function,
    _coerce_excel_date,
    _excel_year_from_value,
    _excel_serial_from_date,
    _excel_serial_from_datetime,
)

from .ast_nodes import (
    _AstBinOp, _AstCall, _AstCtxRef, _AstDynamicMultiRef, _AstMultiRef,
    _AstNum, _AstRef, _AstStr, _AstUnOp, _FUNCTIONS
)
from .tokenizer import _SEQ_KEYWORDS
from .parser import _Parser
from .resolver import CubeResolver
from .tokenizer import _tokenise
from .utils import CellError, RuleValidationError, _RULE_EVAL_DEBUG, _normalize_negative_zero


class RuleEvaluator:
    """Evaluate rule body expressions.  Pure literals work with no resolver."""

    def __init__(self):
        self._ast_cache: dict[str, Any] = {}

    @staticmethod
    def _with_seq_keyword_guard(
        resolver: CubeResolver | None,
        allow_seq_keywords: bool,
        func: Callable,
        *args: Any,
        **kwargs: Any,
    ) -> Any:
        if resolver is None or not allow_seq_keywords:
            return func(*args, **kwargs)

        prev_flag = getattr(resolver, "_allow_seq_keywords", False)
        resolver._allow_seq_keywords = True
        try:
            return func(*args, **kwargs)
        finally:
            resolver._allow_seq_keywords = prev_flag

    @staticmethod
    def _is_error(value: Any) -> bool:
        """Check if a value is an error that should propagate.

        Only CellError instances are considered errors. String error codes
        are NOT supported - they must be CellError objects.
        """
        return isinstance(value, CellError)

    @staticmethod
    def _normalize_ieee_special(value: Any) -> Any:
        """Normalize IEEE 754 special values (NaN, Inf) to CellError.

        Per Roadmap_ms_05: NaN and infinities should be normalized into
        explicit engine error values at evaluation boundaries.

        Returns:
            CellError if value is NaN or infinite, otherwise original value.
        """
        if isinstance(value, float):
            if math.isnan(value):
                return CellError("#NUM!")
            if math.isinf(value):
                return CellError("#RANGE!")
        return value

    @staticmethod
    def _coerce_number(value: Any) -> Any:
        """Coerce a value to float for numeric operations.

        Returns CellError("#VALUE!") if the value is not a number.
        Text is never coerced — matching TM1/Anaplan/Quantrix behavior.
        """
        if isinstance(value, (int, float)):
            return float(value)
        return CellError("#VALUE!")

    def _eval_or_error(
        self, node: Any, resolver: CubeResolver | None, addr: tuple[str, ...]
    ) -> Any:
        """Evaluate a node and return it if it's an error, otherwise return the value."""
        v = self._eval(node, resolver, addr)
        return v if self._is_error(v) else None

    def _require_resolver(self, fn_name: str, resolver: CubeResolver | None) -> CubeResolver:
        """Ensure a resolver is available for functions that need it."""
        if resolver is None:
            raise RuntimeError(f"{fn_name} requires a CubeResolver")
        return resolver

    @staticmethod
    def _require_argc(
        node: _AstCall, exact: int | None = None, min_args: int | None = None, max_args: int | None = None
    ) -> None:
        """Validate function argument count."""
        if exact is not None and len(node.args) != exact:
            raise ValueError(f"{node.fn} requires {exact} argument{'s' if exact != 1 else ''}")
        if min_args is not None and len(node.args) < min_args:
            raise ValueError(f"{node.fn} requires at least {min_args} argument{'s' if min_args != 1 else ''}")
        if max_args is not None and len(node.args) > max_args:
            raise ValueError(f"{node.fn} requires at most {max_args} argument{'s' if max_args != 1 else ''}")

    def _fn_registry(self) -> dict[str, Callable[[_AstCall, CubeResolver | None, tuple[str, ...]], Any]]:
        """Registry of function name -> handler method."""
        registry = {
            # Excel lookup/reference functions
            "XLS_INDEX": self._eval_xls_index,
            "XLS_OFFSET": self._eval_xls_offset,
            "XLS_MATCH": self._eval_xls_match,
            "XLS_ROWS": self._eval_xls_rows,
            "XLS_COLUMNS": self._eval_xls_columns,
            "XLS_HLOOKUP": self._eval_xls_hlookup,
            "XLS_VLOOKUP": self._eval_xls_vlookup,
            "XLS_SUM": self._eval_xls_sum_wrapper,
            "XLS_XIRR": self._eval_xls_xirr_wrapper,
            "XLS_NPV": self._eval_xls_npv_wrapper,
            "XLS_IRR": self._eval_xls_irr_wrapper,
            # Conditional
            "IF": self._fn_if,
            # Math functions
            "ABS": self._fn_abs,
            "ROUND": self._fn_round,
            "PI": self._fn_pi,
            "LN": self._fn_ln,
            "LOG": self._fn_log,
            "LOG10": self._fn_log10,
            "EXP": self._fn_exp,
            "SQRT": self._fn_sqrt,
            "POWER": self._fn_power,
            "SIN": self._fn_sin,
            "COS": self._fn_cos,
            "TAN": self._fn_tan,
            "ASIN": self._fn_asin,
            "ACOS": self._fn_acos,
            "ATAN": self._fn_atan,
            "ATAN2": self._fn_atan2,
            "RADIANS": self._fn_radians,
            "DEGREES": self._fn_degrees,
            "SIGN": self._fn_sign,
            "INT": self._fn_int,
            "MOD": self._fn_mod,
            "QUOTIENT": self._fn_quotient,
            "ROUNDUP": self._fn_roundup,
            "ROUNDDOWN": self._fn_rounddown,
            "TRUNC": self._fn_trunc,
            "FLOOR": self._fn_floor,
            "CEILING": self._fn_ceiling,
            # Logical functions
            "AND": self._fn_and,
            "OR": self._fn_or,
            "NOT": self._fn_not,
            "XOR": self._fn_xor,
            "TRUE": self._fn_true,
            "FALSE": self._fn_false,
            # Type conversion
            "VALUE": self._fn_value,
            "IFERROR": self._fn_iferror,
            # Metadata functions
            "LABEL": self._fn_label,
            "POS": self._fn_pos,
            "POSMAX": self._fn_posmax,
            # Hierarchy navigation
            "ANCE": self._fn_ance,
            "PEER": self._fn_peer,
            "SIBL": self._fn_sibl,
            "DESC": self._eval_desc,
            "CHIL": self._eval_chil,
            "PARE": self._eval_pare,
            # Array/string operations
            "JOIN": self._fn_join,
            "CONCAT": self._fn_concat,
            "CONCATENATE": self._fn_concat,
            # Volatile functions
            "RAND": self._fn_rand,
            "RANDBETWEEN": self._fn_randbetween,
            # String functions
            "LEN": self._fn_len,
            "TRIM": self._fn_trim,
            "LEFT": self._fn_left,
            "RIGHT": self._fn_right,
            "REPT": self._fn_rept,
            "CODE": self._fn_code,
            "CHAR": self._fn_char,
            "UPPER": self._fn_upper,
            "LOWER": self._fn_lower,
            "PROPER": self._fn_proper,
            "SUBSTITUTE": self._fn_substitute,
            "REPLACE": self._fn_replace,
            "MID": self._fn_mid,
            "FIND": self._fn_find,
            # Array slicing
            "SLICE": self._fn_slice,
            # Color functions
            "COLORMAP": self._fn_colormap,
            "HSV2RGB": self._fn_hsv2rgb,
            "RGB": self._fn_rgb,
            "REF": self._fn_ref,
            # Date functions
            "MONTH": self._fn_month,
            "DAY": self._fn_day,
            "YEAR": self._fn_year,
            "DATE": self._fn_date,
            "EOMONTH": self._fn_eomonth,
            "TODAY": self._fn_today,
            "NOW": self._fn_now,
            "WEEKDAY": self._fn_weekday,
            "WEEKNUM": self._fn_weeknum,
        }
        # Dynamically add registered UDF handlers
        try:
            from lib_openm.udf_registry import get_default_registry
            udf_reg = get_default_registry()
            for udf_def in udf_reg.list_all():
                registry[udf_def.name] = self._make_udf_handler(udf_def)
        except ImportError:
            pass  # UDF registry not available
        return registry
    
    def _make_udf_handler(self, udf_def) -> Callable:
        """Create a handler function for a UDF that evaluates its body with substituted args."""
        def udf_handler(node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
            """Evaluate UDF with argument values substituted into its AST body."""
            try:
                from lib_openm.udf_registry import UDFResolver
                
                # Evaluate all arguments
                arg_values = []
                for arg in node.args:
                    val = self._eval(arg, resolver, addr)
                    if isinstance(val, list):
                        val = val[0] if val else 0.0
                    arg_values.append(val)
                
                # Resolve params and evaluate body
                resolver_obj = UDFResolver(udf_def.params, arg_values)
                result = resolver_obj.eval_body(udf_def.ast)
                
                # Normalize special values
                result = self._normalize_ieee_special(result)
                return result
            except ZeroDivisionError:
                return CellError("#DIV/0!")
            except Exception as e:
                return CellError("#EXPRESSION!")
        
        return udf_handler

    def eval(
        self,
        expression: str,
        context: dict[str, Any] | None = None,
        resolver: CubeResolver | None = None,
        base_addr: tuple[str, ...] = (),
    ) -> Any:
        expr = expression.strip()
        if expr == "":
            return 0.0

        cached = self._ast_cache.get(expr)
        if cached is not None:
            ast_node = cached
        else:
            # Syntactic validation for all dynamic bounds in this expression:
            # any inner $<...> that contains range syntax or wildcard items is
            # illegal and must raise RuleValidationError, regardless of the
            # evaluation context.
            if "$<" in expr:
                i = 0
                n = len(expr)
                while i < n:
                    start = expr.find("$<", i)
                    if start == -1:
                        break
                    end = expr.find(">", start + 2)
                    if end == -1:
                        # Let the normal parser surface a syntax error later.
                        break
                    inner = expr[start + 2 : end]
                    from .refs import _validate_dynamic_bound
                    _validate_dynamic_bound(inner)
                    i = end + 1

            tokens = _tokenise(expr)
            ast_node = _Parser(tokens).parse()

            # Validate that rules don't have bidirectional recurrence (both PREV and NEXT)
            self._validate_no_bidirectional_recurrence(ast_node)
            self._ast_cache[expr] = ast_node
        
        result = self._eval(ast_node, resolver, base_addr)
        return _normalize_negative_zero(result)

    def _validate_no_bidirectional_recurrence(self, node: Any) -> None:
        """Check that rule body does not contain both PREV and NEXT (bidirectional recurrence).
        
        Recurrence rules that look both backward and forward create unresolvable
        dependencies and are not allowed.
        """
        seq_keywords_found: set[str] = set()
        
        def _collect_keywords(n: Any) -> None:
            if isinstance(n, _AstRef):
                item_upper = n.item_name.upper()
                if item_upper in _SEQ_KEYWORDS:
                    seq_keywords_found.add(item_upper)
            elif isinstance(n, _AstMultiRef):
                for _, item_name in n.pairs:
                    item_upper = item_name.upper()
                    if item_upper in _SEQ_KEYWORDS:
                        seq_keywords_found.add(item_upper)
            elif isinstance(n, _AstCtxRef):
                # Check contextual refs (bare names like PREV, NEXT)
                name_upper = n.name.upper()
                if name_upper in _SEQ_KEYWORDS:
                    seq_keywords_found.add(name_upper)
            elif isinstance(n, _AstBinOp):
                _collect_keywords(n.l)
                _collect_keywords(n.r)
            elif isinstance(n, _AstUnOp):
                _collect_keywords(n.operand)
            elif isinstance(n, _AstCall):
                for arg in n.args:
                    _collect_keywords(arg)
        
        _collect_keywords(node)
        
        # Check for bidirectional recurrence: both PREV and NEXT present
        if "PREV" in seq_keywords_found and "NEXT" in seq_keywords_found:
            raise RuleValidationError(
                "Bidirectional recurrence rule detected: cannot use both PREV and NEXT in the same rule. "
                "Recurrence rules must calculate in one direction only (either backward with PREV or forward with NEXT, not both)."
            )

    def _eval(self, node: Any, resolver: CubeResolver | None, addr: tuple[str, ...], volatile_seq: list[int] | None = None) -> Any:
        _RULE_EVAL_DEBUG and print(f"DEBUG _eval: node type={type(node).__name__}, isinstance _AstDynamicMultiRef={isinstance(node, _AstDynamicMultiRef)}")
        if isinstance(node, _AstNum):
            return node.v

        if isinstance(node, _AstStr):
            return node.s

        if isinstance(node, _AstUnOp):
            v = self._eval(node.operand, resolver, addr, volatile_seq)
            # Propagate CellError values
            if self._is_error(v):
                return v
            result = -v if node.op == "-" else v
            return self._normalize_ieee_special(_normalize_negative_zero(result))

        if isinstance(node, _AstBinOp):
            l = self._eval(node.l, resolver, addr, volatile_seq)
            # Left-error-wins: return left error before evaluating right operand
            if self._is_error(l):
                return l
            r = self._eval(node.r, resolver, addr, volatile_seq)
            if self._is_error(r):
                return r
            op = node.op
            if op in ("+", "-", "*", "/", "**"):
                l = self._coerce_number(l)
                if self._is_error(l):
                    return l
                r = self._coerce_number(r)
                if self._is_error(r):
                    return r
            if op == "+":
                result = l + r
                return self._normalize_ieee_special(_normalize_negative_zero(result))
            if op == "-":
                result = l - r
                return self._normalize_ieee_special(_normalize_negative_zero(result))
            if op == "*":
                result = l * r
                return self._normalize_ieee_special(_normalize_negative_zero(result))
            if op == "/":
                if r == 0: raise ZeroDivisionError("#DIV/0!")
                result = l / r
                return self._normalize_ieee_special(_normalize_negative_zero(result))
            if op == "**":
                try:
                    result = l ** r
                except ValueError:
                    return CellError("#NUM!")
                except OverflowError:
                    return CellError("#RANGE!")
                # ZeroDivisionError is intentionally propagated so the engine can map it to #DIV/0!
                if isinstance(result, complex):
                    return CellError("#NUM!")
                return self._normalize_ieee_special(_normalize_negative_zero(result))
            if op == "&":
                left_s = self._format_for_string(l)
                right_s = self._format_for_string(r)
                return left_s + right_s
            if op == ">":  return 1.0 if l > r else 0.0
            if op == "<":  return 1.0 if l < r else 0.0
            if op == ">=": return 1.0 if l >= r else 0.0
            if op == "<=": return 1.0 if l <= r else 0.0
            if op == "==": return 1.0 if l == r else 0.0
            if op == "!=": return 1.0 if l != r else 0.0
            raise ValueError(f"Unknown op {op!r}")

        if isinstance(node, _AstRef):
            if resolver is None:
                raise RuntimeError("Cell reference requires a CubeResolver")
            cube_name = getattr(node, "cube_name", None)
            if _RULE_EVAL_DEBUG:
                print(f"DEBUG _eval _AstRef: dim_name={node.dim_name!r}, item_name={node.item_name!r}, cube_name={cube_name!r}")
            try:
                result = self._with_seq_keyword_guard(
                    resolver,
                    getattr(node, "allow_seq_keywords", False),
                    resolver.resolve_ref,
                    node.dim_name,
                    node.item_name,
                    addr,
                    cube_name,
                )
            except (KeyError, ValueError) as exc:
                # RuleValidationError must propagate so callers can surface
                # authoring-time validation failures (e.g. invalid dynamic bounds).
                if isinstance(exc, RuleValidationError):
                    raise
                # Reference points to a deleted or otherwise invalid object.
                return CellError("#REF!")
            if _RULE_EVAL_DEBUG:
                print(f"DEBUG _eval _AstRef: result={result!r}")
            # Handle list result (e.g., from *.* wildcard)
            if isinstance(result, list):
                return result
            # Normalize IEEE special values from cell lookups
            return self._normalize_ieee_special(result)

        if isinstance(node, _AstMultiRef):
            if resolver is None:
                raise RuntimeError("Cell reference requires a CubeResolver")
            cube_name = getattr(node, "cube_name", None)
            try:
                result = self._with_seq_keyword_guard(
                    resolver,
                    getattr(node, "allow_seq_keywords", False),
                    resolver.resolve_multi_ref,
                    node.pairs,
                    addr,
                    cube_name,
                )
            except (KeyError, ValueError) as exc:
                if isinstance(exc, RuleValidationError):
                    raise
                return CellError("#REF!")
            # Normalize IEEE special values in list results
            if isinstance(result, list):
                return [self._normalize_ieee_special(v) for v in result]
            return self._normalize_ieee_special(result)

        if hasattr(node, 'dynamic_calls') and hasattr(node, 'pairs'):
            # Dynamic multi-ref should be handled by functions like SLICE/REF
            # If evaluated directly, treat static pairs like a regular multi-ref
            # Dynamic calls cannot be resolved without context
            if resolver is None:
                raise RuntimeError("Cell reference requires a CubeResolver")
            if node.pairs:
                cube_name = getattr(node, "cube_name", None)
                try:
                    return self._with_seq_keyword_guard(
                        resolver,
                        False,
                        resolver.resolve_multi_ref,
                        node.pairs,
                        addr,
                        cube_name,
                    )
                except (KeyError, ValueError) as exc:
                    if isinstance(exc, RuleValidationError):
                        raise
                    return CellError("#REF!")
            raise ValueError("_AstDynamicMultiRef with only dynamic calls must be used inside SLICE/REF")

        if isinstance(node, _AstCtxRef):
            if resolver is None:
                raise RuleValidationError(f"Contextual ref {node.name!r} requires a CubeResolver")
            return resolver.resolve_ctx(node.name, addr)

        if isinstance(node, _AstCall):
            return self._eval_call(node, resolver, addr, volatile_seq)

        raise ValueError(f"Unknown AST node {type(node)}")

    def _call_signature(self, node: _AstCall) -> str:
        def _fmt(n: Any) -> str:
            if isinstance(n, _AstNum):
                return f"num:{n.v}"
            if isinstance(n, _AstStr):
                return f"str:{n.s}"
            if isinstance(n, _AstRef):
                cube = f"{n.cube_name}::" if n.cube_name else ""
                return f"ref:{cube}{n.dim_name}.{n.item_name}"
            if isinstance(n, _AstMultiRef):
                cube = f"{n.cube_name}::" if n.cube_name else ""
                pairs = ";".join(f"{dim}.{item}" for dim, item in n.pairs)
                return f"mref:{cube}{pairs}"
            if isinstance(n, _AstCtxRef):
                return f"ctx:{n.name}"
            if isinstance(n, _AstCall):
                return f"call:{n.fn}({','.join(_fmt(a) for a in n.args)})"
            if isinstance(n, _AstBinOp):
                return f"bin:{n.op}:{_fmt(n.l)}:{_fmt(n.r)}"
            if isinstance(n, _AstUnOp):
                return f"un:{n.op}:{_fmt(n.operand)}"
            return repr(n)

        return f"{node.fn}({','.join(_fmt(a) for a in node.args)})"

    @staticmethod
    def _xls_pairs_from_arg(arg: Any) -> tuple[list[tuple[str, str]], str | None]:
        if isinstance(arg, _AstRef):
            return [(arg.dim_name, arg.item_name)], getattr(arg, "cube_name", None)
        if isinstance(arg, _AstMultiRef):
            return list(arg.pairs), getattr(arg, "cube_name", None)
        raise ValueError("Expected a reference argument")

    @staticmethod
    def _xls_expand_selector(resolver: CubeResolver, dim_name: str, selector: str) -> list[str]:
        def _normalize_bound(text: str) -> str:
            t = text.strip()
            if "." in t:
                maybe_dim, maybe_item = t.split(".", 1)
                if maybe_dim.strip().lower() == dim_name.lower() and maybe_item.strip():
                    return maybe_item.strip()
            return t

        all_items = resolver.dim_item_names(dim_name)
        if selector == "*":
            return list(all_items)
        if ".." not in selector:
            return [_normalize_bound(selector)]

        start, end = selector.split("..", 1)
        start = _normalize_bound(start)
        end = _normalize_bound(end)
        if not start or not end:
            raise ValueError(f"Invalid range selector {selector!r}")

        lowered = [name.lower() for name in all_items]
        try:
            i1 = lowered.index(start.lower())
            i2 = lowered.index(end.lower())
        except ValueError as exc:
            raise KeyError(f"Unknown selector in range {selector!r} for dimension {dim_name!r}") from exc

        lo, hi = sorted((i1, i2))
        return all_items[lo : hi + 1]

    def _eval_xls_index(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        if resolver is None:
            raise RuntimeError("XLS_INDEX requires a CubeResolver")
        if len(node.args) not in (2, 3):
            raise ValueError("xls_index requires 2 or 3 arguments")

        pairs, cube_name = self._xls_pairs_from_arg(node.args[0])

        row_num_raw = self._eval(node.args[1], resolver, addr)
        if self._is_error(row_num_raw):
            return row_num_raw
        row_num = int(float(row_num_raw))

        col_num = 1
        if len(node.args) == 3:
            col_num_raw = self._eval(node.args[2], resolver, addr)
            if self._is_error(col_num_raw):
                return col_num_raw
            col_num = int(float(col_num_raw))

        if row_num < 1 or col_num < 1:
            raise ValueError("xls_index row/column numbers must be >= 1")

        expanded: list[tuple[str, list[str]]] = [
            (dim_name, self._xls_expand_selector(resolver, dim_name, selector))
            for dim_name, selector in pairs
        ]

        if not expanded:
            raise ValueError("xls_index requires a non-empty reference")

        row_dim_name, row_items = expanded[0]
        if row_num > len(row_items):
            raise ValueError("xls_index row number is out of range")
        selected_pairs: list[tuple[str, str]] = [(row_dim_name, row_items[row_num - 1])]

        if len(expanded) >= 2:
            col_dim_name, col_items = expanded[1]
            if col_num > len(col_items):
                raise ValueError("xls_index column number is out of range")
            selected_pairs.append((col_dim_name, col_items[col_num - 1]))
            tail = expanded[2:]
        else:
            if col_num != 1:
                raise ValueError("xls_index with a 1D reference requires column number = 1")
            tail = []

        for dim_name, choices in tail:
            if len(choices) != 1:
                raise ValueError("xls_index supports up to two varying dimensions")
            selected_pairs.append((dim_name, choices[0]))

        return resolver.resolve_multi_ref(selected_pairs, addr, cube_name)

    def _eval_xls_offset(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        if resolver is None:
            raise RuntimeError("XLS_OFFSET requires a CubeResolver")
        if len(node.args) not in (3, 5):
            raise ValueError("xls_offset requires 3 or 5 arguments")

        pairs, cube_name = self._xls_pairs_from_arg(node.args[0])
        if not pairs:
            raise ValueError("xls_offset requires a reference argument")

        rows_raw = self._eval(node.args[1], resolver, addr)
        cols_raw = self._eval(node.args[2], resolver, addr)
        if self._is_error(rows_raw):
            return rows_raw
        if self._is_error(cols_raw):
            return cols_raw
        row_delta = int(float(rows_raw))
        col_delta = int(float(cols_raw))

        height = 1
        width = 1
        if len(node.args) == 5:
            height_raw = self._eval(node.args[3], resolver, addr)
            width_raw = self._eval(node.args[4], resolver, addr)
            if self._is_error(height_raw):
                return height_raw
            if self._is_error(width_raw):
                return width_raw
            height = int(float(height_raw))
            width = int(float(width_raw))
            if height == 0 or width == 0:
                raise ValueError("xls_offset height and width cannot be zero")

        by_dim: dict[str, str] = {dim_name: selector for dim_name, selector in pairs}
        row_dim_name = next((name for name in by_dim if name.lower() == "row"), None)
        col_dim_name = next((name for name in by_dim if name.lower() == "column"), None)

        if row_dim_name is None or col_dim_name is None:
            raise ValueError("xls_offset requires Row and Column dimensions in the reference")

        row_selector = by_dim[row_dim_name]
        col_selector = by_dim[col_dim_name]
        if ".." in row_selector or row_selector == "*":
            raise ValueError("xls_offset Row reference must be a single item")
        if ".." in col_selector or col_selector == "*":
            raise ValueError("xls_offset Column reference must be a single item")

        row_items = resolver.dim_item_names(row_dim_name)
        col_items = resolver.dim_item_names(col_dim_name)
        row_lookup = {name.lower(): i for i, name in enumerate(row_items)}
        col_lookup = {name.lower(): i for i, name in enumerate(col_items)}
        if row_selector.lower() not in row_lookup:
            raise KeyError(f"Unknown row selector {row_selector!r}")
        if col_selector.lower() not in col_lookup:
            raise KeyError(f"Unknown column selector {col_selector!r}")

        target_row_idx = row_lookup[row_selector.lower()] + row_delta
        target_col_idx = col_lookup[col_selector.lower()] + col_delta
        if height > 0:
            row_start_idx = target_row_idx
            row_end_idx = target_row_idx + height - 1
        else:
            row_start_idx = target_row_idx + height + 1
            row_end_idx = target_row_idx

        if width > 0:
            col_start_idx = target_col_idx
            col_end_idx = target_col_idx + width - 1
        else:
            col_start_idx = target_col_idx + width + 1
            col_end_idx = target_col_idx

        if row_start_idx < 0 or row_end_idx >= len(row_items):
            return 0.0 if len(node.args) == 3 else []
        if col_start_idx < 0 or col_end_idx >= len(col_items):
            return 0.0 if len(node.args) == 3 else []

        row_span = row_items[row_start_idx : row_end_idx + 1]
        col_span = col_items[col_start_idx : col_end_idx + 1]

        def _resolve_at(r_item: str, c_item: str) -> Any:
            shifted_pairs: list[tuple[str, str]] = []
            for dim_name, selector in pairs:
                if dim_name == row_dim_name:
                    shifted_pairs.append((dim_name, r_item))
                elif dim_name == col_dim_name:
                    shifted_pairs.append((dim_name, c_item))
                else:
                    shifted_pairs.append((dim_name, selector))
            return resolver.resolve_multi_ref(shifted_pairs, addr, cube_name)

        if len(row_span) == 1 and len(col_span) == 1:
            return _resolve_at(row_span[0], col_span[0])

        values: list[Any] = []
        for r_item in row_span:
            for c_item in col_span:
                values.append(_resolve_at(r_item, c_item))
        return values

    def _eval_xls_match(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        if resolver is None:
            raise RuntimeError("XLS_MATCH requires a CubeResolver")
        if len(node.args) not in (2, 3):
            raise ValueError("xls_match requires 2 or 3 arguments")

        lookup_value = self._eval(node.args[0], resolver, addr)
        if self._is_error(lookup_value):
            return lookup_value

        pairs, cube_name = self._xls_pairs_from_arg(node.args[1])
        if not pairs:
            raise ValueError("xls_match requires a non-empty lookup array")

        match_type = 1
        if len(node.args) == 3:
            mt = self._eval(node.args[2], resolver, addr)
            if self._is_error(mt):
                return mt
            match_type = int(float(mt))

        if match_type != 0:
            raise ValueError("xls_match currently supports only exact mode (match_type = 0)")

        expanded: list[tuple[str, list[str]]] = [
            (dim_name, self._xls_expand_selector(resolver, dim_name, selector))
            for dim_name, selector in pairs
        ]

        axes = [items for _, items in expanded]
        if not axes:
            raise ValueError("xls_match requires a non-empty lookup array")

        def _eq(a: Any, b: Any) -> bool:
            if isinstance(a, str) and isinstance(b, str):
                return a.lower() == b.lower()
            return a == b

        position = 0

        for combo in itertools.product(*axes):
            position += 1
            selected_pairs = [(dim_name, item_name) for (dim_name, _), item_name in zip(expanded, combo)]
            candidate = resolver.resolve_multi_ref(selected_pairs, addr, cube_name)
            if self._is_error(candidate):
                return candidate
            if _eq(candidate, lookup_value):
                return float(position)

        raise ValueError("xls_match did not find a match")

    def _xls_array_values_from_arg(
        self,
        arg: Any,
        resolver: CubeResolver,
        addr: tuple[str, ...],
    ) -> tuple[list[Any], str | None]:
        pairs, cube_name = self._xls_pairs_from_arg(arg)
        expanded: list[tuple[str, list[str]]] = [
            (dim_name, self._xls_expand_selector(resolver, dim_name, selector))
            for dim_name, selector in pairs
        ]
        if not expanded:
            return [], cube_name

        axes = [items for _, items in expanded]
        values: list[Any] = []
        for combo in itertools.product(*axes):
            selected_pairs = [(dim_name, item_name) for (dim_name, _), item_name in zip(expanded, combo)]
            values.append(resolver.resolve_multi_ref(selected_pairs, addr, cube_name))
        return values, cube_name

    @staticmethod
    def _xls_coerce_bool(v: Any) -> bool:
        if isinstance(v, str):
            text = v.strip().lower()
            if text in ("", "0", "false", "no"):
                return False
            return True
        return bool(v)

    def _xls_table_axes(
        self,
        arg: Any,
        resolver: CubeResolver,
    ) -> tuple[list[tuple[str, list[str]]], str | None, str, str]:
        pairs, cube_name = self._xls_pairs_from_arg(arg)
        expanded: list[tuple[str, list[str]]] = [
            (dim_name, self._xls_expand_selector(resolver, dim_name, selector))
            for dim_name, selector in pairs
        ]
        if not expanded:
            raise ValueError("Lookup table reference cannot be empty")

        row_dim_name = next((dim_name for dim_name, _ in expanded if dim_name.lower() == "row"), None)
        col_dim_name = next((dim_name for dim_name, _ in expanded if dim_name.lower() == "column"), None)

        if row_dim_name is None or col_dim_name is None:
            if len(expanded) < 2:
                raise ValueError("Lookup table requires two dimensions")
            if row_dim_name is None:
                row_dim_name = expanded[0][0]
            if col_dim_name is None:
                col_dim_name = expanded[1][0]

        return expanded, cube_name, row_dim_name, col_dim_name

    def _eval_xls_rows(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        if resolver is None:
            raise RuntimeError("XLS_ROWS requires a CubeResolver")
        if len(node.args) != 1:
            raise ValueError("xls_rows requires 1 argument")

        arg = node.args[0]
        if not isinstance(arg, (_AstRef, _AstMultiRef)):
            values = self._eval(arg, resolver, addr)
            if self._is_error(values):
                return values
            if isinstance(values, list):
                return float(len(values))
            return 1.0

        pairs, _ = self._xls_pairs_from_arg(arg)
        expanded: list[tuple[str, list[str]]] = [
            (dim_name, self._xls_expand_selector(resolver, dim_name, selector))
            for dim_name, selector in pairs
        ]
        if not expanded:
            return 0.0

        row_entry = next(((dim_name, items) for dim_name, items in expanded if dim_name.lower() == "row"), None)
        if row_entry is None:
            row_entry = expanded[0]
        return float(len(row_entry[1]))

    def _eval_xls_columns(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        if resolver is None:
            raise RuntimeError("XLS_COLUMNS requires a CubeResolver")
        if len(node.args) != 1:
            raise ValueError("xls_columns requires 1 argument")

        arg = node.args[0]
        if not isinstance(arg, (_AstRef, _AstMultiRef)):
            values = self._eval(arg, resolver, addr)
            if self._is_error(values):
                return values
            if isinstance(values, list):
                return float(len(values))
            return 1.0

        pairs, _ = self._xls_pairs_from_arg(arg)
        expanded: list[tuple[str, list[str]]] = [
            (dim_name, self._xls_expand_selector(resolver, dim_name, selector))
            for dim_name, selector in pairs
        ]
        if not expanded:
            return 0.0

        col_entry = next(((dim_name, items) for dim_name, items in expanded if dim_name.lower() == "column"), None)
        if col_entry is not None:
            return float(len(col_entry[1]))
        if len(expanded) >= 2:
            return float(len(expanded[1][1]))
        return 1.0

    def _eval_xls_hlookup(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        if resolver is None:
            raise RuntimeError("XLS_HLOOKUP requires a CubeResolver")
        if len(node.args) not in (3, 4):
            raise ValueError("xls_hlookup requires 3 or 4 arguments")

        lookup_value = self._eval(node.args[0], resolver, addr)
        if self._is_error(lookup_value):
            return lookup_value

        expanded, cube_name, row_dim_name, col_dim_name = self._xls_table_axes(node.args[1], resolver)
        by_dim = {dim_name: items for dim_name, items in expanded}
        row_items = by_dim.get(row_dim_name, [])
        col_items = by_dim.get(col_dim_name, [])
        if not row_items or not col_items:
            raise ValueError("xls_hlookup table must include non-empty row and column axes")

        row_index_raw = self._eval(node.args[2], resolver, addr)
        if self._is_error(row_index_raw):
            return row_index_raw
        row_index = int(float(row_index_raw))
        if row_index < 1 or row_index > len(row_items):
            raise ValueError("xls_hlookup row index is out of range")

        range_lookup = True
        if len(node.args) == 4:
            range_lookup_raw = self._eval(node.args[3], resolver, addr)
            if self._is_error(range_lookup_raw):
                return range_lookup_raw
            range_lookup = self._xls_coerce_bool(range_lookup_raw)

        selected_row = row_items[row_index - 1]

        def _resolve_cell(row_item: str, col_item: str) -> Any:
            selected_pairs: list[tuple[str, str]] = []
            for dim_name, items in expanded:
                if dim_name == row_dim_name:
                    selected_pairs.append((dim_name, row_item))
                elif dim_name == col_dim_name:
                    selected_pairs.append((dim_name, col_item))
                else:
                    if len(items) != 1:
                        raise ValueError("xls_hlookup supports up to two varying dimensions")
                    selected_pairs.append((dim_name, items[0]))
            return resolver.resolve_multi_ref(selected_pairs, addr, cube_name)

        def _eq(a: Any, b: Any) -> bool:
            if isinstance(a, str) and isinstance(b, str):
                return a.strip().lower() == b.strip().lower()
            return a == b

        best_col: str | None = None
        best_key: float | None = None
        for col_item in col_items:
            key_val = _resolve_cell(row_items[0], col_item)
            if self._is_error(key_val):
                return key_val
            if _eq(key_val, lookup_value):
                best_col = col_item
                break
            if range_lookup:
                try:
                    key_num = float(key_val)
                    lookup_num = float(lookup_value)
                except Exception:
                    continue
                if key_num <= lookup_num and (best_key is None or key_num >= best_key):
                    best_key = key_num
                    best_col = col_item

        if best_col is None:
            raise ValueError("xls_hlookup did not find a match")
        return _resolve_cell(selected_row, best_col)

    def _eval_xls_vlookup(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        if resolver is None:
            raise RuntimeError("XLS_VLOOKUP requires a CubeResolver")
        if len(node.args) not in (3, 4):
            raise ValueError("xls_vlookup requires 3 or 4 arguments")

        lookup_value = self._eval(node.args[0], resolver, addr)
        if self._is_error(lookup_value):
            return lookup_value

        expanded, cube_name, row_dim_name, col_dim_name = self._xls_table_axes(node.args[1], resolver)
        by_dim = {dim_name: items for dim_name, items in expanded}
        row_items = by_dim.get(row_dim_name, [])
        col_items = by_dim.get(col_dim_name, [])
        if not row_items or not col_items:
            raise ValueError("xls_vlookup table must include non-empty row and column axes")

        col_index_raw = self._eval(node.args[2], resolver, addr)
        if self._is_error(col_index_raw):
            return col_index_raw
        col_index = int(float(col_index_raw))
        if col_index < 1 or col_index > len(col_items):
            raise ValueError("xls_vlookup column index is out of range")

        range_lookup = True
        if len(node.args) == 4:
            range_lookup_raw = self._eval(node.args[3], resolver, addr)
            if self._is_error(range_lookup_raw):
                return range_lookup_raw
            range_lookup = self._xls_coerce_bool(range_lookup_raw)

        selected_col = col_items[col_index - 1]

        def _resolve_cell(row_item: str, col_item: str) -> Any:
            selected_pairs: list[tuple[str, str]] = []
            for dim_name, items in expanded:
                if dim_name == row_dim_name:
                    selected_pairs.append((dim_name, row_item))
                elif dim_name == col_dim_name:
                    selected_pairs.append((dim_name, col_item))
                else:
                    if len(items) != 1:
                        raise ValueError("xls_vlookup supports up to two varying dimensions")
                    selected_pairs.append((dim_name, items[0]))
            return resolver.resolve_multi_ref(selected_pairs, addr, cube_name)

        def _eq(a: Any, b: Any) -> bool:
            if isinstance(a, str) and isinstance(b, str):
                return a.strip().lower() == b.strip().lower()
            return a == b

        best_row: str | None = None
        best_key: float | None = None
        for row_item in row_items:
            key_val = _resolve_cell(row_item, col_items[0])
            if self._is_error(key_val):
                return key_val
            if _eq(key_val, lookup_value):
                best_row = row_item
                break
            if range_lookup:
                try:
                    key_num = float(key_val)
                    lookup_num = float(lookup_value)
                except Exception:
                    continue
                if key_num <= lookup_num and (best_key is None or key_num >= best_key):
                    best_key = key_num
                    best_row = row_item

        if best_row is None:
            raise ValueError("xls_vlookup did not find a match")
        return _resolve_cell(best_row, selected_col)

    def _eval_xls_sum(self, node: _AstCall, resolver: CubeResolver, addr: tuple[str, ...]) -> Any:
        """Evaluate XLS_SUM for array references."""
        arg = node.args[0]
        values, _ = self._xls_array_values_from_arg(arg, resolver, addr)
        for v in values:
            if self._is_error(v):
                return v
        total = 0.0
        for v in values:
            if v is not None:
                try:
                    total += float(v)
                except (ValueError, TypeError):
                    continue  # Treat non-numeric text as 0
        return self._normalize_ieee_special(total)

    def _eval_xls_xirr(self, node: _AstCall, resolver: CubeResolver, addr: tuple[str, ...]) -> Any:
        """Evaluate XLS_XIRR function."""
        if len(node.args) not in (2, 3):
            raise ValueError("xls_xirr requires 2 or 3 arguments")
        values, _ = self._xls_array_values_from_arg(node.args[0], resolver, addr)
        dates, _ = self._xls_array_values_from_arg(node.args[1], resolver, addr)
        for v in values + dates:
            if self._is_error(v):
                return v
        xirr_args: list[Any] = [values, dates]
        if len(node.args) == 3:
            guess = self._eval(node.args[2], resolver, addr)
            if self._is_error(guess):
                return guess
            xirr_args.append(guess)
        return eval_xls_function("XLS_XIRR", xirr_args, eval_node=lambda n: n)

    def _eval_xls_npv(self, node: _AstCall, resolver: CubeResolver, addr: tuple[str, ...]) -> Any:
        """Evaluate XLS_NPV function."""
        if len(node.args) < 2:
            raise ValueError("xls_npv requires at least 2 arguments")
        rate = self._eval(node.args[0], resolver, addr)
        if self._is_error(rate):
            return rate
        npv_args: list[Any] = [rate]
        if len(node.args) == 2 and isinstance(node.args[1], (_AstRef, _AstMultiRef)):
            values, _ = self._xls_array_values_from_arg(node.args[1], resolver, addr)
            for v in values:
                if self._is_error(v):
                    return v
            npv_args.append(values)
        else:
            for arg in node.args[1:]:
                v = self._eval(arg, resolver, addr)
                if self._is_error(v):
                    return v
                npv_args.append(v)
        return eval_xls_function("XLS_NPV", npv_args, eval_node=lambda n: n)

    def _eval_xls_irr(self, node: _AstCall, resolver: CubeResolver, addr: tuple[str, ...]) -> Any:
        """Evaluate XLS_IRR function."""
        if len(node.args) not in (1, 2):
            raise ValueError("xls_irr requires 1 or 2 arguments")
        if not isinstance(node.args[0], (_AstRef, _AstMultiRef)):
            raise ValueError("xls_irr requires an array reference as first argument")
        values, _ = self._xls_array_values_from_arg(node.args[0], resolver, addr)
        for v in values:
            if self._is_error(v):
                return v
        irr_args: list[Any] = [values]
        if len(node.args) == 2:
            guess = self._eval(node.args[1], resolver, addr)
            if self._is_error(guess):
                return guess
            irr_args.append(guess)
        return eval_xls_function("XLS_IRR", irr_args, eval_node=lambda n: n)

    # Wrapper methods for XLS functions that need special pre-processing
    def _eval_xls_sum_wrapper(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """Wrapper for XLS_SUM with array reference validation."""
        # Only use optimized array sum when we have a resolver and a single ref argument
        if resolver is None or len(node.args) != 1:
            # Fall back to generic XLS function handler
            return eval_xls_function(
                "XLS_SUM",
                node.args,
                eval_node=lambda n: self._eval(n, resolver, addr),
            )
        arg = node.args[0]
        if not isinstance(arg, (_AstRef, _AstMultiRef)):
            # Fall back to generic XLS function handler
            return eval_xls_function(
                "XLS_SUM",
                node.args,
                eval_node=lambda n: self._eval(n, resolver, addr),
            )
        return self._eval_xls_sum(node, resolver, addr)

    def _eval_xls_xirr_wrapper(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """Wrapper for XLS_XIRR with resolver validation."""
        resolver = self._require_resolver("XLS_XIRR", resolver)
        return self._eval_xls_xirr(node, resolver, addr)

    def _eval_xls_npv_wrapper(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """Wrapper for XLS_NPV with resolver validation."""
        resolver = self._require_resolver("XLS_NPV", resolver)
        return self._eval_xls_npv(node, resolver, addr)

    def _eval_xls_irr_wrapper(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """Wrapper for XLS_IRR with resolver validation."""
        resolver = self._require_resolver("XLS_IRR", resolver)
        return self._eval_xls_irr(node, resolver, addr)

    # =========================================================================
    # Function handlers for the dispatch table
    # =========================================================================

    def _fn_if(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """IF(condition, then_value, else_value)."""
        self._require_argc(node, exact=3)
        cond = self._eval(node.args[0], resolver, addr)
        if self._is_error(cond):
            return cond
        if isinstance(cond, str):
            return CellError("#VALUE!")
        branch = node.args[1] if cond else node.args[2]
        return self._eval(branch, resolver, addr)

    # Math functions
    def _fn_abs(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(abs(v))

    def _fn_round(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        places = 0
        if len(node.args) == 2:
            p = self._eval(node.args[1], resolver, addr)
            if self._is_error(p):
                return p
            p = self._coerce_number(p)
            if self._is_error(p):
                return p
            places = max(0, int(p))
        factor = 10 ** places
        scaled = v * factor
        if scaled >= 0:
            result = math.floor(scaled + 0.5) / factor
        else:
            result = math.ceil(scaled - 0.5) / factor
        return self._normalize_ieee_special(float(result))

    def _fn_pi(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=0)
        return math.pi

    def _fn_ln(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        if v <= 0:
            return CellError("#NUM!")
        return self._normalize_ieee_special(cr_math.log(v))

    def _fn_log(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        if v <= 0:
            return CellError("#NUM!")
        if len(node.args) == 2:
            base = self._eval(node.args[1], resolver, addr)
            if self._is_error(base):
                return base
            base = self._coerce_number(base)
            if self._is_error(base):
                return base
            if base <= 0 or base == 1:
                return CellError("#NUM!")
            return self._normalize_ieee_special(cr_math.log(v) / cr_math.log(base))
        return self._normalize_ieee_special(cr_math.log10(v))

    def _fn_log10(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        if v <= 0:
            return CellError("#NUM!")
        return self._normalize_ieee_special(cr_math.log10(v))

    def _fn_exp(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.exp(v))

    def _fn_sqrt(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        if v < 0:
            return CellError("#NUM!")
        return self._normalize_ieee_special(cr_math.sqrt(v))

    def _fn_power(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        base = self._eval(node.args[0], resolver, addr)
        if self._is_error(base):
            return base
        base = self._coerce_number(base)
        if self._is_error(base):
            return base
        exp = self._eval(node.args[1], resolver, addr)
        if self._is_error(exp):
            return exp
        exp = self._coerce_number(exp)
        if self._is_error(exp):
            return exp
        # Match spreadsheet semantics: 0 raised to a negative exponent is a division-by-zero error.
        if base == 0 and exp < 0:
            return CellError("#DIV/0!")
        result = cr_math.pow(base, exp)
        if isinstance(result, complex):
            return CellError("#NUM!")
        return self._normalize_ieee_special(result)

    def _fn_sin(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.sin(v))

    def _fn_cos(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.cos(v))

    def _fn_tan(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.tan(v))

    def _fn_asin(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.asin(v))

    def _fn_acos(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.acos(v))

    def _fn_atan(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.atan(v))

    def _fn_atan2(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        y = self._eval(node.args[0], resolver, addr)
        if self._is_error(y):
            return y
        y = self._coerce_number(y)
        if self._is_error(y):
            return y
        x = self._eval(node.args[1], resolver, addr)
        if self._is_error(x):
            return x
        x = self._coerce_number(x)
        if self._is_error(x):
            return x
        return self._normalize_ieee_special(cr_math.atan2(y, x))

    def _fn_radians(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.radians(v))

    def _fn_degrees(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return self._normalize_ieee_special(cr_math.degrees(v))

    def _fn_sign(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return 1.0 if v > 0 else (-1.0 if v < 0 else 0.0)

    def _fn_int(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        return float(int(v))

    def _fn_mod(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        dividend = self._eval(node.args[0], resolver, addr)
        if self._is_error(dividend):
            return dividend
        dividend = self._coerce_number(dividend)
        if self._is_error(dividend):
            return dividend
        divisor = self._eval(node.args[1], resolver, addr)
        if self._is_error(divisor):
            return divisor
        divisor = self._coerce_number(divisor)
        if self._is_error(divisor):
            return divisor
        if divisor == 0:
            raise ZeroDivisionError("#DIV/0!")
        return self._normalize_ieee_special(dividend % divisor)

    def _fn_quotient(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        dividend = self._eval(node.args[0], resolver, addr)
        if self._is_error(dividend):
            return dividend
        dividend = self._coerce_number(dividend)
        if self._is_error(dividend):
            return dividend
        divisor = self._eval(node.args[1], resolver, addr)
        if self._is_error(divisor):
            return divisor
        divisor = self._coerce_number(divisor)
        if self._is_error(divisor):
            return divisor
        if divisor == 0:
            raise ZeroDivisionError("#DIV/0!")
        try:
            return self._normalize_ieee_special(float(int(dividend / divisor)))
        except OverflowError:
            return CellError("#RANGE!")

    def _fn_roundup(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        places = 0
        if len(node.args) == 2:
            p = self._eval(node.args[1], resolver, addr)
            if self._is_error(p):
                return p
            p = self._coerce_number(p)
            if self._is_error(p):
                return p
            places = max(0, int(p))
        factor = 10 ** places
        return self._normalize_ieee_special(math.ceil(v * factor) / factor if v >= 0 else math.floor(v * factor) / factor)

    def _fn_rounddown(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        places = 0
        if len(node.args) == 2:
            p = self._eval(node.args[1], resolver, addr)
            if self._is_error(p):
                return p
            p = self._coerce_number(p)
            if self._is_error(p):
                return p
            places = max(0, int(p))
        factor = 10 ** places
        return self._normalize_ieee_special(math.floor(v * factor) / factor if v >= 0 else math.ceil(v * factor) / factor)

    def _fn_trunc(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        if len(node.args) >= 2:
            p = self._eval(node.args[1], resolver, addr)
            if self._is_error(p):
                return p
            p = self._coerce_number(p)
            if self._is_error(p):
                return p
            places = int(p)
        else:
            places = 0
        factor = 10 ** places
        return self._normalize_ieee_special(float(int(v * factor)) / factor)

    def _fn_floor(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        if len(node.args) >= 2:
            sig = self._eval(node.args[1], resolver, addr)
            if self._is_error(sig):
                return sig
            sig = self._coerce_number(sig)
            if self._is_error(sig):
                return sig
        else:
            sig = 1.0
        if sig == 0:
            raise ZeroDivisionError("#DIV/0!")
        return self._normalize_ieee_special(math.floor(v / sig) * sig)

    def _fn_ceiling(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        v = self._coerce_number(v)
        if self._is_error(v):
            return v
        if len(node.args) >= 2:
            sig = self._eval(node.args[1], resolver, addr)
            if self._is_error(sig):
                return sig
            sig = self._coerce_number(sig)
            if self._is_error(sig):
                return sig
        else:
            sig = 1.0
        if sig == 0:
            raise ZeroDivisionError("#DIV/0!")
        return self._normalize_ieee_special(math.ceil(v / sig) * sig)

    def _fn_concat(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1)
        parts: list[str] = []
        for arg in node.args:
            v = self._eval(arg, resolver, addr)
            if self._is_error(v):
                return v
            if isinstance(v, list):
                for item in v:
                    if self._is_error(item):
                        return item
                    parts.append(self._format_for_string(item))
            else:
                parts.append(self._format_for_string(v))
        return "".join(parts)

    # Logical functions
    def _fn_and(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1)
        for arg in node.args:
            v = self._eval(arg, resolver, addr)
            if self._is_error(v):
                return v
            if isinstance(v, str):
                return CellError("#VALUE!")
            if not v or v == 0:
                return 0.0
        return 1.0

    def _fn_or(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1)
        for arg in node.args:
            v = self._eval(arg, resolver, addr)
            if self._is_error(v):
                return v
            if isinstance(v, str):
                return CellError("#VALUE!")
            if v and v != 0:
                return 1.0
        return 0.0

    def _fn_not(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        if isinstance(v, str):
            return CellError("#VALUE!")
        return 0.0 if v and v != 0 else 1.0

    def _fn_xor(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1)
        true_count = 0
        for arg in node.args:
            v = self._eval(arg, resolver, addr)
            if self._is_error(v):
                return v
            if isinstance(v, str):
                return CellError("#VALUE!")
            if v and v != 0:
                true_count += 1
        return 1.0 if (true_count % 2 == 1) else 0.0

    def _fn_true(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=0)
        return 1.0

    def _fn_false(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=0)
        return 0.0

    # Type conversion functions
    def _fn_value(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        if v is None:
            return 0.0
        if isinstance(v, bool):
            return 1.0 if v else 0.0
        if isinstance(v, (int, float)):
            return float(v)
        if isinstance(v, str):
            text = v.strip()
            if not text:
                return CellError("#VALUE!")
            try:
                return float(text)
            except ValueError:
                return CellError("#VALUE!")
        return CellError("#VALUE!")

    def _fn_iferror(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        try:
            v = self._eval(node.args[0], resolver, addr)
        except (ZeroDivisionError, ValueError, OverflowError, TypeError):
            return self._eval(node.args[1], resolver, addr)
        if self._is_error(v):
            return self._eval(node.args[1], resolver, addr)
        return v

    def _fn_month(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        try:
            d = _coerce_excel_date(v, "month")
            return float(d.month)
        except (ValueError, TypeError):
            return CellError("#VALUE!")

    def _fn_day(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        try:
            d = _coerce_excel_date(v, "day")
            return float(d.day)
        except (ValueError, TypeError):
            return CellError("#VALUE!")

    def _fn_year(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        try:
            return _excel_year_from_value(v)
        except (ValueError, TypeError):
            return CellError("#VALUE!")

    def _fn_date(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=3)
        vals = []
        for arg in node.args:
            v = self._eval(arg, resolver, addr)
            if self._is_error(v):
                return v
            vals.append(v)
        try:
            from datetime import date as _date
            return _excel_serial_from_date(_date(int(float(vals[0])), int(float(vals[1])), int(float(vals[2]))))
        except (ValueError, TypeError):
            return CellError("#VALUE!")

    def _fn_eomonth(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        v0 = self._eval(node.args[0], resolver, addr)
        if self._is_error(v0):
            return v0
        v1 = self._eval(node.args[1], resolver, addr)
        if self._is_error(v1):
            return v1
        try:
            from datetime import date as _date, timedelta as _timedelta
            start_date = _coerce_excel_date(v0, "eomonth")
            month_delta = int(float(v1))
            month_index = start_date.year * 12 + (start_date.month - 1) + month_delta
            target_year = month_index // 12
            target_month = month_index % 12 + 1
            if target_month == 12:
                next_month = _date(target_year + 1, 1, 1)
            else:
                next_month = _date(target_year, target_month + 1, 1)
            return _excel_serial_from_date(next_month - _timedelta(days=1))
        except (ValueError, TypeError):
            return CellError("#VALUE!")

    def _fn_today(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=0)
        from datetime import date as _date
        return _excel_serial_from_date(_date.today())

    def _fn_now(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=0)
        from datetime import datetime as _datetime
        return _excel_serial_from_datetime(_datetime.now())

    def _fn_weekday(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        rtype = 1
        if len(node.args) >= 2:
            rt = self._eval(node.args[1], resolver, addr)
            if self._is_error(rt):
                return rt
            try:
                rtype = int(float(rt))
            except (ValueError, TypeError):
                return CellError("#VALUE!")
        try:
            d = _coerce_excel_date(v, "weekday")
        except (ValueError, TypeError):
            return CellError("#VALUE!")
        dow = d.weekday()  # Monday=0..Sunday=6
        # ISO 26300 §6.10.21 WEEKDAY type table
        if rtype == 1:
            return float((dow + 1) % 7 + 1)
        elif rtype == 2:
            return float(dow + 1)
        elif rtype == 3:
            return float(dow)
        elif 11 <= rtype <= 17:
            shift = rtype - 11  # 0=Mon..6=Sun
            return float((dow - shift) % 7 + 1)
        else:
            return CellError("#NUM!")

    def _fn_weeknum(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        mode = 1
        if len(node.args) >= 2:
            m = self._eval(node.args[1], resolver, addr)
            if self._is_error(m):
                return m
            try:
                mode = int(float(m))
            except (ValueError, TypeError):
                return CellError("#VALUE!")
        # Validate mode per ISO 26300 §6.10.22 constraints
        if not (mode in (1, 2, 21, 150) or 11 <= mode <= 17):
            return CellError("#NUM!")
        try:
            d = _coerce_excel_date(v, "weeknum")
        except (ValueError, TypeError):
            return CellError("#VALUE!")
        if mode in (21, 150):
            # ISO 8601: first week with the first Thursday
            from datetime import date as _date, timedelta as _td
            jan1 = _date(d.year, 1, 1)
            # Find the Thursday of the first ISO week
            # ISO week 1 is the week containing the first Thursday
            thursday_of_week1 = jan1 + _td(days=(3 - jan1.weekday()) % 7)
            week1_monday = thursday_of_week1 - _td(days=3)
            if d < week1_monday:
                # Belongs to last week of previous year
                prev_dec31 = _date(d.year - 1, 12, 31)
                prev_thursday = prev_dec31 + _td(days=(3 - prev_dec31.weekday()) % 7)
                prev_week1_monday = prev_thursday - _td(days=3)
                return float((d - prev_week1_monday).days // 7 + 1)
            return float((d - week1_monday).days // 7 + 1)
        else:
            # Modes 1,2,11-17: week containing Jan 1 is week 1
            # Week starts on: mode 1=Sunday, mode 2=Monday, 11=Mon,12=Tue,...17=Sun
            if mode == 1:
                week_start_dow = 6  # Sunday
            elif mode == 2 or mode == 11:
                week_start_dow = 0  # Monday
            else:
                week_start_dow = mode - 11  # 12=Tue(1),...,17=Sun(6)
            from datetime import date as _date, timedelta as _td2
            jan1 = _date(d.year, 1, 1)
            jan1_dow = jan1.weekday()  # Mon=0..Sun=6
            # Days from Jan 1 to the start of week 1 (Jan 1 is in week 1)
            # Week 1 starts at Jan 1 minus offset to the week_start day
            offset = (jan1_dow - week_start_dow) % 7
            week1_start = jan1 - _td2(days=offset)
            if d < week1_start:
                # In the last week of the previous year
                prev_jan1 = _date(d.year - 1, 1, 1)
                prev_jan1_dow = prev_jan1.weekday()
                prev_offset = (prev_jan1_dow - week_start_dow) % 7
                prev_week1_start = prev_jan1 - _td2(days=prev_offset)
                return float((d - prev_week1_start).days // 7 + 1)
            return float((d - week1_start).days // 7 + 1)

    def _fn_rand(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=0)
        return random.random()

    def _fn_randbetween(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        low = self._eval(node.args[0], resolver, addr)
        if self._is_error(low):
            return low
        high = self._eval(node.args[1], resolver, addr)
        if self._is_error(high):
            return high
        if not isinstance(low, (int, float)):
            return CellError("#VALUE!")
        if not isinstance(high, (int, float)):
            return CellError("#VALUE!")
        low_int = math.floor(low)
        high_int = math.floor(high)
        if low_int > high_int:
            raise ValueError("RANDBETWEEN requires bottom <= top")
        return float(random.randint(low_int, high_int))

    def _fn_join(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        list_arg = self._eval(node.args[0], resolver, addr)
        if self._is_error(list_arg):
            return list_arg
        delimiter = self._eval(node.args[1], resolver, addr)
        if self._is_error(delimiter):
            return delimiter
        if not isinstance(list_arg, list):
            raise ValueError("JOIN first argument must be a list")
        delim_str = self._format_for_string(delimiter)
        str_items = [self._format_for_string(item) for item in list_arg]
        return delim_str.join(str_items)

    # Metadata functions (LABEL, POS, POSMAX)
    def _resolve_dim_arg(
        self, arg: Any, resolver: CubeResolver | None, addr: tuple[str, ...]
    ) -> tuple[str | None, str | None]:
        """Resolve dimension argument from various AST node types."""
        dim_name: str | None = None
        cube_name: str | None = None
        if isinstance(arg, _AstCtxRef):
            dim_name = arg.name
        elif isinstance(arg, _AstStr):
            dim_name = arg.s
        elif isinstance(arg, _AstRef):
            dim_name = arg.dim_name
            cube_name = getattr(arg, "cube_name", None)
        elif isinstance(arg, _AstMultiRef) and arg.pairs:
            dim_name = arg.pairs[-1][0]
            cube_name = getattr(arg, "cube_name", None)
        else:
            if resolver is None:
                raise RuntimeError("Dynamic dimension argument requires a CubeResolver")
            resolved = self._eval(arg, resolver, addr)
            if self._is_error(resolved):
                return resolved, None  # type: ignore[return-value]
            dim_name = str(resolved)
        return dim_name, cube_name

    def _fn_label(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        resolver = self._require_resolver("LABEL", resolver)
        self._require_argc(node, min_args=0, max_args=1)
        if not node.args:
            return resolver.label_for_addr(addr)
        dim_name, cube_name = self._resolve_dim_arg(node.args[0], resolver, addr)
        if self._is_error(dim_name):
            return dim_name
        dim_name = (dim_name or "").strip()
        if not dim_name:
            raise ValueError("LABEL dimension argument cannot be empty")
        return resolver.label_for_dim(dim_name, addr, cube_name)

    def _fn_pos(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        resolver = self._require_resolver("POS", resolver)
        self._require_argc(node, exact=1)
        dim_name, cube_name = self._resolve_dim_arg(node.args[0], resolver, addr)
        if self._is_error(dim_name):
            return dim_name
        dim_name = (dim_name or "").strip()
        if not dim_name:
            raise ValueError("POS dimension argument cannot be empty")
        return resolver.pos_for_dim(dim_name, addr, cube_name)

    def _fn_posmax(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        resolver = self._require_resolver("POSMAX", resolver)
        self._require_argc(node, exact=1)
        dim_name, cube_name = self._resolve_dim_arg(node.args[0], resolver, addr)
        if self._is_error(dim_name):
            return dim_name
        dim_name = (dim_name or "").strip()
        if not dim_name:
            raise ValueError("POSMAX dimension argument cannot be empty")
        return resolver.posmax_for_dim(dim_name, addr, cube_name)

    # Hierarchy navigation functions
    def _resolve_item_ref(
        self, arg: Any, func_name: str
    ) -> tuple[str | None, str | None, str | None]:
        """Resolve dimension item reference from AST node."""
        dim_name: str | None = None
        item_name: str | None = None
        cube_name: str | None = None
        if isinstance(arg, _AstRef):
            dim_name = arg.dim_name
            item_name = arg.item_name
            cube_name = getattr(arg, "cube_name", None)
        elif isinstance(arg, _AstMultiRef) and arg.pairs:
            dim_name, item_name = arg.pairs[-1]
            cube_name = getattr(arg, "cube_name", None)
        elif isinstance(arg, (_AstCtxRef, _AstStr)):
            raise ValueError(f"{func_name} requires a specific dimension item (e.g., Dim.Item), not just dimension name")
        else:
            raise ValueError(f"{func_name} requires a specific dimension item reference (e.g., Dim.Item)")
        return dim_name, item_name, cube_name

    def _fn_ance(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        resolver = self._require_resolver("ANCE", resolver)
        self._require_argc(node, exact=1)
        dim_name, item_name, cube_name = self._resolve_item_ref(node.args[0], "ANCE")
        if not dim_name or not item_name:
            raise ValueError("ANCE requires both dimension and item (e.g., Dim.Item)")
        return resolver.ancestors_for_dim_item(dim_name, item_name, addr, cube_name)

    def _fn_peer(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        resolver = self._require_resolver("PEER", resolver)
        self._require_argc(node, exact=1)
        dim_name, item_name, cube_name = self._resolve_item_ref(node.args[0], "PEER")
        if not dim_name or not item_name:
            raise ValueError("PEER requires both dimension and item (e.g., Dim.Item)")
        return resolver.peers_for_dim_item(dim_name, item_name, addr, cube_name)

    def _fn_sibl(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        resolver = self._require_resolver("SIBL", resolver)
        self._require_argc(node, exact=1)
        dim_name, item_name, cube_name = self._resolve_item_ref(node.args[0], "SIBL")
        if not dim_name or not item_name:
            raise ValueError("SIBL requires both dimension and item (e.g., Dim.Item)")
        return resolver.siblings_for_dim_item(dim_name, item_name, addr, cube_name)

    # String functions
    def _fn_len(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        return v if self._is_error(v) else float(len(self._format_for_string(v)))

    def _fn_trim(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        return v if self._is_error(v) else self._format_for_string(v).strip()

    def _fn_left(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        num_chars = 1
        if len(node.args) == 2:
            num_chars_raw = self._eval(node.args[1], resolver, addr)
            if self._is_error(num_chars_raw):
                return num_chars_raw
            nc = self._coerce_number(num_chars_raw)
            if self._is_error(nc):
                return nc
            num_chars = int(nc)
            if num_chars < 0:
                return CellError("#VALUE!")
        return self._format_for_string(v)[:num_chars]

    def _fn_right(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=1, max_args=2)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        num_chars = 1
        if len(node.args) == 2:
            num_chars_raw = self._eval(node.args[1], resolver, addr)
            if self._is_error(num_chars_raw):
                return num_chars_raw
            nc = self._coerce_number(num_chars_raw)
            if self._is_error(nc):
                return nc
            num_chars = int(nc)
            if num_chars < 0:
                return CellError("#VALUE!")
        text = self._format_for_string(v)
        return text[-num_chars:] if num_chars > 0 else ""

    def _fn_rept(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=2)
        text = self._eval(node.args[0], resolver, addr)
        if self._is_error(text):
            return text
        num_times = self._eval(node.args[1], resolver, addr)
        if self._is_error(num_times):
            return num_times
        nt = self._coerce_number(num_times)
        if self._is_error(nt):
            return nt
        repeat_count = int(nt)
        if repeat_count < 0:
            return CellError("#VALUE!")
        formatted_text = self._format_for_string(text)
        result = formatted_text * repeat_count
        return result[:1024] if len(result) > 1024 else result

    def _fn_code(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        text = self._eval(node.args[0], resolver, addr)
        if self._is_error(text):
            return text
        formatted_text = self._format_for_string(text)
        if not formatted_text:
            return CellError("#VALUE!")
        return float(ord(formatted_text[0]))

    def _fn_char(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        cn = self._coerce_number(v)
        if self._is_error(cn):
            return cn
        code_num = int(cn)
        if code_num < 1 or code_num > 255:
            return CellError("#VALUE!")
        return chr(code_num)

    def _fn_upper(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        return self._format_for_string(v).upper()

    def _fn_lower(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        return self._format_for_string(v).lower()

    def _fn_proper(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=1)
        v = self._eval(node.args[0], resolver, addr)
        if self._is_error(v):
            return v
        return self._format_for_string(v).title()

    def _fn_substitute(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=3, max_args=4)
        text = self._eval(node.args[0], resolver, addr)
        if self._is_error(text):
            return text
        old_text = self._eval(node.args[1], resolver, addr)
        if self._is_error(old_text):
            return old_text
        new_text = self._eval(node.args[2], resolver, addr)
        if self._is_error(new_text):
            return new_text
        s = self._format_for_string(text)
        old_str = self._format_for_string(old_text)
        new_str = self._format_for_string(new_text)
        if not old_str:
            return s
        if len(node.args) == 4:
            instance_raw = self._eval(node.args[3], resolver, addr)
            if self._is_error(instance_raw):
                return instance_raw
            instance_num = int(self._coerce_number(instance_raw))
            if instance_num < 1:
                return CellError("#VALUE!")
            count = 0
            result = []
            i = 0
            while i < len(s):
                if s[i:i + len(old_str)] == old_str:
                    count += 1
                    if count == instance_num:
                        result.append(new_str)
                        i += len(old_str)
                        result.append(s[i:])
                        return "".join(result)
                    result.append(old_str)
                    i += len(old_str)
                else:
                    result.append(s[i])
                    i += 1
            return s
        return s.replace(old_str, new_str)

    def _fn_replace(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=4)
        text = self._eval(node.args[0], resolver, addr)
        if self._is_error(text):
            return text
        start_raw = self._eval(node.args[1], resolver, addr)
        if self._is_error(start_raw):
            return start_raw
        num_raw = self._eval(node.args[2], resolver, addr)
        if self._is_error(num_raw):
            return num_raw
        new_text = self._eval(node.args[3], resolver, addr)
        if self._is_error(new_text):
            return new_text
        s = self._format_for_string(text)
        start_num = int(self._coerce_number(start_raw))
        num_chars = int(self._coerce_number(num_raw))
        new_str = self._format_for_string(new_text)
        if start_num < 1:
            return CellError("#VALUE!")
        start_idx = start_num - 1
        return s[:start_idx] + new_str + s[start_idx + num_chars:]

    def _fn_mid(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, exact=3)
        text = self._eval(node.args[0], resolver, addr)
        if self._is_error(text):
            return text
        start_raw = self._eval(node.args[1], resolver, addr)
        if self._is_error(start_raw):
            return start_raw
        num_raw = self._eval(node.args[2], resolver, addr)
        if self._is_error(num_raw):
            return num_raw
        s = self._format_for_string(text)
        start_num = int(self._coerce_number(start_raw))
        num_chars = int(self._coerce_number(num_raw))
        if start_num < 1 or num_chars < 0:
            return CellError("#VALUE!")
        start_idx = start_num - 1
        return s[start_idx:start_idx + num_chars]

    def _fn_find(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        self._require_argc(node, min_args=2, max_args=3)
        find_text = self._eval(node.args[0], resolver, addr)
        if self._is_error(find_text):
            return find_text
        within_text = self._eval(node.args[1], resolver, addr)
        if self._is_error(within_text):
            return within_text
        find_str = self._format_for_string(find_text)
        within_str = self._format_for_string(within_text)
        start_num = 1
        if len(node.args) == 3:
            start_raw = self._eval(node.args[2], resolver, addr)
            if self._is_error(start_raw):
                return start_raw
            start_num = int(self._coerce_number(start_raw))
        if start_num < 1:
            return CellError("#VALUE!")
        if not find_str:
            return float(start_num)
        idx = within_str.find(find_str, start_num - 1)
        if idx == -1:
            return CellError("#VALUE!")
        return float(idx + 1)

    def _fn_slice(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """SLICE function: returns a list/array of values from the specified reference(s)."""
        resolver = self._require_resolver("SLICE", resolver)
        self._require_argc(node, min_args=1)
        return self._eval_slice_impl(node, resolver, addr)

    # Color functions for conditional formatting
    # =========================================================================

    # Built-in color palettes (RGB triples 0-255)
    _COLORMAP_PALETTES: dict[str, list[tuple[int, int, int]]] = {
        # Viridis: perceptually uniform, colorblind-friendly
        "viridis": [
            (68, 1, 84), (72, 35, 116), (64, 67, 135), (52, 94, 141),
            (41, 120, 142), (33, 144, 140), (32, 167, 133), (54, 186, 121),
            (98, 203, 103), (141, 215, 86), (189, 223, 64), (254, 231, 37),
        ],
        # Plasma: perceptually uniform, warmer
        "plasma": [
            (13, 8, 135), (56, 11, 124), (84, 14, 113), (105, 17, 101),
            (123, 21, 89), (138, 27, 78), (152, 33, 66), (165, 41, 55),
            (177, 50, 44), (188, 61, 34), (199, 74, 25), (209, 89, 17),
            (219, 106, 11), (228, 125, 9), (236, 147, 15), (243, 170, 26),
            (248, 193, 43), (252, 216, 66), (253, 238, 94), (240, 249, 33),
        ],
        # Coolwarm: diverging (blue to red), good for negative/positive
        "coolwarm": [
            (59, 76, 192), (77, 102, 204), (97, 130, 217), (118, 157, 227),
            (141, 182, 235), (165, 204, 240), (190, 222, 242), (213, 235, 239),
            (229, 241, 230), (241, 243, 220), (248, 241, 203), (254, 235, 180),
            (254, 223, 153), (252, 208, 125), (251, 189, 100), (248, 168, 81),
            (244, 146, 69), (238, 123, 64), (229, 99, 61), (217, 72, 57),
        ],
        # RdYlGn: diverging (red-yellow-green), good for bad-neutral-good
        "rdylgn": [
            (165, 0, 38), (190, 26, 46), (215, 48, 39), (233, 83, 52),
            (241, 115, 72), (248, 149, 97), (253, 182, 113), (254, 206, 133),
            (254, 227, 159), (255, 243, 191), (255, 255, 205), (250, 250, 145),
            (233, 245, 148), (208, 237, 146), (182, 225, 143), (147, 211, 145),
            (114, 193, 142), (81, 176, 141), (51, 159, 136), (26, 152, 80),
            (0, 104, 55),
        ],
        # Blues: sequential blue
        "blues": [
            (247, 251, 255), (227, 238, 249), (207, 225, 242), (182, 213, 232),
            (148, 196, 223), (117, 176, 209), (88, 156, 196), (64, 135, 188),
            (49, 114, 176), (38, 93, 162), (29, 73, 147), (19, 54, 122),
        ],
        # Greens: sequential green
        "greens": [
            (247, 252, 245), (229, 244, 229), (204, 235, 197), (176, 223, 174),
            (141, 211, 150), (114, 197, 138), (88, 181, 111), (66, 163, 93),
            (50, 141, 81), (40, 121, 74), (28, 101, 68), (12, 82, 60),
        ],
        # Grayscale: black to white
        "grayscale": [
            (0, 0, 0), (25, 25, 25), (51, 51, 51), (76, 76, 76),
            (102, 102, 102), (127, 127, 127), (153, 153, 153), (178, 178, 178),
            (204, 204, 204), (229, 229, 229), (255, 255, 255),
        ],
    }

    def _fn_colormap(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """COLORMAP(palette_name, position) - returns hex color string from palette.

        Args:
            palette_name: One of "viridis", "plasma", "coolwarm", "rdylgn",
                         "blues", "greens", "grayscale"
            position: Value 0-1 representing position in the palette

        Returns:
            Hex color string like "#RRGGBB"
        """
        self._require_argc(node, exact=2)

        # Evaluate palette name
        pal_name = self._eval(node.args[0], resolver, addr)
        if isinstance(pal_name, CellError):
            return pal_name
        pal_name = str(pal_name).lower().strip()

        # Evaluate position
        pos = self._eval(node.args[1], resolver, addr)
        if isinstance(pos, CellError):
            return pos
        try:
            pos = float(pos)
        except (TypeError, ValueError):
            return CellError("#VALUE!")

        # Get palette
        palette = self._COLORMAP_PALETTES.get(pal_name)
        if palette is None:
            return CellError("#VALUE!")

        # Clamp position to [0, 1]
        pos = max(0.0, min(1.0, pos))

        # Interpolate within palette
        n = len(palette)
        if n == 0:
            return "#000000"
        if n == 1:
            r, g, b = palette[0]
            return f"#{r:02x}{g:02x}{b:02x}"

        # Map position to palette index with interpolation
        scaled_pos = pos * (n - 1)
        idx = int(scaled_pos)
        t = scaled_pos - idx  # Fractional part for interpolation

        # Get colors to interpolate between
        c1 = palette[min(idx, n - 1)]
        c2 = palette[min(idx + 1, n - 1)]

        # Linear interpolation
        r = int(c1[0] + t * (c2[0] - c1[0]))
        g = int(c1[1] + t * (c2[1] - c1[1]))
        b = int(c1[2] + t * (c2[2] - c1[2]))

        return f"#{r:02x}{g:02x}{b:02x}"

    def _fn_hsv2rgb(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """HSV2RGB(hue, saturation, value) - convert HSV to hex color string.

        Args:
            hue: 0-360 degrees
            saturation: 0-1 (0=gray, 1=full color)
            value: 0-1 (0=black, 1=full brightness)

        Returns:
            Hex color string like "#RRGGBB"
        """
        self._require_argc(node, exact=3)

        h = self._eval(node.args[0], resolver, addr)
        s = self._eval(node.args[1], resolver, addr)
        v = self._eval(node.args[2], resolver, addr)

        if isinstance(h, CellError):
            return h
        if isinstance(s, CellError):
            return s
        if isinstance(v, CellError):
            return v

        try:
            h = float(h) % 360
            s = max(0.0, min(1.0, float(s) / 100.0))
            v = max(0.0, min(1.0, float(v) / 100.0))
        except (TypeError, ValueError):
            return CellError("#VALUE!")

        # HSV to RGB conversion
        c = v * s
        x = c * (1 - abs((h / 60) % 2 - 1))
        m = v - c

        if 0 <= h < 60:
            r, g, b = c, x, 0
        elif 60 <= h < 120:
            r, g, b = x, c, 0
        elif 120 <= h < 180:
            r, g, b = 0, c, x
        elif 180 <= h < 240:
            r, g, b = 0, x, c
        elif 240 <= h < 300:
            r, g, b = x, 0, c
        else:
            r, g, b = c, 0, x

        r = round((r + m) * 255)
        g = round((g + m) * 255)
        b = round((b + m) * 255)

        return f"#{r:02X}{g:02X}{b:02X}"

    def _fn_rgb(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """RGB(red, green, blue) - create hex color string from RGB values.

        Args:
            red: 0-255
            green: 0-255
            blue: 0-255

        Returns:
            Hex color string like "#RRGGBB"
        """
        self._require_argc(node, exact=3)

        r = self._eval(node.args[0], resolver, addr)
        g = self._eval(node.args[1], resolver, addr)
        b = self._eval(node.args[2], resolver, addr)

        if isinstance(r, CellError):
            return r
        if isinstance(g, CellError):
            return g
        if isinstance(b, CellError):
            return b

        try:
            r = int(max(0, min(255, float(r))))
            g = int(max(0, min(255, float(g))))
            b = int(max(0, min(255, float(b))))
        except (TypeError, ValueError):
            return CellError("#VALUE!")

        return f"#{r:02X}{g:02X}{b:02X}"

    def _fn_ref(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        """REF function: returns list of coordinate tuples for debugging.
        Same syntax as SLICE but returns [('dim1.item1', 'dim2.item3'), ...]
        """
        resolver = self._require_resolver("REF", resolver)
        self._require_argc(node, min_args=1)

        dim_constraints: dict[str, list[str]] = {}

        for arg in node.args:
            if isinstance(arg, _AstRef):
                dim_name = arg.dim_name
                item_name = self._resolve_slice_item_name(resolver, dim_name, arg.item_name, addr)
                if dim_name not in dim_constraints:
                    dim_constraints[dim_name] = []
                dim_constraints[dim_name].append(item_name)
            elif isinstance(arg, _AstMultiRef):
                for dim_name, item_name in arg.pairs:
                    item_name = self._resolve_slice_item_name(resolver, dim_name, item_name, addr)
                    if dim_name not in dim_constraints:
                        dim_constraints[dim_name] = []
                    dim_constraints[dim_name].append(item_name)
            elif hasattr(arg, 'dynamic_calls') and hasattr(arg, 'pairs'):
                for dim_name, item_name in arg.pairs:
                    item_name = self._resolve_slice_item_name(resolver, dim_name, item_name, addr)
                    if dim_name not in dim_constraints:
                        dim_constraints[dim_name] = []
                    dim_constraints[dim_name].append(item_name)
                for call in arg.dynamic_calls:
                    call_result = self._eval_call(call, resolver, addr)
                    if isinstance(call_result, list):
                        for item_str in call_result:
                            if isinstance(item_str, str) and "." in item_str:
                                parts = item_str.split(".")
                                if len(parts) == 2:
                                    dim_name, item_name = parts[0], parts[1]
                                    if dim_name not in dim_constraints:
                                        dim_constraints[dim_name] = []
                                    dim_constraints[dim_name].append(item_name)
            elif isinstance(arg, _AstCall):
                call_result = self._eval_call(arg, resolver, addr)
                if isinstance(call_result, list):
                    for item_str in call_result:
                        if isinstance(item_str, str) and "." in item_str:
                            parts = item_str.split(".")
                            if len(parts) == 2:
                                dim_name, item_name = parts[0], parts[1]
                                if dim_name not in dim_constraints:
                                    dim_constraints[dim_name] = []
                                dim_constraints[dim_name].append(item_name)

        if not dim_constraints:
            return []

        # Build cartesian product and return as list of tuples
        # Convert item IDs to labels for readability
        dims = list(dim_constraints.keys())
        item_lists = [dim_constraints[d] for d in dims]
        tuples = []

        # Build a lookup cache: dim_name -> {item_id: item_name}
        item_label_cache: dict[str, dict[str, str]] = {}
        ws = resolver._engine.workspace
        for dim_name in dims:
            item_label_cache[dim_name] = {}
            for d in ws.dimensions.values():
                if d.name.lower() == dim_name.lower():
                    for item in d.items:
                        item_label_cache[dim_name][item.id] = item.name
                    break

        for item_combo in itertools.product(*item_lists):
            labeled_coords = []
            for dim_name, item_val in zip(dims, item_combo):
                # If item_val looks like an ID (starts with 'item_'), look up label
                if isinstance(item_val, str) and item_val.startswith('item_'):
                    label = item_label_cache.get(dim_name, {}).get(item_val, item_val)
                else:
                    label = item_val
                labeled_coords.append(f"{dim_name}.{label}")
            tuples.append(tuple(labeled_coords))

        return tuples if tuples else []

    def _eval_call(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...], volatile_seq: list[int] | None = None) -> Any:
        # Volatile functions (RAND, RANDBETWEEN) use a special cache that persists
        # across paint events but is cleared when dirty nodes are detected.
        # This ensures consistent values during rendering while updating on actual changes.
        # Each call site within a rule gets a unique sequence number for unique values.
        if node.fn in ("RAND", "RANDBETWEEN", "XLS_RAND", "XLS_RANDBETWEEN"):
            volatile_hook = getattr(resolver, "cache_volatile_call", None) if resolver is not None else None
            if volatile_hook is not None:
                signature = self._call_signature(node)
                # Track call sequence to differentiate multiple RAND() calls in same rule
                if volatile_seq is None:
                    volatile_seq = [0]
                volatile_seq[0] += 1
                call_number = volatile_seq[0]

                def _compute() -> Any:
                    return self._eval_call_impl(node, resolver, addr)

                return volatile_hook(node.fn, signature, addr, call_number, _compute)
            # Fallback: direct computation if no volatile cache available
            return self._eval_call_impl(node, resolver, addr)

        # XLS_OFFSET is also volatile but doesn't benefit from caching in the same way
        if node.fn == "XLS_OFFSET":
            return self._eval_call_impl(node, resolver, addr)

        memo_hook = getattr(resolver, "memoize_function_call", None) if resolver is not None else None
        if memo_hook is None:
            return self._eval_call_impl(node, resolver, addr)

        signature = self._call_signature(node)

        def _compute() -> Any:
            return self._eval_call_impl(node, resolver, addr)

        return memo_hook(node.fn, signature, addr, _compute)

    @staticmethod
    def _format_for_string(v: Any) -> str:
        """Format a value for string operations using Excel 'General' format.

        - Whole numbers < 1e12: integer format (no decimal point)
        - Numbers >= 1e12 or < 1e-4: scientific notation (shortest round-trip)
        - Otherwise: shortest round-tripping decimal (str(v))

        Uses up to 17 significant decimal digits to guarantee exact binary64
        round-trip. Fifteen digits suffice for decimal input; seventeen may be
        required to reconstruct an arbitrary binary64 bit pattern exactly.
        """
        if v is None:
            return ""
        if isinstance(v, bool):
            return "TRUE" if v else "FALSE"
        if isinstance(v, (int, float)):
            if isinstance(v, float):
                if v == 0:
                    return "0"
                abs_v = abs(v)
                if abs_v >= 1e12 or abs_v < 1e-4:
                    s = repr(v)
                    if "e" in s or "E" in s:
                        parts = s.replace("E", "e").split("e")
                        mant, exp_s = parts[0], parts[1]
                        if "." in mant:
                            mant = mant.rstrip("0").rstrip(".")
                        ei = int(exp_s)
                        sign = "+" if ei >= 0 else "-"
                        return f"{mant}e{sign}{abs(ei):02d}"
                    neg = s.startswith("-")
                    if neg:
                        s = s[1:]
                    if "." in s:
                        int_part, frac_part = s.split(".")
                    else:
                        int_part, frac_part = s, ""
                    frac_part = frac_part.rstrip("0")
                    digits = int_part + frac_part
                    exp = len(int_part) - 1
                    if len(digits) == 1:
                        mant = digits
                    else:
                        mant = digits[0] + "." + digits[1:]
                    if "." in mant:
                        mant = mant.rstrip("0").rstrip(".")
                    sign = "+" if exp >= 0 else "-"
                    return ("-" if neg else "") + f"{mant}e{sign}{abs(exp):02d}"
                if v.is_integer():
                    return str(int(v))
            return str(v)
        return str(v)

    def _resolve_slice_item_name(
        self, resolver: CubeResolver, dim_name: str, item_name: str, addr: tuple[str, ...]
    ) -> str:
        """Resolve contextual keywords like THIS, PREV, NEXT to actual item names for SLICE."""
        if item_name.upper() not in _SEQ_KEYWORDS:
            return item_name
        try:
            prev_flag = getattr(resolver, "_allow_seq_keywords", False)
            resolver._allow_seq_keywords = True
            try:
                # Find dimension by name
                dim = None
                for d in resolver._engine.workspace.dimensions.values():
                    if d.name.lower() == dim_name.lower():
                        dim = d
                        break
                if dim:
                    cube_dim_ids = resolver._cube.dimension_ids
                    if dim.id in cube_dim_ids:
                        slot = cube_dim_ids.index(dim.id)
                        for item in dim.items:
                            if item.id == addr[slot]:
                                return item.id
            finally:
                resolver._allow_seq_keywords = prev_flag
        except Exception:
            pass
        return item_name

    def _eval_slice_impl(self, node: _AstCall, resolver: CubeResolver, addr: tuple[str, ...]) -> list[Any]:
        """Implementation of SLICE function logic."""
        dim_constraints: dict[str, set[str]] = {}

        def _add_constraint(dim_name: str, item_name: str) -> None:
            if dim_name not in dim_constraints:
                dim_constraints[dim_name] = set()
            dim_constraints[dim_name].add(item_name)

        for arg in node.args:
            if isinstance(arg, _AstRef):
                dim_name = arg.dim_name
                item_name = self._resolve_slice_item_name(resolver, dim_name, arg.item_name, addr)
                _add_constraint(dim_name, item_name)
            elif isinstance(arg, _AstMultiRef):
                for dim_name, item_name in arg.pairs:
                    item_name = self._resolve_slice_item_name(resolver, dim_name, item_name, addr)
                    _add_constraint(dim_name, item_name)
            elif hasattr(arg, 'dynamic_calls') and hasattr(arg, 'pairs'):
                for dim_name, item_name in arg.pairs:
                    item_name = self._resolve_slice_item_name(resolver, dim_name, item_name, addr)
                    _add_constraint(dim_name, item_name)
                for call in arg.dynamic_calls:
                    call_result = self._eval_call(call, resolver, addr)
                    if isinstance(call_result, list):
                        for item_str in call_result:
                            if isinstance(item_str, str) and "." in item_str:
                                parts = item_str.split(".")
                                if len(parts) == 2:
                                    dim_name, item_name = parts[0], parts[1]
                                    _add_constraint(dim_name, item_name)
            elif isinstance(arg, _AstCall):
                call_result = self._eval_call(arg, resolver, addr)
                if isinstance(call_result, list):
                    for item_str in call_result:
                        if isinstance(item_str, str) and "." in item_str:
                            parts = item_str.split(".")
                            if len(parts) == 2:
                                dim_name, item_name = parts[0], parts[1]
                                _add_constraint(dim_name, item_name)

        if not dim_constraints:
            return []

        dims = list(dim_constraints.keys())
        item_lists = [list(dim_constraints[d]) for d in dims]
        values = []
        for item_combo in itertools.product(*item_lists):
            pairs = list(zip(dims, item_combo))
            try:
                vals = resolver.slice_over_ref(pairs, addr)
                if isinstance(vals, list):
                    values.extend(vals)
            except Exception:
                pass
        return values

    def _eval_call_impl(self, node: _AstCall, resolver: CubeResolver | None, addr: tuple[str, ...]) -> Any:
        fn = node.fn

        # Check the function registry first
        registry = self._fn_registry()
        if fn in registry:
            return registry[fn](node, resolver, addr)

        # Fallback to XLS_FUNCTIONS for unhandled Excel functions
        if fn in XLS_FUNCTIONS:
            return eval_xls_function(
                fn,
                node.args,
                eval_node=lambda n: self._eval(n, resolver, addr),
            )

        # Handle aggregate functions with slice semantics
        if fn in ("SUM", "MIN", "MAX", "AVG", "AVERAGE", "COUNT", "COUNTA") and resolver is not None and len(node.args) == 1:
            arg = node.args[0]
            if fn == "SUM":
                agg_fn = getattr(resolver, "sum_over_ref", None)
            else:
                agg_fn = getattr(resolver, "aggregate_over_ref", None)
            if agg_fn is not None:
                if isinstance(arg, _AstRef):
                    cube_name = getattr(arg, "cube_name", None)
                    extra_args = (fn,) if fn != "SUM" else ()
                    result = self._with_seq_keyword_guard(
                        resolver,
                        getattr(arg, "allow_seq_keywords", False),
                        agg_fn,
                        [(arg.dim_name, arg.item_name)],
                        addr,
                        cube_name,
                        *extra_args,
                    )
                    if result is not NotImplemented:
                        return result
                elif isinstance(arg, _AstMultiRef):
                    cube_name = getattr(arg, "cube_name", None)
                    extra_args = (fn,) if fn != "SUM" else ()
                    result = self._with_seq_keyword_guard(
                        resolver,
                        getattr(arg, "allow_seq_keywords", False),
                        agg_fn,
                        list(arg.pairs),
                        addr,
                        cube_name,
                        *extra_args,
                    )
                    if result is not NotImplemented:
                        return result

        # For multi-arg aggregates, expand cross-cube reference args as slices
        # (wildcarding unshared dimensions) instead of scalar resolution.
        # Only use the slice path when ALL args are cross-cube refs; otherwise
        # slice_over_ref would record spurious dependency edges for args that
        # are ultimately resolved via scalar resolve_ref instead.
        if fn in ("SUM", "MIN", "MAX", "AVG", "AVERAGE", "COUNT", "COUNTA") and resolver is not None and len(node.args) > 1:
            slice_fn = getattr(resolver, "slice_over_ref", None)
            if slice_fn is not None:
                all_cross_cube = all(
                    (isinstance(a, _AstRef) or isinstance(a, _AstMultiRef))
                    and getattr(a, "cube_name", None) is not None
                    for a in node.args
                )
                if all_cross_cube:
                    nums: list[float] = []
                    non_empty_count = 0
                    all_expanded = True
                    for arg in node.args:
                        if isinstance(arg, _AstRef):
                            try:
                                vals = slice_fn([(arg.dim_name, arg.item_name)], addr, arg.cube_name)
                            except (KeyError, ValueError, TypeError):
                                all_expanded = False
                                break
                        elif isinstance(arg, _AstMultiRef):
                            try:
                                vals = slice_fn(list(arg.pairs), addr, arg.cube_name)
                            except (KeyError, ValueError, TypeError):
                                all_expanded = False
                                break
                        else:
                            all_expanded = False
                            break
                        for v in vals:
                            if self._is_error(v):
                                return v
                            if v is not None:
                                non_empty_count += 1
                                try:
                                    nums.append(float(v))
                                except (ValueError, TypeError):
                                    pass
                    if all_expanded:
                        if fn == "SUM":
                            return self._normalize_ieee_special(sum(nums) if nums else 0.0)
                        if fn == "MIN":
                            return self._normalize_ieee_special(min(nums) if nums else 0.0)
                        if fn == "MAX":
                            return self._normalize_ieee_special(max(nums) if nums else 0.0)
                        if fn in ("AVG", "AVERAGE"):
                            if not nums:
                                return CellError("#DIV/0!")
                            return self._normalize_ieee_special(sum(nums) / len(nums))
                        if fn == "COUNT":
                            return float(len(nums))
                        if fn == "COUNTA":
                            return float(non_empty_count)

        # COUNTIF and COUNTIFS
        if fn == "COUNTIF" and len(node.args) >= 2:
            return self._eval_countif(node.args, resolver, addr)
        if fn == "COUNTIFS" and len(node.args) >= 2 and len(node.args) % 2 == 0:
            return self._eval_countifs(node.args, resolver, addr)
        # SUMIF
        if fn == "SUMIF" and len(node.args) >= 2:
            return self._eval_sumif(node.args, resolver, addr)
        # SUMIFS
        if fn == "SUMIFS" and len(node.args) >= 3 and len(node.args) % 2 == 1:
            return self._eval_sumifs(node.args, resolver, addr)

        # Handle aggregate functions with evaluated arguments
        vals = [self._eval(a, resolver, addr) for a in node.args]
        for v in vals:
            if self._is_error(v):
                return v

        nums: list[float] = []
        non_empty_count = 0

        def _resolve_address_value(val: str) -> float | None:
            if not isinstance(val, str):
                return None
            if "::" in val:
                parts = val.split("::", 1)
                cube_part = parts[0]
                ref_part = parts[1] if len(parts) > 1 else ""
            else:
                cube_part = None
                ref_part = val
            if "." not in ref_part:
                return None
            dim_name, item_name = ref_part.split(".", 1)
            if not dim_name or not item_name:
                return None
            if resolver is None:
                return None
            try:
                return resolver.resolve_ref(dim_name, item_name, addr, cube_part)
            except (KeyError, ValueError, TypeError):
                return None

        for v in vals:
            if v is None:
                continue
            if isinstance(v, list):
                for inner in v:
                    if inner is not None:
                        non_empty_count += 1
                        # Check for CellError values in list elements
                        if self._is_error(inner):
                            return inner
                        resolved = _resolve_address_value(inner) if isinstance(inner, str) else None
                        if resolved is not None:
                            nums.append(resolved)
                        else:
                            try:
                                nums.append(float(inner))
                            except (ValueError, TypeError):
                                continue
            else:
                non_empty_count += 1
                # Check for CellError values in scalar values
                if self._is_error(v):
                    return v
                resolved = _resolve_address_value(v) if isinstance(v, str) else None
                if resolved is not None:
                    nums.append(resolved)
                else:
                    try:
                        nums.append(float(v))
                    except (ValueError, TypeError):
                        continue

        if fn == "SUM":
            return self._normalize_ieee_special(sum(nums) if nums else 0.0)
        if fn == "MIN":
            return self._normalize_ieee_special(min(nums) if nums else 0.0)
        if fn == "MAX":
            return self._normalize_ieee_special(max(nums) if nums else 0.0)
        if fn in ("AVG", "AVERAGE"):
            if not nums:
                return CellError("#DIV/0!")
            return self._normalize_ieee_special(sum(nums) / len(nums))
        if fn == "COUNT":
            return float(len(nums))
        if fn == "COUNTA":
            return float(non_empty_count)

        return CellError("#NAME!")

    def _eval_countif(self, args, resolver, addr):
        """COUNTIF(range, criteria) - count cells matching criteria."""
        if len(args) < 2:
            return 0.0
        range_arg = args[0]
        criteria_arg = args[1]

        # Evaluate criteria first
        criteria_val = self._eval(criteria_arg, resolver, addr)
        if self._is_error(criteria_val):
            return criteria_val
        criteria_str = str(criteria_val) if criteria_val is not None else ""

        # Get values from range
        range_values = self._eval_range_values(range_arg, resolver, addr)
        if self._is_error(range_values):
            return range_values

        # Count matches
        count = 0
        for val in range_values:
            if self._value_matches_criteria(val, criteria_str):
                count += 1
        return float(count)

    def _eval_countifs(self, args, resolver, addr):
        """COUNTIFS(range1, criteria1, range2, criteria2, ...) - count cells matching all criteria."""
        if len(args) < 2 or len(args) % 2 != 0:
            return 0.0

        # Collect all range/criteria pairs
        pairs = []
        for i in range(0, len(args), 2):
            range_arg = args[i]
            criteria_arg = args[i + 1]

            criteria_val = self._eval(criteria_arg, resolver, addr)
            if self._is_error(criteria_val):
                return criteria_val
            criteria_str = str(criteria_val) if criteria_val is not None else ""

            range_values = self._eval_range_values(range_arg, resolver, addr)
            if self._is_error(range_values):
                return range_values

            pairs.append((range_values, criteria_str))

        if not pairs:
            return 0.0

        # Count where all criteria match (by index)
        min_len = min(len(rv) for rv, _ in pairs)
        count = 0
        for i in range(min_len):
            if all(self._value_matches_criteria(pairs[j][0][i], pairs[j][1]) for j in range(len(pairs))):
                count += 1
        return float(count)

    def _eval_sumif(self, args, resolver, addr):
        """SUMIF(range, criteria [, sum_range]) - sum cells matching criteria."""
        if len(args) < 2:
            return 0.0
        range_arg = args[0]
        criteria_arg = args[1]

        # Evaluate criteria
        criteria_val = self._eval(criteria_arg, resolver, addr)
        if self._is_error(criteria_val):
            return criteria_val
        criteria_str = str(criteria_val) if criteria_val is not None else ""

        # Get values from criteria range
        range_values = self._eval_range_values(range_arg, resolver, addr)
        if self._is_error(range_values):
            return range_values

        # Get sum range values (optional 3rd arg; defaults to range)
        if len(args) >= 3:
            sum_values = self._eval_range_values(args[2], resolver, addr)
            if self._is_error(sum_values):
                return sum_values
        else:
            sum_values = range_values

        # Sum matching values
        total = 0.0
        min_len = min(len(range_values), len(sum_values))
        for i in range(min_len):
            if self._value_matches_criteria(range_values[i], criteria_str):
                v = sum_values[i]
                if v is None:
                    continue
                try:
                    total += float(v)
                except (ValueError, TypeError):
                    pass
        return total

    def _eval_sumifs(self, args, resolver, addr):
        """SUMIFS(sum_range, criteria_range1, criteria1, ...) - sum cells matching all criteria."""
        if len(args) < 3 or len(args) % 2 != 1:
            return 0.0

        sum_values = self._eval_range_values(args[0], resolver, addr)
        if self._is_error(sum_values):
            return sum_values

        # Collect criteria pairs
        pairs = []
        for i in range(1, len(args), 2):
            range_values = self._eval_range_values(args[i], resolver, addr)
            if self._is_error(range_values):
                return range_values
            criteria_val = self._eval(args[i + 1], resolver, addr)
            if self._is_error(criteria_val):
                return criteria_val
            criteria_str = str(criteria_val) if criteria_val is not None else ""
            pairs.append((range_values, criteria_str))

        total = 0.0
        for i in range(len(sum_values)):
            if all(
                i < len(pairs[k][0]) and self._value_matches_criteria(pairs[k][0][i], pairs[k][1])
                for k in range(len(pairs))
            ):
                v = sum_values[i]
                if v is None:
                    continue
                try:
                    total += float(v)
                except (ValueError, TypeError):
                    pass
        return total

    def _eval_range_values(self, range_arg, resolver, addr):
        """Evaluate a range argument and return list of values."""
        # Handle reference to cube slice
        if isinstance(range_arg, _AstRef) and resolver is not None:
            agg_fn = getattr(resolver, "aggregate_over_ref", None)
            if agg_fn is not None:
                cube_name = getattr(range_arg, "cube_name", None)
                result = self._with_seq_keyword_guard(
                    resolver,
                    getattr(range_arg, "allow_seq_keywords", False),
                    agg_fn,
                    [(range_arg.dim_name, range_arg.item_name)],
                    addr,
                    cube_name,
                    "COUNTA_VALUES",  # Special flag to get values, not count
                )
                if isinstance(result, list):
                    return result
                if isinstance(result, float):
                    return [result]

        # Handle multi-ref (e.g. Cube::Dim.*:Dim2.Item) via slice_over_ref
        if isinstance(range_arg, _AstMultiRef) and resolver is not None:
            cube_name = getattr(range_arg, "cube_name", None)
            # Try slice_over_ref first for cross-cube wildcard expansion
            slice_fn = getattr(resolver, "slice_over_ref", None)
            if slice_fn is not None:
                try:
                    result = self._with_seq_keyword_guard(
                        resolver,
                        getattr(range_arg, "allow_seq_keywords", False),
                        slice_fn,
                        list(range_arg.pairs),
                        addr,
                        cube_name,
                    )
                    if isinstance(result, list):
                        return result
                    if isinstance(result, float):
                        return [result]
                except (KeyError, ValueError, TypeError):
                    pass
            # Fall back to aggregate_over_ref with COUNTA_VALUES
            agg_fn = getattr(resolver, "aggregate_over_ref", None)
            if agg_fn is not None:
                try:
                    result = self._with_seq_keyword_guard(
                        resolver,
                        getattr(range_arg, "allow_seq_keywords", False),
                        agg_fn,
                        list(range_arg.pairs),
                        addr,
                        cube_name,
                        "COUNTA_VALUES",
                    )
                    if isinstance(result, list):
                        return result
                    if isinstance(result, float):
                        return [result]
                except (KeyError, ValueError, TypeError):
                    pass

        # Evaluate directly
        val = self._eval(range_arg, resolver, addr)
        if self._is_error(val):
            return val
        if isinstance(val, list):
            return val
        return [val] if val is not None else []

    def _value_matches_criteria(self, val, criteria):
        """Check if a value matches a criteria string."""
        if val is None:
            return False

        # Handle numeric comparisons
        criteria = criteria.strip()

        # Check for comparison operators
        if criteria.startswith(">="):
            try:
                threshold = float(criteria[2:])
                return float(val) >= threshold
            except (ValueError, TypeError):
                return False
        elif criteria.startswith("<="):
            try:
                threshold = float(criteria[2:])
                return float(val) <= threshold
            except (ValueError, TypeError):
                return False
        elif criteria.startswith(">"):
            try:
                threshold = float(criteria[1:])
                return float(val) > threshold
            except (ValueError, TypeError):
                return False
        elif criteria.startswith("<"):
            try:
                threshold = float(criteria[1:])
                return float(val) < threshold
            except (ValueError, TypeError):
                return False
        elif criteria.startswith("="):
            # Exact match
            match_val = criteria[1:]
            try:
                # Try numeric comparison first
                return float(val) == float(match_val)
            except (ValueError, TypeError):
                # String comparison
                return str(val).lower() == match_val.lower()
        elif criteria.startswith("<>"):
            # Not equal
            match_val = criteria[2:]
            try:
                return float(val) != float(match_val)
            except (ValueError, TypeError):
                return str(val).lower() != match_val.lower()

        # Wildcard match (* and ?)
        if "*" in criteria or "?" in criteria:
            return fnmatch.fnmatch(str(val).lower(), criteria.lower())

        # Exact match (try numeric first, then string)
        try:
            return float(val) == float(criteria)
        except (ValueError, TypeError):
            return str(val).lower() == criteria.lower()

    # -----------------------------------------------------------------------
    # Hierarchy navigation functions - return lists of item IDs
    # -----------------------------------------------------------------------

    def _eval_desc(self, node, resolver, addr):
        """Return descendant leaf item IDs for aggregation."""
        return self._outline_navigate("DESC", node, resolver, addr)

    def _eval_ance(self, node, resolver, addr):
        """Return ancestor item IDs (parent chain)."""
        return self._outline_navigate("ANCE", node, resolver, addr)

    def _eval_peer(self, node, resolver, addr):
        """Return peer (same-level) item IDs."""
        return self._outline_navigate("PEER", node, resolver, addr)

    def _eval_sibl(self, node, resolver, addr):
        """Return sibling item IDs."""
        return self._outline_navigate("SIBL", node, resolver, addr)

    def _eval_chil(self, node, resolver, addr):
        """Return immediate child item IDs."""
        return self._outline_navigate("CHIL", node, resolver, addr)

    def _eval_pare(self, node, resolver, addr):
        """Return parent item ID (single item in list)."""
        return self._outline_navigate("PARE", node, resolver, addr)

    def _outline_navigate(self, op: str, node, resolver, addr) -> list[str]:
        """Handle ANCE, DESC, PEER, SIBL, CHIL, PARE operations.

        Returns list of formatted address strings:
        - If cube_name specified: ["cube::dim.item1", "cube::dim.item2", ...]
        - If no cube: ["dim.item1", "dim.item2", ...]
        These can be resolved by aggregate functions like SUM.
        """
        if resolver is None:
            raise RuntimeError(f"{op} requires a CubeResolver")
        if len(node.args) < 1:
            raise ValueError(f"{op} requires 1 argument (dimension item reference)")

        arg = node.args[0]
        dim_name: str | None = None
        item_name: str | None = None
        cube_name: str | None = None

        # Require a specific item reference - pure dimension names not allowed
        if isinstance(arg, _AstRef):
            dim_name = arg.dim_name
            item_name = arg.item_name
            cube_name = getattr(arg, "cube_name", None)
        elif isinstance(arg, _AstMultiRef) and arg.pairs:
            dim_name, item_name = arg.pairs[-1]
            cube_name = getattr(arg, "cube_name", None)
        elif isinstance(arg, _AstCtxRef):
            raise ValueError(f"{op} requires a specific dimension item (e.g., Dim.Item), not just dimension name")
        elif isinstance(arg, _AstStr):
            raise ValueError(f"{op} requires a specific dimension item (e.g., Dim.Item), not just dimension name")
        else:
            raise ValueError(f"{op} requires a specific dimension item reference (e.g., Dim.Item)")

        if not dim_name or not item_name:
            raise ValueError(f"{op} requires both dimension and item (e.g., Dim.Item)")

        # Dispatch to resolver method based on operation
        if op == "DESC":
            result = resolver.descendants_for_dim_item(dim_name, item_name, addr, cube_name)
        elif op == "ANCE":
            result = resolver.ancestors_for_dim_item(dim_name, item_name, addr, cube_name)
        elif op == "PEER":
            result = resolver.peers_for_dim_item(dim_name, item_name, addr, cube_name)
        elif op == "SIBL":
            result = resolver.siblings_for_dim_item(dim_name, item_name, addr, cube_name)
        elif op == "CHIL":
            result = resolver.children_for_dim_item(dim_name, item_name, addr, cube_name)
        elif op == "PARE":
            result = resolver.parent_for_dim_item(dim_name, item_name, addr, cube_name)
        else:
            raise ValueError(f"Unknown outline operation: {op}")

        # Convert result to list of item names
        if isinstance(result, list):
            item_names = result
        else:
            item_names = [result] if result else []

        # Format as address strings
        # Resolver methods return item labels (names), so use directly
        prefix = f"{cube_name}::" if cube_name else ""
        return [f"{prefix}{dim_name}.{name}" for name in item_names if name]
