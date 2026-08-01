"""Correctly-rounded IEEE-754 transcendental math functions via gmpy2/MPFR.

All functions accept Python floats, compute in MPFR with precision=53
and round-to-nearest-even, then return a Python float. This guarantees
bit-identical results across platforms (Linux, macOS, Windows).

Domain errors (e.g. log(-1), asin(2)) return NaN or Inf as floats rather
than raising ValueError/OverflowError. Callers should use
_normalize_ieee_special to convert these to CellError values.
"""
from __future__ import annotations

import gmpy2

# Set module-level context: IEEE-754 double precision, round-to-nearest-even.
gmpy2.set_context(gmpy2.context(precision=53, round=gmpy2.RoundToNearest))


def log(x: float) -> float:
    return float(gmpy2.log(gmpy2.mpfr(x)))


def log10(x: float) -> float:
    return float(gmpy2.log10(gmpy2.mpfr(x)))


def exp(x: float) -> float:
    return float(gmpy2.exp(gmpy2.mpfr(x)))


def sqrt(x: float) -> float:
    return float(gmpy2.sqrt(gmpy2.mpfr(x)))


def sin(x: float) -> float:
    return float(gmpy2.sin(gmpy2.mpfr(x)))


def cos(x: float) -> float:
    return float(gmpy2.cos(gmpy2.mpfr(x)))


def tan(x: float) -> float:
    return float(gmpy2.tan(gmpy2.mpfr(x)))


def asin(x: float) -> float:
    return float(gmpy2.asin(gmpy2.mpfr(x)))


def acos(x: float) -> float:
    return float(gmpy2.acos(gmpy2.mpfr(x)))


def atan(x: float) -> float:
    return float(gmpy2.atan(gmpy2.mpfr(x)))


def atan2(y: float, x: float) -> float:
    return float(gmpy2.atan2(gmpy2.mpfr(y), gmpy2.mpfr(x)))


def pow(base: float, exp: float) -> float:
    return float(gmpy2.mpfr(base) ** gmpy2.mpfr(exp))


def radians(x: float) -> float:
    return float(gmpy2.radians(gmpy2.mpfr(x)))


def degrees(x: float) -> float:
    return float(gmpy2.degrees(gmpy2.mpfr(x)))
