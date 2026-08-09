"""Live-Engine evaluation session for solver candidate evaluations.

This module implements the ``SolverEvaluationSession`` that temporarily
writes candidate decision-variable values into the live Engine workspace,
recalculates, reads objective/constraint cells, and restores original
values — all without dispatching public commands, pushing undo actions,
publishing events, or incrementing revisions.

The session uses direct ``cube.data`` manipulation combined with internal
Engine cache/dependency-graph invalidation.  This is architecturally
acceptable because the evaluation adapter is a runtime-internal component,
not a client.  The public command/event/undo path is never touched.

Phase 0 state-reset proof: after every evaluation (successful or failed),
the original hardvalue states are restored and recalculation returns the
model to the same observable baseline.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any

from lib_openm.model import Cube

from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_RESTORATION_FAILED,
    SOLVER_RESTORATION_VALUE_UNSUPPORTED,
    SOLVER_UNSUPPORTED_FEATURE,
)
from lib_runtime.solver.solver_types import (
    SolverProblem,
    SolverVariable,
    SolverObjective,
    SolverConstraint,
)

logger = logging.getLogger(__name__)


# --- Restoration table entry ----------------------------------------------

@dataclass
class _RestorationEntry:
    """Snapshot of a cell's original state for restoration."""

    cube_id: str
    addr: tuple[str, ...]
    original_value: Any
    was_override: bool


# --- Supported value check -------------------------------------------------

def _is_supported_hardvalue(value: Any) -> bool:
    """Check whether a value is a supported type for solver variable restoration.

    Supported: int, float (not NaN/inf).
    Unsupported: None (blank), str, CellError, bool, complex, etc.
    """
    if value is None:
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, (int, float)):
        import math
        if math.isnan(value) or math.isinf(value):
            return False
        return True
    return False


# --- Evaluation session ----------------------------------------------------

