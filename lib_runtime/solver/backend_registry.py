"""Backend capability registry.

Static registry of solver backends and their algorithm capabilities.
"""

from __future__ import annotations

from lib_runtime.solver.solver_types import (
    BackendCapability,
    AlgorithmCapability,
    CancellationCapability,
)


# --- SciPy backend capabilities -------------------------------------------

SCIPY_AUTO = AlgorithmCapability(
    algorithm_id="auto",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_COBYLA = AlgorithmCapability(
    algorithm_id="cobyla",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_BOBYQA = AlgorithmCapability(
    algorithm_id="bobyqa",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=False,
    supports_equality_constraints=False,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_NEWUOA = AlgorithmCapability(
    algorithm_id="newuoa",
    backend_id="scipy",
    supports_bounds=False,
    supports_inequality_constraints=False,
    supports_equality_constraints=False,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_LINCOA = AlgorithmCapability(
    algorithm_id="lincoa",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=False,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_LINPROG = AlgorithmCapability(
    algorithm_id="linprog",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
    linear=True,
)

SCIPY_SLSQP = AlgorithmCapability(
    algorithm_id="slsqp",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_TRUST_CONSTR = AlgorithmCapability(
    algorithm_id="trust-constr",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_NELDER_MEAD = AlgorithmCapability(
    algorithm_id="nelder-mead",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=False,
    supports_equality_constraints=False,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_POWELL = AlgorithmCapability(
    algorithm_id="powell",
    backend_id="scipy",
    supports_bounds=True,
    supports_inequality_constraints=False,
    supports_equality_constraints=False,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

SCIPY_BACKEND = BackendCapability(
    backend_id="scipy",
    algorithms=["auto", "cobyla", "bobyqa", "newuoa", "lincoa", "linprog", "slsqp", "trust-constr", "nelder-mead", "powell"],
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)


# --- pymoo backend capabilities -------------------------------------------

PYMOO_AUTO = AlgorithmCapability(
    algorithm_id="auto",
    backend_id="pymoo",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

PYMOO_NSGA2 = AlgorithmCapability(
    algorithm_id="nsga2",
    backend_id="pymoo",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

PYMOO_NSGA3 = AlgorithmCapability(
    algorithm_id="nsga3",
    backend_id="pymoo",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

PYMOO_MOEAD = AlgorithmCapability(
    algorithm_id="moead",
    backend_id="pymoo",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

PYMOO_SMS_EMOA = AlgorithmCapability(
    algorithm_id="sms-emoa",
    backend_id="pymoo",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

PYMOO_GA = AlgorithmCapability(
    algorithm_id="ga",
    backend_id="pymoo",
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)

PYMOO_BACKEND = BackendCapability(
    backend_id="pymoo",
    algorithms=["auto", "nsga2", "nsga3", "moead", "sms-emoa", "ga"],
    supports_bounds=True,
    supports_inequality_constraints=True,
    supports_equality_constraints=True,
    requires_derivatives=False,
    cancellation=CancellationCapability.COOPERATIVE_CALLBACK,
)


# --- Registry --------------------------------------------------------------

_BACKENDS: dict[str, BackendCapability] = {
    "scipy": SCIPY_BACKEND,
    "pymoo": PYMOO_BACKEND,
}

_ALGORITHMS: dict[tuple[str, str], AlgorithmCapability] = {
    ("scipy", "auto"): SCIPY_AUTO,
    ("scipy", "cobyla"): SCIPY_COBYLA,
    ("scipy", "bobyqa"): SCIPY_BOBYQA,
    ("scipy", "newuoa"): SCIPY_NEWUOA,
    ("scipy", "lincoa"): SCIPY_LINCOA,
    ("scipy", "linprog"): SCIPY_LINPROG,
    ("scipy", "slsqp"): SCIPY_SLSQP,
    ("scipy", "trust-constr"): SCIPY_TRUST_CONSTR,
    ("scipy", "nelder-mead"): SCIPY_NELDER_MEAD,
    ("scipy", "powell"): SCIPY_POWELL,
    ("pymoo", "auto"): PYMOO_AUTO,
    ("pymoo", "nsga2"): PYMOO_NSGA2,
    ("pymoo", "nsga3"): PYMOO_NSGA3,
    ("pymoo", "moead"): PYMOO_MOEAD,
    ("pymoo", "sms-emoa"): PYMOO_SMS_EMOA,
    ("pymoo", "ga"): PYMOO_GA,
}


def list_backends() -> list[BackendCapability]:
    """Return all registered backends."""
    return list(_BACKENDS.values())


def get_backend(backend_id: str) -> BackendCapability | None:
    """Look up a backend by ID."""
    return _BACKENDS.get(backend_id)


def list_algorithms(backend_id: str) -> list[AlgorithmCapability]:
    """Return all algorithms for a given backend."""
    return [
        algo for (bid, _), algo in _ALGORITHMS.items() if bid == backend_id
    ]


def get_algorithm(backend_id: str, algorithm_id: str) -> AlgorithmCapability | None:
    """Look up an algorithm by backend + algorithm ID."""
    return _ALGORITHMS.get((backend_id, algorithm_id))
