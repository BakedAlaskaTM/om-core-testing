"""SolverEvaluationPort implementation: Engine-backed workspace reads,
evaluation-session creation, live-Engine temporary evaluation.

This adapter bridges the runtime solver service to the Engine.  It
implements ``SolverEvaluationPort`` with:

- ``resolve_cell_reference``: resolves cube_name/channel/selectors to
  canonical cell IDs.
- ``begin_solver_evaluation``: acquires workspace lease, resolves problem,
  captures restoration table, creates evaluation session.

The workspace lease is a runtime-level reservation that prevents other
commands from mutating the workspace during the solver run.  It is
implemented as a per-workspace lock in the adapter, not as an Engine
state machine transition.
"""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any

from lib_openm.api import Engine
from lib_openm.model import Workspace, Cube
from lib_openm.technical_ids import CHANNEL_TO_AT_ID

from lib_runtime.solver.cancellation_token import CancellationToken
from lib_runtime.solver.solver_errors import (
    SolverException,
    SOLVER_WORKSPACE_BUSY,
    SOLVER_RESULT_STALE,
    SOLVER_VARIABLE_MISSING_INITIAL,
    SOLVER_VARIABLE_BOUNDS_INVALID,
    SOLVER_CELL_ERROR,
    SOLVER_RESTORATION_VALUE_UNSUPPORTED,
    SOLVER_MALFORMED_ADDRESS,
)
from lib_runtime.solver.solver_types import (
    SolverProblem,
    SolverVariable,
    SolverObjective,
    SolverConstraint,
)
from lib_runtime.solver.solver_evaluation_session import SolverEvaluationSession
from lib_runtime.solver.solver_problem import (
    ResolvedSolverEvaluationSession,
    validate_problem,
    compute_model_fingerprint,
    compute_solve_fingerprint,
)

logger = logging.getLogger(__name__)


def _compute_workspace_revision(engine: Engine) -> str:
    """Compute a simple content-hash revision for the workspace.

    Phase 1: This hashes only user override hardvalues and rule
    definitions — the canonical inputs.  Computed values are excluded
    because they change during recalculation and are not canonical state.
    """
    ws = engine.workspace
    parts: list[str] = []
    for cube_id in sorted(ws.cubes.keys()):
        cube = ws.cubes[cube_id]
        # Only hash user override addresses (canonical hardvalues).
        for addr in sorted(cube.user_override_addrs):
            val = cube.data.get(addr)
            parts.append(f"{cube_id}:ovr:{addr}:{val}")
    # Hash rules (canonical formula definitions).
    for rule_id in sorted(ws.rule_order):
        rule = ws.rules.get(rule_id)
        if rule is not None:
            parts.append(f"rule:{rule_id}:{rule.expression}")
    content = "|".join(parts)
    return hashlib.sha256(content.encode()).hexdigest()[:16]


