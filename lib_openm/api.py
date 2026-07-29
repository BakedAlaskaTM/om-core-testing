from __future__ import annotations

import importlib.metadata
from typing import Any, Callable

from lib_contracts.types import RuleValidationError
from lib_openm.engine_state import (
    EngineState,
    EngineBusyError,
    EngineFaultedError,
    EngineShuttingDownError,
)
from lib_openm._engine_core import (
    _EngineCore,
    AddAggregateItemResult,
    CellMeta,
    CellValue,
    Explain,
    RuleDiagnostic,
    iter_view_ids,
)
from lib_openm.model import Workspace


class Engine:
    """Public facade over the private engine core."""

    def __init__(self, workspace: Workspace, *, event_publisher=None) -> None:
        self._core = _EngineCore(self, workspace, event_publisher=event_publisher)

    def __getattr__(self, name: str):
        # Delegate private/internal attribute access to the core. This keeps
        # existing tests and internal callers working while the public facade
        # only exposes the documented public API.
        if name.startswith("_"):
            return getattr(self._core, name)
        raise AttributeError(f"{type(self).__name__!r} object has no attribute {name!r}")

    def __setattr__(self, name: str, value) -> None:
        # Keep private/internal state on the core so tests and internal callers
        # that mutate engine internals see the same state the public methods read.
        if name == "_core" or not name.startswith("_") or not hasattr(self, "_core"):
            super().__setattr__(name, value)
        else:
            setattr(self._core, name, value)

    # ------------------------------------------------------------------
    # Engine lifecycle / state machine (Phase 1)
    # ------------------------------------------------------------------

    def read_engine_state(self):
        return self._core._state_machine.get_engine_state()

    def read_engine_diagnostics(self):
        return self._core._state_machine.get_diagnostics()

    def engine_info(self) -> dict[str, Any]:
        """Return engine backend type, package version, and connection state."""
        backend = "python"
        connected = True
        server_version: str | None = None
        version = self._engine_version()
        result: dict[str, Any] = {
            "type": backend,
            "version": version,
            "connected": connected,
        }
        if server_version is not None:
            result["server_version"] = server_version
        return result

    def _engine_version(self) -> str:
        """Resolve the om-core package version from metadata or pyproject.toml."""
        try:
            return importlib.metadata.version("om-core")
        except importlib.metadata.PackageNotFoundError:
            pass
        try:
            from pathlib import Path
            import tomllib

            pyproject = Path(__file__).resolve().parent.parent / "pyproject.toml"
            with pyproject.open("rb") as f:
                data = tomllib.load(f)
            return str(data.get("project", {}).get("version", "unknown"))
        except Exception:
            return "unknown"

    def execute_serialized_command(
        self,
        command_id: str,
        allowed_states: set[EngineState],
        target_state: EngineState,
        body: Callable[[], Any],
        *,
        is_recovery: bool = False,
        next_state: EngineState | None = None,
        next_state_reason: str | None = None,
    ) -> Any:
        return self._core._state_machine.execute_serialized_command(
            command_id,
            allowed_states,
            target_state,
            body,
            is_recovery=is_recovery,
            next_state=next_state,
            next_state_reason=next_state_reason,
        )

    def transition_to_state(self, new_state: EngineState, *, reason: str | None = None) -> None:
        self._core._state_machine.transition_to(new_state, reason=reason)

    def request_cancel_operation(self) -> None:
        self._core._state_machine.request_cancel()

    def is_cancel_operation_requested(self) -> bool:
        return self._core._state_machine.is_cancel_requested()

    def shutdown_engine(self) -> None:
        self._core._state_machine.shutdown()

    @property
    def undo_manager(self):
        return getattr(self._core, 'undo_manager')

    @property
    def workspace(self):
        return getattr(self._core, 'workspace')

    def addr_to_cell_ref(self, *args, **kwargs):
        return self._core.addr_to_cell_ref(*args, **kwargs)

    def analyze_detach_dimension_from_cube(self, *args, **kwargs):
        return self._core.analyze_detach_dimension_from_cube(*args, **kwargs)

    def analyze_dimension_deletion_impact(self, *args, **kwargs):
        return self._core.analyze_dimension_deletion_impact(*args, **kwargs)

    def analyze_dimension_item_deletion(self, *args, **kwargs):
        return self._core.analyze_dimension_item_deletion(*args, **kwargs)

    def apply_rule_batch(self, *args, **kwargs):
        return self._core.apply_rule_batch(*args, **kwargs)

    def attach_dimension_to_cube(self, *args, **kwargs):
        return self._core.attach_dimension_to_cube(*args, **kwargs)

    def batch_set_cell_data(self, *args, **kwargs):
        return self._core.batch_set_cell_data(*args, **kwargs)

    def batch_set_cell_hardvalues_by_addr(self, *args, **kwargs):
        return self._core.batch_set_cell_hardvalues_by_addr(*args, **kwargs)

    def bootstrap_dependency_graph(self, *args, **kwargs):
        return self._core.bootstrap_dependency_graph(*args, **kwargs)

    @property
    def generation(self):
        return self._core.generation

    @property
    def is_gui_ready(self):
        return self._core.is_gui_ready

    def bump_generation(self, *args, **kwargs):
        return self._core.bump_generation(*args, **kwargs)

    def can_redo(self, *args, **kwargs):
        return self._core.can_redo(*args, **kwargs)

    def can_undo(self, *args, **kwargs):
        return self._core.can_undo(*args, **kwargs)

    def cell_value_for_view_rc(self, *args, **kwargs):
        return self._core.cell_value_for_view_rc(*args, **kwargs)

    def clear_cell_hardvalue(self, *args, **kwargs):
        return self._core.clear_cell_hardvalue(*args, **kwargs)

    def create_aggregate_item(self, *args, **kwargs):
        return self._core.create_aggregate_item(*args, **kwargs)

    def create_cube(self, *args, **kwargs):
        return self._core.create_cube(*args, **kwargs)

    def create_dimension(self, *args, **kwargs):
        return self._core.create_dimension(*args, **kwargs)

    def create_default_view_for_cube(self, *args, **kwargs):
        return self._core.create_default_view_for_cube(*args, **kwargs)

    def create_dimension_item(self, *args, **kwargs):
        return self._core.create_dimension_item(*args, **kwargs)

    def batch_create_dimension_items(self, *args, **kwargs):
        return self._core.batch_create_dimension_items(*args, **kwargs)

    def create_group(self, *args, **kwargs):
        return self._core.create_group(*args, **kwargs)

    def create_view(self, *args, **kwargs):
        return self._core.create_view(*args, **kwargs)

    def delete_cube(self, *args, **kwargs):
        return self._core.delete_cube(*args, **kwargs)

    def delete_dimension(self, *args, **kwargs):
        return self._core.delete_dimension(*args, **kwargs)

    def delete_dimension_items(self, *args, **kwargs):
        return self._core.delete_dimension_items(*args, **kwargs)

    def delete_group_node(self, *args, **kwargs):
        return self._core.delete_group_node(*args, **kwargs)

    def delete_rule(self, *args, **kwargs):
        return self._core.delete_rule(*args, **kwargs)

    def delete_rule_anchored(self, *args, **kwargs):
        return self._core.delete_rule_anchored(*args, **kwargs)

    def dependency_metrics(self, *args, **kwargs):
        return self._core.dependency_metrics(*args, **kwargs)

    def detach_dimension_from_cube(self, *args, **kwargs):
        return self._core.detach_dimension_from_cube(*args, **kwargs)

    def dimension_outline_for_dim(self, *args, **kwargs):
        return self._core.dimension_outline_for_dim(*args, **kwargs)

    def enable_dependency_tracking(self, *args, **kwargs):
        return self._core.enable_dependency_tracking(*args, **kwargs)

    def dependency_tracking_disabled(self, *args, **kwargs):
        return self._core.dependency_tracking_disabled(*args, **kwargs)

    def enable_multithread_recompute(self, *args, **kwargs):
        return self._core.enable_multithread_recompute(*args, **kwargs)

    def evaluate_all_cubes_bruteforce(self, *args, **kwargs):
        return self._core.evaluate_all_cubes_bruteforce(*args, **kwargs)

    def find_cube_by_id(self, *args, **kwargs):
        return self._core.find_cube_by_id(*args, **kwargs)

    def find_cube_by_name(self, *args, **kwargs):
        return self._core.find_cube_by_name(*args, **kwargs)

    def find_anchored_rule(self, *args, **kwargs):
        return self._core.workspace.find_anchored_rule(*args, **kwargs)

    def find_rule(self, *args, **kwargs):
        return self._core.find_rule(*args, **kwargs)

    def get_cell_by_addr(self, *args, **kwargs):
        return self._core.get_cell_by_addr(*args, **kwargs)

    def get_cached_cell_value_by_addr(self, *args, **kwargs):
        return self._core.get_cached_cell_value_by_addr(*args, **kwargs)

    def get_cell_value(self, *args, **kwargs):
        return self._core.get_cell_value(*args, **kwargs)

    def resolve_cell_meta(self, *args, **kwargs):
        return self._core.resolve_cell_meta(*args, **kwargs)

    def get_cells_batch(self, *args, **kwargs):
        return self._core.get_cells_batch(*args, **kwargs)

    def get_range(self, *args, **kwargs):
        return self._core.get_range(*args, **kwargs)

    def get_redo_description(self, *args, **kwargs):
        return self._core.get_redo_description(*args, **kwargs)

    def get_undo_description(self, *args, **kwargs):
        return self._core.get_undo_description(*args, **kwargs)

    def has_system_graph_cubes(self, *args, **kwargs):
        return self._core.has_system_graph_cubes(*args, **kwargs)

    @property
    def is_calculating(self):
        return self._core.is_calculating

    def is_cancel_requested(self, *args, **kwargs):
        return self._core.is_cancel_requested(*args, **kwargs)

    def list_cube_ids(self, *args, **kwargs):
        return self._core.list_cube_ids(*args, **kwargs)

    def list_dependents(self, *args, **kwargs):
        return self._core.list_dependents(*args, **kwargs)

    def list_precedents(self, *args, **kwargs):
        return self._core.list_precedents(*args, **kwargs)

    def list_views(self, *args, **kwargs):
        return self._core.list_views(*args, **kwargs)

    def move_items_to_group(self, *args, **kwargs):
        return self._core.move_items_to_group(*args, **kwargs)

    def move_nodes(self, *args, **kwargs):
        return self._core.move_nodes(*args, **kwargs)

    def move_view_dimension(self, *args, **kwargs):
        return self._core.move_view_dimension(*args, **kwargs)

    def place_item_nodes(self, *args, **kwargs):
        return self._core.place_item_nodes(*args, **kwargs)

    def recalculate_all(self, *args, **kwargs):
        return self._core.recalculate_all(*args, **kwargs)

    def recompute_dirty_nodes(self, *args, **kwargs):
        return self._core.recompute_dirty_nodes(*args, **kwargs)

    def dirty_count(self, *args, **kwargs):
        return self._core.dirty_count(*args, **kwargs)

    def has_dirty_nodes(self, *args, **kwargs):
        return self._core.has_dirty_nodes(*args, **kwargs)

    def redo(self, *args, **kwargs):
        return self._core.redo(*args, **kwargs)

    def rename_cube(self, *args, **kwargs):
        return self._core.rename_cube(*args, **kwargs)

    def rename_dimension(self, *args, **kwargs):
        return self._core.rename_dimension(*args, **kwargs)

    def rename_dimension_item(self, *args, **kwargs):
        return self._core.rename_dimension_item(*args, **kwargs)

    def rename_group_node(self, *args, **kwargs):
        return self._core.rename_group_node(*args, **kwargs)

    def reorder_nodes(self, *args, **kwargs):
        return self._core.reorder_nodes(*args, **kwargs)

    def replace_workspace(self, *args, **kwargs):
        return self._core.replace_workspace(*args, **kwargs)

    def request_cancel(self, *args, **kwargs):
        return self._core.request_cancel(*args, **kwargs)

    def require_cube_by_id(self, *args, **kwargs):
        return self._core.require_cube_by_id(*args, **kwargs)

    def require_dimension_by_id(self, *args, **kwargs):
        return self._core.require_dimension_by_id(*args, **kwargs)

    def require_view_by_id(self, *args, **kwargs):
        return self._core.require_view_by_id(*args, **kwargs)

    def reset_cancel(self, *args, **kwargs):
        return self._core.reset_cancel(*args, **kwargs)

    def reset_profiler_snapshot(self, *args, **kwargs):
        return self._core.reset_profiler_snapshot(*args, **kwargs)

    def reset_rule_eval_profile(self, *args, **kwargs):
        return self._core.reset_rule_eval_profile(*args, **kwargs)

    def resolve_cube_id_by_name(self, *args, **kwargs):
        return self._core.resolve_cube_id_by_name(*args, **kwargs)

    def resolve_default_view_id_by_cube(self, *args, **kwargs):
        return self._core.resolve_default_view_id_by_cube(*args, **kwargs)

    def resolve_item_node_id(self, *args, **kwargs):
        return self._core.resolve_item_node_id(*args, **kwargs)

    def rule_counts_for_cube(self, *args, **kwargs):
        return self._core.rule_counts_for_cube(*args, **kwargs)

    def rule_eval_profile_snapshot(self, *args, **kwargs):
        return self._core.rule_eval_profile_snapshot(*args, **kwargs)

    def set_cell_hardvalue(self, *args, **kwargs):
        return self._core.set_cell_hardvalue(*args, **kwargs)

    def set_cell_hardvalue_by_addr(self, *args, **kwargs):
        return self._core.set_cell_hardvalue_by_addr(*args, **kwargs)

    def set_dimension_item_order(self, *args, **kwargs):
        return self._core.set_dimension_item_order(*args, **kwargs)

    def set_range(self, *args, **kwargs):
        return self._core.set_range(*args, **kwargs)

    def set_rule(self, *args, **kwargs):
        return self._core.set_rule(*args, **kwargs)

    def set_rule_anchored(self, *args, **kwargs):
        return self._core.set_rule_anchored(*args, **kwargs)

    def set_rule_anchored_by_addr(self, *args, **kwargs):
        return self._core.set_rule_anchored_by_addr(*args, **kwargs)

    def set_rule_order(self, *args, **kwargs):
        return self._core.set_rule_order(*args, **kwargs)

    def set_view_axes(self, *args, **kwargs):
        return self._core.set_view_axes(*args, **kwargs)

    def set_view_layout(self, *args, **kwargs):
        return self._core.set_view_layout(*args, **kwargs)

    def trace_calculation_flow(self, *args, **kwargs):
        return self._core.trace_calculation_flow(*args, **kwargs)

    def trace_circular_references(self, *args, **kwargs):
        return self._core.trace_circular_references(*args, **kwargs)

    def undo(self, *args, **kwargs):
        return self._core.undo(*args, **kwargs)

    def ungroup_items(self, *args, **kwargs):
        return self._core.ungroup_items(*args, **kwargs)

    def update_cell_rule(self, *args, **kwargs):
        return self._core.update_cell_rule(*args, **kwargs)

    def update_rule(self, *args, **kwargs):
        return self._core.update_rule(*args, **kwargs)

    def update_rule_full(self, *args, **kwargs):
        return self._core.update_rule_full(*args, **kwargs)

    def view_col_count(self, *args, **kwargs):
        return self._core.view_col_count(*args, **kwargs)

    def view_col_dim_ids(self, *args, **kwargs):
        return self._core.view_col_dim_ids(*args, **kwargs)

    def view_col_header(self, *args, **kwargs):
        return self._core.view_col_header(*args, **kwargs)

    def view_col_items(self, *args, **kwargs):
        return self._core.view_col_items(*args, **kwargs)

    def view_col_keys(self, *args, **kwargs):
        return self._core.view_col_keys(*args, **kwargs)

    def view_page_dim_ids(self, *args, **kwargs):
        return self._core.view_page_dim_ids(*args, **kwargs)

    def view_page_dimensions(self, *args, **kwargs):
        return self._core.view_page_dimensions(*args, **kwargs)

    def view_row_dim_ids(self, *args, **kwargs):
        return self._core.view_row_dim_ids(*args, **kwargs)

    def view_row_items(self, *args, **kwargs):
        return self._core.view_row_items(*args, **kwargs)

    def view_row_keys(self, *args, **kwargs):
        return self._core.view_row_keys(*args, **kwargs)

    # --- Phase 2.5: public API methods replacing internal _core._xxx access ---

    def clear_caches(self, scope: str = "all") -> None:
        if scope == "all":
            self._core._clear_caches()
        elif scope == "cell":
            self._core._clear_cell_cache()

    def dimension_effective_order(self, dim_id: str) -> list[str]:
        return self._core._dimension_effective_order(dim_id)

    def dimension_effective_order_window(
        self, dim_id: str, *, offset: int = 0, limit: int | None = None
    ) -> list[str]:
        return self._core._dimension_effective_order_window(
            dim_id, offset=offset, limit=limit
        )

    def multithread_recompute_config(self) -> dict[str, int]:
        return self._core._multithread_recompute_config()

    def dirty_keys(self) -> list[str]:
        return self._core._dep_graph.dirty_keys()

    def is_dependency_tracking_enabled(self) -> bool:
        return self._core._is_tracking_enabled()

    def addr_for_view_ids(
        self, view_id: str, *, row_key=None, col_key=None
    ) -> tuple[str, ...]:
        return self._core._addr_for_view_ids(view_id, row_key=row_key, col_key=col_key)

    def get_page_item_id(self, view_id: str, dim_id: str) -> str | None:
        return self._core._get_page_item_id(view_id, dim_id)

    def ensure_group_in_graph(
        self, dim_id: str, group_node, parent_group_id: str | None = None
    ) -> str:
        return self._core._ensure_group_in_graph(
            dim_id, group_node, parent_group_id=parent_group_id
        )

    @property
    def engine_lock(self):
        return self._core._engine_lock