class SolverEvaluationSession:
    """Live-Engine evaluation session.

    Holds the exclusive workspace solver-evaluation lease.  Each
    ``evaluate_candidate`` call temporarily writes decision-variable
    values, recalculates, reads objective/constraint cells, and restores
    original values.

    The session must be closed exactly once via ``close()``, which
    restores original values and releases the lease.
    """

    def __init__(
        self,
        engine: Any,
        problem: SolverProblem,
    ) -> None:
        # Accept either an Engine, RemoteEngine, or _EngineCore directly.
        if hasattr(engine, "_core"):
            # Local Engine facade — use its _core for direct manipulation.
            self._engine = engine
            self._core = engine._core
        elif hasattr(engine, "clear_cell_cache"):
            # _EngineCore passed directly (e.g. from tests).
            self._engine = engine
            self._core = engine
        else:
            # RemoteEngine — no _core available, use public API.
            self._engine = engine
            self._core = None
        self._is_remote = self._core is None
        self._problem = problem
        self._closed = False
        self._lock = threading.Lock()
        self.live_eval_count = 0

        # Build restoration table from variable source cells.
        self._restoration_table: list[_RestorationEntry] = []
        for var in problem.variables:
            cube = self._require_cube_by_id(var.cube_id)
            addr = var.addr
            original_value = cube.get(addr)
            was_override = addr in cube.user_override_addrs

            # Validate that the initial value is a supported type.
            if not _is_supported_hardvalue(original_value):
                raise SolverException(
                    SOLVER_RESTORATION_VALUE_UNSUPPORTED,
                    f"Variable cell {var.display_ref} has unsupported "
                    f"hardvalue type: {type(original_value).__name__} "
                    f"(value={original_value!r}). Only numeric hardvalues "
                    f"are supported for solver variables.",
                )

            self._restoration_table.append(
                _RestorationEntry(
                    cube_id=var.cube_id,
                    addr=addr,
                    original_value=original_value,
                    was_override=was_override,
                )
            )

        # Also validate objective and constraint cells exist.
        for obj in problem.objectives:
            cube = self._require_cube_by_id(obj.cube_id)
            # Objectives are read-only — no restoration needed, but must exist.
            _ = cube  # existence check

        for con in problem.constraints:
            cube = self._require_cube_by_id(con.cube_id)
            _ = cube  # existence check

        logger.debug(
            "SolverEvaluationSession created: %d variables, %d objectives, %d constraints",
            len(problem.variables),
            len(problem.objectives),
            len(problem.constraints),
        )

    # --- Public API --------------------------------------------------------

    def evaluate_candidate(self, values: list[float]) -> dict[str, Any]:
        """Evaluate a single candidate point.

        Args:
            values: Decision variable values, one per variable in the problem.

        Returns:
            Dict with ``objectives`` (list[float]) and ``constraints``
            (list[float]) keys.

        Raises:
            SolverException: If evaluation fails or session is closed.
        """
        if self._closed:
            raise SolverException(
                SOLVER_RESTORATION_FAILED,
                "Cannot evaluate on a closed session.",
            )
        if len(values) != len(self._problem.variables):
            raise SolverException(
                SOLVER_UNSUPPORTED_FEATURE,
                f"Expected {len(self._problem.variables)} values, "
                f"got {len(values)}.",
            )

        with self._lock:
            self.live_eval_count += 1
            try:
                self._write_candidate(values)
                self._recalculate()
                result = self._read_objectives_and_constraints()
                # Restore original values after every evaluation.
                self._restore()
                return result
            except Exception:
                # On any failure, attempt restoration before re-raising.
                logger.exception("Candidate evaluation failed, attempting restoration")
                self._restore()
                raise

    def close(self) -> None:
        """Close the session: restore original values and release lease.

        Must be called exactly once.  Idempotent (subsequent calls are no-ops).
        """
        if self._closed:
            return
        with self._lock:
            self._closed = True
            try:
                self._restore()
            except Exception:
                logger.exception("Restoration failed during session close")
                raise

    # --- Context manager support ------------------------------------------

    def __enter__(self) -> "SolverEvaluationSession":
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    # --- Internal: write candidate values ---------------------------------

    def _require_cube_by_id(self, cube_id: str) -> Cube:
        """Require a cube by ID — works for both local and remote engines."""
        return self._engine.require_cube_by_id(cube_id)

    def _write_candidate(self, values: list[float]) -> None:
        """Write candidate values, bypassing undo/events."""
        if self._is_remote:
            self._write_candidate_remote(values)
        else:
            self._write_candidate_local(values)

    def _write_candidate_local(self, values: list[float]) -> None:
        """Write candidate values directly to cube.data (local engine)."""
        for var, value in zip(self._problem.variables, values):
            cube = self._core.require_cube_by_id(var.cube_id)
            addr = var.addr
            cube.set(addr, value)
            cube.user_override_addrs.add(addr)

        self._core.clear_cell_cache()
        for var in self._problem.variables:
            self._core.invalidate_cell(var.cube_id, var.addr)

    def _write_candidate_remote(self, values: list[float]) -> None:
        """Write candidate values via existing RPC methods (remote engine)."""
        # Group entries by cube_id for batch calls.
        by_cube: dict[str, list[tuple[tuple[str, ...], float]]] = {}
        for var, value in zip(self._problem.variables, values):
            by_cube.setdefault(var.cube_id, []).append((var.addr, value))

        was_suppressed = getattr(self._engine, "_suppress_events", False)
        self._engine._suppress_events = True
        try:
            for cube_id, entries in by_cube.items():
                self._engine.batch_set_cell_hardvalues_by_addr(cube_id, entries)
        finally:
            self._engine._suppress_events = was_suppressed

        # Clear caches on the remote server.
        self._engine.clear_caches()

    # --- Internal: recalculate --------------------------------------------

    def _recalculate(self) -> None:
        """Trigger recalculation of dirty nodes."""
        self._engine.recompute_dirty_nodes(include_all=False)

    # --- Internal: read results -------------------------------------------

    def _read_objectives_and_constraints(self) -> dict[str, Any]:
        """Read objective and constraint cell values after recalculation."""
        objectives: list[float] = []
        for obj in self._problem.objectives:
            cube = self._require_cube_by_id(obj.cube_id)
            raw = self._engine.get_cell_by_addr(cube, obj.addr)
            value = self._coerce_numeric(raw, obj.display_ref)
            objectives.append(value)

        constraints: list[float] = []
        for con in self._problem.constraints:
            cube = self._require_cube_by_id(con.cube_id)
            raw = self._engine.get_cell_by_addr(cube, con.addr)
            value = self._coerce_numeric(raw, con.display_ref)
            constraints.append(value)

        return {
            "objectives": objectives,
            "constraints": constraints,
        }

    @staticmethod
    def _coerce_numeric(value: Any, display_ref: str) -> float:
        """Coerce a cell value to float, raising on non-numeric or error values."""
        from lib_openm.rule_eval.utils import CellError

        if isinstance(value, CellError):
            raise SolverException(
                SOLVER_RESTORATION_VALUE_UNSUPPORTED,
                f"Cell {display_ref} returned error: {value.code}",
            )
        if value is None:
            return 0.0
        if isinstance(value, bool):
            return 1.0 if value else 0.0
        if isinstance(value, (int, float)):
            import math
            if math.isnan(value) or math.isinf(value):
                raise SolverException(
                    SOLVER_RESTORATION_VALUE_UNSUPPORTED,
                    f"Cell {display_ref} returned NaN/inf: {value}",
                )
            return float(value)
        # Non-numeric text etc.
        raise SolverException(
            SOLVER_RESTORATION_VALUE_UNSUPPORTED,
            f"Cell {display_ref} returned non-numeric value: {value!r}",
        )

    # --- Internal: restore original values --------------------------------

    def _restore(self) -> None:
        """Restore all variable cells to their original state.

        This is called after every candidate evaluation (in evaluate_candidate)
        and during session close.  It writes original values back,
        restores user_override_addrs membership, clears caches, invalidates
        dep graph nodes, and recalculates.
        """
        if self._is_remote:
            self._restore_remote()
        else:
            self._restore_local()

    def _restore_local(self) -> None:
        """Restore original values via direct cube.data manipulation (local)."""
        for entry in self._restoration_table:
            cube = self._core.require_cube_by_id(entry.cube_id)
            addr = entry.addr

            if entry.original_value is not None:
                cube.set(addr, entry.original_value)
            else:
                cube.data.pop(addr, None)

            if entry.was_override:
                cube.user_override_addrs.add(addr)
            else:
                cube.user_override_addrs.discard(addr)

        self._core.clear_cell_cache()
        for entry in self._restoration_table:
            self._core.invalidate_cell(entry.cube_id, entry.addr)

        self._core.recompute_dirty_nodes(include_all=False)

    def _restore_remote(self) -> None:
        """Restore original values via existing RPC methods (remote)."""
        # Group entries by cube_id for batch calls.
        by_cube: dict[str, list[tuple[tuple[str, ...], Any]]] = {}
        clear_by_cube: dict[str, list[tuple[str, ...]]] = {}
        for entry in self._restoration_table:
            if entry.original_value is not None:
                by_cube.setdefault(entry.cube_id, []).append(
                    (entry.addr, entry.original_value)
                )
            else:
                clear_by_cube.setdefault(entry.cube_id, []).append(entry.addr)

        was_suppressed = getattr(self._engine, "_suppress_events", False)
        self._engine._suppress_events = True
        try:
            for cube_id, entries in by_cube.items():
                self._engine.batch_set_cell_hardvalues_by_addr(cube_id, entries)
            for cube_id, addrs in clear_by_cube.items():
                for addr in addrs:
                    self._engine.clear_cell_hardvalue_by_addr(cube_id, addr)
        finally:
            self._engine._suppress_events = was_suppressed

        self._engine.clear_caches()
        self._engine.recompute_dirty_nodes(include_all=False)
