"""Algorithm name to scipy.optimize.minimize method mapping.

Note: BOBYQA, NEWUOA, LINCOA require the ``pdfo`` package which is
incompatible with NumPy 2.x (Fortran .so compiled against NumPy 1.x).
When ``pyprima`` (pure-Python PRIMA) is released on PyPI, these
algorithms can be enabled. Until then, they fall back to COBYLA with
a warning, and constraint validation rejects unsupported constraint
types before the fallback.
"""

from __future__ import annotations

ALGORITHM_MAP: dict[str, str] = {
    "cobyla": "COBYLA",
    # pdfo algorithms — fall back to COBYLA (see scipy_backend.py).
    "bobyqa": "COBYLA",
    "newuoa": "COBYLA",
    "lincoa": "COBYLA",
    "linprog": "linprog",
    "slsqp": "SLSQP",
    "trust-constr": "trust-constr",
    "nelder-mead": "Nelder-Mead",
    "powell": "Powell",
}


def get_scipy_method(algorithm_id: str) -> str:
    """Return the scipy.optimize.minimize method name for an algorithm."""
    method = ALGORITHM_MAP.get(algorithm_id)
    if method is None:
        raise ValueError(f"Unknown algorithm: {algorithm_id}")
    return method