class EngineSolverEvaluationAdapter:
    """Implements ``SolverEvaluationPort`` against a live Engine instance.

    Manages per-workspace solver leases to prevent concurrent solver
    jobs on the same workspace.
    """

    def __init__(self, engine: Engine) -> None:
        self._engine = engine
        self._workspace_leases: dict[str, threading.Lock] = {}
        self._leases_lock = threading.Lock()
        self._leased_workspaces: set[str] = set()

    def _get_core(self):
        """Return the _EngineCore for direct cube manipulation.

        Returns None for RemoteEngine — callers must fall back to the
        engine's public API in that case.
        """
        return getattr(self._engine, "_core", None)

    def resolve_cell_reference(self, workspace_id: str, cell_ref: dict) -> dict:
        """Resolve a cell reference to canonical IDs.

        Expected ``cell_ref`` keys:
        - ``cube_name``: name of the cube
        - ``channel``: optional channel name (default: "value")
        - ``selectors``: dict mapping dimension names to item names

        Returns dict with ``workspace_id``, ``cube_id``, ``address_ids``,
        and ``display_ref``.
        """
        ws = self._engine.workspace
        cube_name = cell_ref.get("cube_name")
        if cube_name is None:
            raise SolverException(
                SOLVER_MALFORMED_ADDRESS,
                "cell_ref missing 'cube_name'",
            )

        cube = None
        for c in ws.cubes.values():
            if c.name == cube_name:
                cube = c
                break
        if cube is None:
            raise SolverException(
                SOLVER_MALFORMED_ADDRESS,
                f"Cube not found: {cube_name}",
            )

        channel = cell_ref.get("channel", "value")
        at_id = CHANNEL_TO_AT_ID.get(channel, f"at_{channel}")

        selectors = cell_ref.get("selectors", {})

        # Build address: at_id + dimension items in cube dimension order.
        addr_parts: list[str] = [at_id]
        for dim_id in cube.dimension_ids:
            if dim_id == "@":
                continue  # Already handled by at_id
            dim = ws.dimensions.get(dim_id)
            if dim is None:
                raise SolverException(
                    SOLVER_MALFORMED_ADDRESS,
                    f"Dimension not found: {dim_id}",
                )
            item_name = selectors.get(dim.name)
            if item_name is None:
                raise SolverException(
                    SOLVER_MALFORMED_ADDRESS,
                    f"Selector missing for dimension '{dim.name}' in cell_ref",
                )
            # Find item ID by name.
            item_id = None
            for item in dim.items:
                if item.name == item_name:
                    item_id = item.id
                    break
            if item_id is None:
                raise SolverException(
                    SOLVER_MALFORMED_ADDRESS,
                    f"Item '{item_name}' not found in dimension '{dim.name}'",
                )
            addr_parts.append(item_id)

        addr = tuple(addr_parts)

        # Build display ref: CubeName::Dim.Item:Dim.Item
        display_parts = []
        for dim_id in cube.dimension_ids:
            if dim_id == "@":
                continue
            dim = ws.dimensions.get(dim_id)
            if dim is None:
                continue
            item_name = selectors.get(dim.name, "?")
            display_parts.append(f"{dim.name}.{item_name}")
        display_ref = f"{cube_name}::{':'.join(display_parts)}"

        return {
            "workspace_id": workspace_id,
            "cube_id": cube.id,
            "address_ids": addr,
            "display_ref": display_ref,
        }

    def begin_solver_evaluation(
        self,
        workspace_id: str,
        problem_spec: dict,
        expected_revision: str | None,
        limits: dict,
        cancellation_token: CancellationToken,
    ) -> ResolvedSolverEvaluationSession:
        """Begin a solver evaluation session against the live Engine."""
        telemetry: dict[str, float] = {}

        # 1. Acquire workspace lease.
        t0 = time.time()
        if not self._acquire_lease(workspace_id):
            raise SolverException(
                SOLVER_WORKSPACE_BUSY,
                f"Workspace {workspace_id} is already leased by another solver job",
            )
        telemetry["lease_acquisition_ms"] = (time.time() - t0) * 1000.0

        try:
            # 2. Verify revision.
            current_rev = _compute_workspace_revision(self._engine)
            if expected_revision is not None and expected_revision != current_rev:
                self._release_lease(workspace_id)
                raise SolverException(
                    SOLVER_RESULT_STALE,
                    f"Revision mismatch: expected {expected_revision}, "
                    f"current {current_rev}",
                )

            # 3. Resolve all solver references (primary + auxiliary cell refs).
            t1 = time.time()
            resolved_refs: dict[str, dict] = {}

            def _collect_ref(ref_obj: Any) -> None:
                """Collect a cell ref dict for resolution if not already resolved."""
                if ref_obj is None or not isinstance(ref_obj, dict):
                    return
                ref_key = str(ref_obj)
                if ref_key not in resolved_refs:
                    resolved_refs[ref_key] = self.resolve_cell_reference(
                        workspace_id, ref_obj
                    )

            for section in ("variables", "objectives", "constraints"):
                for entry in problem_spec.get(section, []):
                    # Primary cell ref.
                    _collect_ref(entry.get("cell_ref") or entry.get("ref"))
                    # Variable bounds can be cell refs.
                    if section == "variables":
                        for bound_key in ("lower_bound", "upper_bound"):
                            bv = entry.get(bound_key)
                            if isinstance(bv, dict) and "cube_name" in bv:
                                _collect_ref(bv)
                    # Objective direction can be a cell ref.
                    if section == "objectives":
                        direction = entry.get("direction")
                        if isinstance(direction, dict) and "direction_ref" in direction:
                            _collect_ref(direction["direction_ref"])
                    # Constraint bounds can be cell refs.
                    if section == "constraints":
                        for bound_key in ("lower_bound", "upper_bound", "bound"):
                            bv = entry.get(bound_key)
                            if isinstance(bv, dict) and "cube_name" in bv:
                                _collect_ref(bv)

            # Limit values can be cell refs.
            for limit_val in problem_spec.get("limits", {}).values():
                if isinstance(limit_val, dict) and "cell_ref" in limit_val:
                    _collect_ref(limit_val["cell_ref"])

            telemetry["reference_resolution_ms"] = (time.time() - t1) * 1000.0

            # 4. Build SolverProblem from spec + resolved refs.
            problem = self._build_problem(problem_spec, resolved_refs)

            # 5. Validate.
            validate_problem(problem)

            # 6. Compute fingerprints.
            t2 = time.time()
            model_fp = compute_model_fingerprint(problem)
            solve_fp = compute_solve_fingerprint(problem, current_rev, limits)
            telemetry["fingerprint_ms"] = (time.time() - t2) * 1000.0

            # 7. Create evaluation session.
            t3 = time.time()
            session = SolverEvaluationSession(
                self._engine,
                problem,
            )
            telemetry["restoration_capture_ms"] = (time.time() - t3) * 1000.0

            return ResolvedSolverEvaluationSession(
                session=session,
                problem=problem,
                base_revision=current_rev,
                model_fingerprint=model_fp,
                solve_fingerprint=solve_fp,
                telemetry=telemetry,
            )
        except SolverException:
            self._release_lease(workspace_id)
            raise
        except Exception:
            self._release_lease(workspace_id)
            raise

    def release_lease(self, workspace_id: str) -> None:
        """Release the workspace solver lease."""
        self._release_lease(workspace_id)

    def is_workspace_leased(self, workspace_id: str) -> bool:
        """Check if a workspace is currently leased by a solver."""
        with self._leases_lock:
            return workspace_id in self._leased_workspaces

    def get_current_revision(self) -> str:
        """Return the current workspace revision."""
        return _compute_workspace_revision(self._engine)

    # --- Internal: lease management ---------------------------------------

    def _acquire_lease(self, workspace_id: str) -> bool:
        with self._leases_lock:
            if workspace_id in self._leased_workspaces:
                return False
            self._leased_workspaces.add(workspace_id)
            return True

    def _release_lease(self, workspace_id: str) -> None:
        with self._leases_lock:
            self._leased_workspaces.discard(workspace_id)

    # --- Internal: problem building --------------------------------------

    def _read_cell_value(
        self,
        resolved: dict,
    ) -> float:
        """Read a numeric cell value from a resolved cell reference."""
        cube = self._engine.require_cube_by_id(resolved["cube_id"])
        raw = cube.get(resolved["address_ids"])
        if raw is None:
            return 0.0
        if isinstance(raw, (int, float)) and not isinstance(raw, bool):
            return float(raw)
        # Try string-to-float for cells containing numeric strings.
        try:
            return float(raw)
        except (TypeError, ValueError):
            return 0.0

    def _resolve_bound_value(
        self,
        bound: Any,
        resolved_refs: dict,
    ) -> float | None:
        """Resolve a bound value that may be a float, a cell-ref dict, or None."""
        if bound is None:
            return None
        if isinstance(bound, (int, float)) and not isinstance(bound, bool):
            return float(bound)
        if isinstance(bound, dict) and "cube_name" in bound:
            resolved = resolved_refs[str(bound)]
            return self._read_cell_value(resolved)
        return None
    
    def _resolve_direction(
        self,
        direction: Any,
        resolved_refs: dict,
    ) -> str:
        """Resolve direction to 'minimize' or 'maximize'.
        
        Accepts literal strings, or a dict with 'direction_ref' pointing to
        a cell ref whose value is 'min'/'max' or 1/-1.
        """
        if isinstance(direction, str):
            if direction in ("min", "minimize"):
                return "minimize"
            if direction in ("max", "maximize"):
                return "maximize"
        if isinstance(direction, dict) and "direction_ref" in direction:
            ref = direction["direction_ref"]
            resolved = resolved_refs[str(ref)]
            raw = self._read_cell_value(resolved)
            if raw >= 0:
                return "maximize"
            else:
                return "minimize"
        return "minimize"

    def _build_problem(
        self,
        problem_spec: dict,
        resolved_refs: dict,
    ) -> SolverProblem:
        """Build a SolverProblem from the problem spec and resolved refs."""
        variables: list[SolverVariable] = []
        for var_spec in problem_spec.get("variables", []):
            ref = var_spec.get("cell_ref") or var_spec.get("ref")
            ref_key = str(ref)
            resolved = resolved_refs[ref_key]

            # Read initial value from the engine.
            cube = self._engine.require_cube_by_id(resolved["cube_id"])
            addr = resolved["address_ids"]
            initial = cube.get(addr)
            if initial is None:
                initial = var_spec.get("initial_value", 0.0)
            if not isinstance(initial, (int, float)) or isinstance(initial, bool):
                # Use spec initial_value as fallback.
                initial = var_spec.get("initial_value", 0.0)

            # Resolve bounds — may be floats or cell refs.
            lower_bound = self._resolve_bound_value(
                var_spec.get("lower_bound"), resolved_refs
            )
            upper_bound = self._resolve_bound_value(
                var_spec.get("upper_bound"), resolved_refs
            )

            variables.append(SolverVariable(
                cube_id=resolved["cube_id"],
                addr=addr,
                initial_value=float(initial),
                lower_bound=lower_bound,
                upper_bound=upper_bound,
                display_ref=resolved["display_ref"],
            ))

        objectives: list[SolverObjective] = []
        for obj_spec in problem_spec.get("objectives", []):
            ref = obj_spec.get("cell_ref") or obj_spec.get("ref")
            ref_key = str(ref)
            resolved = resolved_refs[ref_key]
            direction = self._resolve_direction(
                obj_spec.get("direction", "minimize"), resolved_refs
            )
            objectives.append(SolverObjective(
                cube_id=resolved["cube_id"],
                addr=resolved["address_ids"],
                direction=direction,
                display_ref=resolved["display_ref"],
            ))

        constraints: list[SolverConstraint] = []
        for con_spec in problem_spec.get("constraints", []):
            ref = con_spec.get("cell_ref") or con_spec.get("ref")
            ref_key = str(ref)
            resolved = resolved_refs[ref_key]
            constraints.append(SolverConstraint(
                cube_id=resolved["cube_id"],
                addr=resolved["address_ids"],
                constraint_type=con_spec.get("type", "lower"),
                bound_value=self._resolve_bound_value(
                    con_spec.get("bound"), resolved_refs
                ),
                lower_bound_value=self._resolve_bound_value(
                    con_spec.get("lower_bound"), resolved_refs
                ),
                upper_bound_value=self._resolve_bound_value(
                    con_spec.get("upper_bound"), resolved_refs
                ),
                display_ref=resolved["display_ref"],
            ))

        # Resolve limit values that are cell refs.
        resolved_limits: dict[str, Any] = {}
        for key, val in problem_spec.get("limits", {}).items():
            if isinstance(val, dict) and "cell_ref" in val:
                ref = val["cell_ref"]
                resolved = resolved_refs[str(ref)]
                resolved_limits[key] = self._read_cell_value(resolved)
            else:
                resolved_limits[key] = val

        return SolverProblem(
            variables=variables,
            objectives=objectives,
            constraints=constraints,
            backend_id=problem_spec.get("backend", "scipy"),
            algorithm=problem_spec.get("algorithm", "cobyla"),
            limits=resolved_limits,
            options=problem_spec.get("options", {}),
        )
