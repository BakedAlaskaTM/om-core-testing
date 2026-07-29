"""Tokenizer for rule body expressions."""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

# Token types
_TT_NUM    = "NUM"
_TT_STR    = "STR"
_TT_NAME   = "NAME"
_TT_REF    = "REF"    # [Dim:Item]
_TT_OP     = "OP"
_TT_COMMA  = "COMMA"
_TT_LPAREN = "LPAREN"
_TT_RPAREN = "RPAREN"
_TT_ANCHOR = "ANCHOR"  # $ prefix for anchored rules
_TT_EOF    = "EOF"
_SEQ_KEYWORDS = {"THIS", "PREV", "NEXT", "FIRST", "LAST"}


@dataclass
class _Tok:
    kind: str
    value: Any
    is_bracketed: bool = False


_CUBE_WILDCARD_REF = re.compile(
    r"(?P<cube>[A-Za-z_%][A-Za-z0-9_\s%]*)::\s*\*(?:\s*\.\s*\*)?(?=$|[\s+\-*/^(),%<>!=])"
)
_CUBE_BARE_REF = re.compile(
    r"(?P<cube>[A-Za-z_%][A-Za-z0-9_\s%]*)::\s*(?=$|[\s+\-*/^(),%<>!=])"
)


def _normalise_cube_wildcards(expr: str) -> str:
    def repl(match: re.Match[str]) -> str:
        cube = match.group("cube").strip()
        return f"{cube}::[*.*]"

    expr = _CUBE_WILDCARD_REF.sub(repl, expr)
    return _CUBE_BARE_REF.sub(repl, expr)


def _normalise_bare_wildcard_ref(expr: str) -> str:
    """Normalize bare *.* to bracketed [*.*] for consistent parsing.

    Bare *.* inside function args like MIN(*.*) needs to be wrapped in
    brackets so the tokenizer treats it as a single REF token.
    """
    # Match *.* that is not already inside brackets or preceded by ::
    # Use negative lookbehind to avoid matching after :: or [ or .
    import re

    # Replace bare *.* that appear as standalone tokens (e.g., inside MIN())
    # but not after :: (cube prefix) or inside []
    result = []
    i = 0
    n = len(expr)
    while i < n:
        # Check if we're at a *.* pattern
        if i + 2 < n and expr[i:i+3] == "*.*":
            # Check what's before this
            before_ok = True
            if i > 0:
                prev_char = expr[i-1]
                # Don't match if preceded by :, [, . (part of larger ref)
                if prev_char in ":[.":
                    before_ok = False
                # Don't match if preceded by another * (e.g., already handled)
                if prev_char == "*":
                    before_ok = False
            # Check what's after
            after_ok = True
            if i + 3 < n:
                next_char = expr[i+3]
                # Don't match if followed by more dots or brackets (part of larger ref)
                if next_char in ".[]":
                    after_ok = False

            if before_ok and after_ok:
                result.append("[*.*]")
                i += 3
                continue

        result.append(expr[i])
        i += 1

    return "".join(result)


_UNBRACKETED_WILDCARD_CHAIN_RE = re.compile(
    r"(?<![A-Za-z0-9_%\[])"  # not part of a larger identifier or already bracketed
    + r"(?:([A-Za-z_%][A-Za-z0-9_\s%]*)::\s*)?"
    + r"([A-Za-z_%][A-Za-z0-9_\s%]*\.(?:\*|[A-Za-z0-9_%][A-Za-z0-9_\s%]*)"
    + r"(?:\s*:\s*[A-Za-z_%][A-Za-z0-9_\s%]*\.(?:\*|[A-Za-z0-9_%][A-Za-z0-9_\s%]*))*)"
    + r"(?![A-Za-z0-9_%])"
)


def _normalise_unbracketed_wildcard_chains(expr: str) -> str:
    """Convert unbracketed multi-segment refs that use `.*` into bracketed form.

    The unbracketed reference regex cannot distinguish the `*` wildcard from the
    multiplication operator, so `Cube::Dim.Item:Dim.*` is rewritten to
    `Cube::[Dim.Item, Dim.*]` before tokenization. References without a `.*`
    wildcard are left unchanged.

    Colons separating dimension segments are converted to commas so the
    resulting bracketed form is a valid multi-ref (e.g.
    `Day.*:Hour.*` → `[Day.*, Hour.*]`).
    """

    def repl(match: re.Match[str]) -> str:
        cube, chain = match.group(1), match.group(2)
        if ".*" not in chain:
            return match.group(0)
        chain = chain.replace(":", ",")
        if cube:
            return f"{cube}::[{chain}]"
        return f"[{chain}]"

    # Only apply the regex outside of brackets — patterns inside [...]
    # are already bracketed refs and must not be re-wrapped.
    result: list[str] = []
    i = 0
    n = len(expr)
    while i < n:
        ch = expr[i]
        if ch == "[":
            # Find the matching close bracket and copy the whole span verbatim.
            depth = 1
            j = i + 1
            while j < n and depth > 0:
                if expr[j] == "[":
                    depth += 1
                elif expr[j] == "]":
                    depth -= 1
                j += 1
            result.append(expr[i:j])
            i = j
        else:
            # Try to match the regex at this position.
            m = _UNBRACKETED_WILDCARD_CHAIN_RE.match(expr, i)
            if m:
                result.append(repl(m))
                i = m.end()
            else:
                result.append(ch)
                i += 1
    return "".join(result)


