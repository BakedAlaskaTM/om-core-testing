"""QueryService — stateless read-side boundary for OpenM."""

from __future__ import annotations

import time
import uuid
from typing import Any

from .executor import ExecutionContext, ExecutionResult, ExecutionStatus
from .message_bus import MessageBus, MessageEnvelope


# Explicit mapping from client-facing query IDs to bus envelope topics.
# Aligned with docs/b-architecture/b-04-query-catalog.md.
QUERY_TYPE_TO_TOPIC: dict[str, str] = {
    # View domain
    "view_detail": "query.view.detail",
    "view_list": "query.view.list",
    "view_row_keys": "query.view.keys",
    "view_col_keys": "query.view.keys",
    "view_row_header": "query.view.headers",
    "view_col_header": "query.view.headers",
    "view_state": "query.view.state",
    "page_selection": "query.view.page_selection",
    # Outline / group domain
    "outline_tree": "query.outline.tree",
    # Selection / current state domain
    "current_view": "query.selection.current_view",
    "current_cube": "query.selection.current_cube",
    "active_view_current": "query.selection.active_view",
    "selection_current": "query.selection.current",
    "selection_stats": "query.selection.stats",
    # Workspace domain
    "workspace_summary": "query.workspace.summary",
    "workspace_snapshot": "query.workspace.snapshot",
    "workspace_rules": "query.workspace.rules",
    # Timeline domain
    "timeline_snapshots": "query.timeline.snapshots",
    # Dimension domain
    "dimension_detail": "query.dimension.detail",
    "dimension_list": "query.dimension.list",
    "dimension_effective_order": "query.dimension.effective_order",
    "dimension_effective_order_window": "query.dimension.effective_order_window",
    "dimension_deletion_impact": "query.dimension.deletion_impact",
    "dimension_item_deletion_impact": "query.dimension.item_deletion_impact",
    # Cube domain
    "cube_detail": "query.cube.detail",
    "cube_list": "query.cube.list",
    "cube_detach_impact": "query.cube.detach_impact",
    "cube_rule_counts": "query.cube.rule_counts",
    # Cell domain
    "cell_detail": "query.cell.value",
    "cell_range": "query.cell.range",
    "addr_resolve": "query.cell.addr",
    "cell_channel_values": "query.cell.channel_values",
    "cell_viewport_range": "query.cell.viewport_range",
    "cell_value_by_ref": "query.cell.value_by_ref",
    "cell_rule": "query.cell.rule",
    "grid_viewport_snapshot": "query.grid.viewport_snapshot",
    "grid_viewport_snapshot_batch": "query.grid.viewport_snapshot_batch",
    # Rule domain
    "rule_detail": "query.rule.detail",
    "rule_target_resolve": "query.rule.target_resolve",
    # UDF domain
    "udf_list": "query.udf.list",
    "udf_detail": "query.udf.detail",
    # Undo domain
    "undo_state": "query.undo.state",
    # Profiler domain
    "profiler_list": "query.profiler.list",
    # Diagnostics domain
    "diagnostics_calculation_flow": "query.diagnostics.calculation_flow",
    "diagnostics_circular_references": "query.diagnostics.circular_references",
    "diagnostics_dependency_tracking_state": "query.diagnostics.dependency_tracking_state",
    "diagnostics_dependency_metrics": "query.diagnostics.dependency_metrics",
    "diagnostics_dirty_count": "query.diagnostics.dirty_count",
    "diagnostics_multithread_config": "query.diagnostics.multithread_config",
    "diagnostics_rule_eval_profile": "query.diagnostics.rule_eval_profile",
    "diagnostics_engine_backend": "query.diagnostics.engine_backend",
}

# Reverse lookup used by the service when a topic arrives with a payload.
# Multiple query IDs may map to the same topic; the payload carries the query_type.
QUERY_TOPICS: set[str] = set(QUERY_TYPE_TO_TOPIC.values())


def topic_for_query_type(query_type: str) -> str:
    """Return the canonical bus topic for a query ID.

    Unregistered query IDs use the legacy topic shape ``query.<query_type>`` so
    they still route through the bus while remaining explicitly outside the
    stable catalog.
    """
    return QUERY_TYPE_TO_TOPIC.get(query_type, f"query.{query_type}")


class QueryService:
    """
    Serves reads without mutating canonical state.

    Phase 4: subscribes to ``query.*`` bus topics and replies via the bus.
    Phase 5: may read from Read Models for projected UI reads.

    Design: stateless — receives ExecutionContext from the envelope so it never
    captures a global engine reference.
    """

    def __init__(self, bus: MessageBus | None = None) -> None:
        self.bus = bus

    def subscribe(self, bus: MessageBus | None = None) -> None:
        """Register this service as a subscriber for all query bus topics."""
        self.bus = bus or self.bus
        if self.bus is None:
            raise RuntimeError("QueryService requires a MessageBus to subscribe")
        # Wildcard patterns cover all query topics:
        #   query.*    → 2-segment topics (e.g., query.something)
        #   query.*.*  → 3-segment topics (e.g., query.view.detail)
        # The bus deduplication prevents double-dispatch when a topic
        # matches multiple patterns.
        self.bus.subscribe("query.*", self._handle_query_envelope)
        self.bus.subscribe("query.*.*", self._handle_query_envelope)

    def _handle_query_envelope(self, envelope: MessageEnvelope) -> None:
        """Handle an incoming query envelope and publish a correlated reply."""
        if not isinstance(envelope, MessageEnvelope):
            return
        if envelope.message_type != "query":
            return
        if not envelope.reply_to:
            return

        payload = dict(envelope.payload or {})
        query_type = payload.pop("query_type", None)
        if not query_type:
            # Fallback: derive from topic if payload omitted query_type.
            query_type = envelope.topic.split(".", 1)[1] if "." in envelope.topic else None
            if not query_type:
                return

        result = self.execute(envelope.context, query_type, **payload)
        reply_envelope = MessageEnvelope(
            message_id=uuid.uuid4().hex,
            message_type="reply",
            topic=envelope.reply_to,
            correlation_id=envelope.correlation_id,
            session_id=envelope.session_id,
            client_type=envelope.client_type,
            workspace_id=envelope.workspace_id,
            actor_id=envelope.actor_id,
            timestamp=time.perf_counter(),
            payload={
                "status": result.status.name,
                "data": result.data,
                "error": result.error,
            },
            context=None,
            status="succeeded" if result.success else "failed",
            reply_to=None,
        )
        self.bus.publish(envelope.reply_to, reply_envelope)

    def handle(
        self,
        ctx: ExecutionContext,
        query_type: str,
        **params
    ) -> Any:
        """Dispatch to existing query handlers using the caller's session context."""
        from ..commands.query import cmd_query

        if not query_type:
            raise ValueError("Query type is required")

        return cmd_query(ctx, query_type, **params)

    def execute(self, ctx: ExecutionContext, query_type: str, **params) -> ExecutionResult:
        """Dispatch a query and wrap the result in an ExecutionResult."""
        try:
            data = self.handle(ctx, query_type, **params)
            return ExecutionResult(
                status=ExecutionStatus.SUCCESS,
                command_id="query",
                data=data,
            )
        except Exception as e:
            return ExecutionResult(
                status=ExecutionStatus.ERROR,
                command_id="query",
                error=str(e),
            )
