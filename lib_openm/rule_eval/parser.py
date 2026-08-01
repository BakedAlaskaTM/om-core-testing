"""Recursive-descent parser for rule body expressions."""
from __future__ import annotations

from typing import Any

from .tokenizer import _Tok, _TT_NUM, _TT_STR, _TT_NAME, _TT_REF, _TT_OP, _TT_COMMA, _TT_LPAREN, _TT_RPAREN, _TT_EOF, _SEQ_KEYWORDS
from .ast_nodes import _AstNum, _AstStr, _AstBinOp, _AstUnOp, _AstRef, _AstMultiRef, _AstDynamicMultiRef, _AstCtxRef, _AstCall, _FUNCTIONS
# Import UDF functions set for dynamic registration
try:
    from lib_openm.udf_registry import _UdfFunctions
except ImportError:
    _UdfFunctions = set()
from .refs import _parse_ref_segment, _split_ref_inner, _split_ref_chain


class _Parser:
    def __init__(self, tokens: list[_Tok]):
        self._toks = tokens
        self._pos = 0

    def _cur(self) -> _Tok: return self._toks[self._pos]
    def _peek(self, offset: int = 1) -> _Tok: return self._toks[self._pos + offset]

    def _eat(self, kind: str | None = None, value: Any = None) -> _Tok:
        t = self._cur()
        if kind and t.kind != kind:
            raise SyntaxError(f"Expected {kind}, got {t.kind}={t.value!r}")
        if value is not None and t.value != value:
            raise SyntaxError(f"Expected {value!r}, got {t.value!r}")
        self._pos += 1
        return t

    def parse(self) -> Any:
        node = self._expr()
        self._eat(_TT_EOF)
        return node

    def _expr(self) -> Any:
        return self._comparison()

    def _comparison(self) -> Any:
        left = self._additive()
        while self._cur().kind == _TT_OP and self._cur().value in (">", "<", ">=", "<=", "==", "!=", "<>", "="):
            op = self._eat().value
            if op == "<>": op = "!="
            elif op == "=": op = "=="
            right = self._additive()
            left = _AstBinOp(op, left, right)
        return left

    def _additive(self) -> Any:
        left = self._multiplicative()
        while self._cur().kind == _TT_OP and self._cur().value in ("+", "-", "&"):
            op = self._eat().value
            right = self._multiplicative()
            left = _AstBinOp(op, left, right)
        return left

    def _multiplicative(self) -> Any:
        left = self._power()
        while self._cur().kind == _TT_OP and self._cur().value in ("*", "/"):
            op = self._eat().value
            right = self._power()
            left = _AstBinOp(op, left, right)
        return left

    def _power(self) -> Any:
        # Right-associative: 2 ** 3 ** 2 = 2 ** (3 ** 2) = 512
        base = self._unary()
        if self._cur().kind == _TT_OP and self._cur().value == "**":
            self._eat()
            exp = self._power()  # Right associative - call _power again
            return _AstBinOp("**", base, exp)
        return base

    def _unary(self) -> Any:
        if self._cur().kind == _TT_OP and self._cur().value == "-":
            self._eat()
            return _AstUnOp("-", self._unary())  # Support double negation: --5
        if self._cur().kind == _TT_OP and self._cur().value == "+":
            self._eat()
            return self._unary()  # Support double positive: ++5
        return self._atom()

    def _atom(self) -> Any:
        t = self._cur()

        if t.kind == _TT_NUM:
            self._eat()
            return _AstNum(t.value)

        if t.kind == _TT_STR:
            self._eat()
            return _AstStr(t.value)

        if t.kind == _TT_REF:
            self._eat()
            # Support Improv-style refs with optional sheet/cube prefixes and
            # multi-dimension overrides separated by commas:
            #   Dim:Item
            #   Dim.Item
            #   Cube::Dim.Item
            #   [Year:1994, Quarter:Q2]
            #   Cube::[Year:1994, Quarter:Q2]

            raw = t.value
            allow_seq_keywords = t.is_bracketed
            cube_name: str | None = None
            if "::" in raw:
                prefix, rest = raw.split("::", 1)
                cube_name = prefix.strip() or None
                raw = rest.strip()

            # For cube-qualified multi-refs like Cube::[Year:1994, Quarter:Q2]
            # the inner value still has brackets; normalise to bare
            # "Year:1994, Quarter:Q2" so the split and _parse_ref_segment
            # logic below works the same as for plain "[Year:..., Quarter:...]".
            if raw.startswith("[") and raw.endswith("]"):
                raw = raw[1:-1].strip()

            # Split the inner content into segments, but *do not* split on
            # commas that appear inside dynamic $<...> expressions so that
            # multi-refs used inside dynamic bounds remain intact.
            segments = _split_ref_inner(raw)

            pairs: list[tuple[str, str]] = []
            dynamic_calls: list[_AstCall] = []
            saw_seq_keyword = False
            has_dynamic = False

            for part in segments:
                # Check if this segment contains a function call (has parens)
                if '(' in part and ')' in part:
                    # This looks like a function call - parse it dynamically
                    has_dynamic = True
                    # Tokenize and parse the function call
                    from .tokenizer import _tokenise
                    try:
                        func_tokens = _tokenise(part)
                        func_parser = _Parser(func_tokens)
                        func_node = func_parser.parse()
                        if isinstance(func_node, _AstCall):
                            dynamic_calls.append(func_node)
                        elif isinstance(func_node, _AstRef):
                            # Function returned a single ref, treat as static
                            pairs.append((func_node.dim_name, func_node.item_name))
                        elif isinstance(func_node, _AstMultiRef):
                            # Function returned multi-ref, extract pairs
                            for dim_name, item_name in func_node.pairs:
                                pairs.append((dim_name, item_name))
                    except (ValueError, TypeError):
                        # If dynamic parsing fails, treat as static ref
                        chain_segments = _split_ref_chain(part)
                        for seg in chain_segments:
                            dim_name, item_name = _parse_ref_segment(seg)
                            if item_name.upper() in _SEQ_KEYWORDS:
                                saw_seq_keyword = True
                            pairs.append((dim_name, item_name))
                else:
                    # Static reference segment
                    # Handle bracketed format like "year[this]" -> strip brackets first
                    part_clean = part.strip()
                    if part_clean.startswith("[") and part_clean.endswith("]"):
                        part_clean = part_clean[1:-1].strip()
                    chain_segments = _split_ref_chain(part_clean)
                    for seg in chain_segments:
                        dim_name, item_name = _parse_ref_segment(seg)
                        if item_name.upper() in _SEQ_KEYWORDS:
                            saw_seq_keyword = True
                        pairs.append((dim_name, item_name))

            allow_seq_keywords = allow_seq_keywords and saw_seq_keyword

            if len(pairs) == 1 and not has_dynamic:
                return _AstRef(pairs[0][0], pairs[0][1], cube_name, allow_seq_keywords)
            if has_dynamic:
                return _AstDynamicMultiRef(pairs, dynamic_calls, cube_name)
            return _AstMultiRef(pairs, cube_name, allow_seq_keywords)

        if t.kind == _TT_NAME:
            name_upper = t.value.upper()
            # Boolean literals: treat TRUE/FALSE (any case) as numeric 1/0
            # instead of contextual references. This matches common
            # spreadsheet semantics and keeps expressions like
            # IF(cond, TRUE, FALSE) working without requiring dedicated
            # dimensions or items named "TRUE"/"FALSE".
            if name_upper in ("TRUE", "FALSE") and self._peek().kind == _TT_LPAREN:
                return self._call(name_upper)
            if name_upper == "TRUE":
                self._eat()
                return _AstNum(1.0)
            if name_upper == "FALSE":
                self._eat()
                return _AstNum(0.0)
            if name_upper in _FUNCTIONS or name_upper in _UdfFunctions:
                if self._peek().kind == _TT_LPAREN:
                    return self._call(name_upper)
            # Unknown name followed by ( — treat as function call for #NAME! at eval time
            if self._peek().kind == _TT_LPAREN:
                return self._call(name_upper)
            self._eat()
            return _AstCtxRef(t.value)

        if t.kind == _TT_LPAREN:
            self._eat()
            node = self._expr()
            self._eat(_TT_RPAREN)
            return node

        raise SyntaxError(f"Unexpected token {t.kind}={t.value!r}")

    def _call(self, fn: str) -> _AstCall:
        self._eat(_TT_NAME)   # consume function name
        self._eat(_TT_LPAREN)
        args: list[Any] = []
        if self._cur().kind != _TT_RPAREN:
            args.append(self._expr())
            while self._cur().kind == _TT_COMMA:
                self._eat()
                args.append(self._expr())
        self._eat(_TT_RPAREN)
        return _AstCall(fn, args)