# Single regex for tokenization - NUM without leading +/-
_TOKEN_RE = re.compile(
    r"""
      (?P<NUM>    (?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)? )
    | (?P<STR>    "([^"\\]|\\.)*" | '([^'\\]|\\.)*' )
    | (?P<ANCHOR> \$ )                                              # $ anchor prefix
    | (?P<REF>
          (?:[A-Za-z_%][A-Za-z0-9_\s%]*|\*)::\[(?:\$<[^>]*>|[^\[\]]|\[[^\[\]]*\])+\]   # Cube-qualified or wildcard cube ref
        | \[(?:\$<[^>]*>|[^\[\]]|\[[^\[\]]*\])+\]                    # [Dim.Item] or multi-ref, allowing inner Dim[Item]
        | (?:[A-Za-z_%][A-Za-z0-9_\s%]*|\*)\[(?:\$<[^>]*>|[^\[\]]|\[[^\[\]]*\])+\]     # Dim[Item] sugar
            (?:\s*:\s*(?:[A-Za-z_%][A-Za-z0-9_\s%]*|\*)
                (?:\[(?:\$<[^>]*>|[^\[\]]|\[[^\[\]]*\])+\]
                |[:.][^+\-*/^(),%<>!=$]*)
            )*
        | (?:[A-Za-z_%][A-Za-z0-9_\s%]*|\*|@)(?:::+|[:.])                    # Improv-style unbracketed ref prefix (incl. @ dim)
          [^+\-*/^(),%<>!=$]*                                            # item name (exclude comma so IF arg commas stay top-level)
          (?:\$<[^>]+>)?                                                 # optional dynamic-bound suffix $<...> (may contain commas and ']')
          (?:\s*:\s*(?:[A-Za-z_%][A-Za-z0-9_\s%]*|\*|@)
                (?:\[(?:\$<[^>]*>|[^\[\]]|\[[^\[\]]*\])+\]
                |[:.][^+\-*/^(),%<>!=$]*)
            )*
      )
    | (?P<NAME>   [A-Za-z_%][A-Za-z0-9_\s%]* )
    | (?P<OP>     \*\* | <> | <= | >= | == | != | = | [+\-*/^(),%<>!&|] )
    | (?P<WS>     \s+ )
    """,
    re.VERBOSE,
)


def _tokenise(expr: str) -> list[_Tok]:
    expr = _normalise_cube_wildcards(expr)
    expr = _normalise_bare_wildcard_ref(expr)
    expr = _normalise_unbracketed_wildcard_chains(expr)
    toks: list[_Tok] = []
    pos = 0
    while pos < len(expr):
        # Check for signed numbers at start or after operators/parens/comma
        # This allows -5 and +3 to be tokenized as negative/positive numbers
        # but keeps - and + as operators after operands (e.g., Years.2024+1)
        prev_kind = toks[-1].kind if toks else None
        is_unary_context = prev_kind is None or prev_kind in (_TT_OP, _TT_LPAREN, _TT_COMMA)
        
        if is_unary_context and expr[pos] in '+-' and pos + 1 < len(expr) and expr[pos + 1].isdigit():
            # Try to match signed number
            signed_num_match = re.match(r'[+-](?:\d+\.?\d*|\.\d+)(?:[eE][+-]?\d+)?', expr[pos:])
            if signed_num_match:
                val = signed_num_match.group()
                toks.append(_Tok(_TT_NUM, float(val)))
                pos += len(val)
                continue
        
        m = _TOKEN_RE.match(expr, pos)
        if not m:
            raise SyntaxError(f"Unexpected character {expr[pos]!r} at pos {pos}")
        kind = m.lastgroup
        val = m.group()
        pos = m.end()
        if kind == "WS":
            continue
        if kind == "NUM":
            num_val = float(val)
            # Check for percentage suffix (e.g., 23% -> 0.23)
            if pos < len(expr) and expr[pos] == '%':
                num_val = num_val / 100.0
                pos += 1  # Skip the % character
            toks.append(_Tok(_TT_NUM, num_val))
        elif kind == "STR":
            # Strip surrounding quotes; simple escape handling is delegated to
            # the regex, which keeps the contents as-is inside the quotes.
            inner = val[1:-1]
            toks.append(_Tok(_TT_STR, inner))
        elif kind == "REF":
            # Support bracketed [Dim:Item] as well as bare Improv-style
            # refs like Dim:Item, Sheet::Dim.Item, etc.
            # Handle multiplication case: *[...] should be * + [...]
            if (
                val.startswith("*[")
                and toks
                and toks[-1].kind in (_TT_NUM, _TT_STR, _TT_REF, _TT_RPAREN)
            ):
                toks.append(_Tok(_TT_OP, "*"))
                val = val[1:]  # Strip the leading *
            is_bracketed = "[" in val
            # Strip outer brackets from the value to normalize
            inner = val[1:-1] if val.startswith("[") and val.endswith("]") else val
            toks.append(_Tok(_TT_REF, inner, is_bracketed=is_bracketed))
        elif kind == "NAME":
            toks.append(_Tok(_TT_NAME, val))
        elif kind == "ANCHOR":
            toks.append(_Tok(_TT_ANCHOR, val))
        elif kind == "OP":
            if val == '(':
                toks.append(_Tok(_TT_LPAREN, val))
            elif val == ')':
                toks.append(_Tok(_TT_RPAREN, val))
            elif val == ',':
                toks.append(_Tok(_TT_COMMA, val))
            else:
                # Normalize operators: ^ to **, <> to !=
                normalized_val = val
                if val == "^":
                    normalized_val = "**"
                elif val == "<>":
                    normalized_val = "!="
                toks.append(_Tok(_TT_OP, normalized_val))
        else:
            raise SyntaxError(f"Unknown token kind {kind}")
    toks.append(_Tok(_TT_EOF, None))
    return toks
