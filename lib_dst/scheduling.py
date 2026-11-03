"""Trace-recorded virtual delivery policy and scheduled DST execution."""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Any

from lib_command.core.message_bus import MessageEnvelope, _topic_matches
from lib_command.core.scheduled_message_bus import ScheduledMessageBus
from lib_dst.executor import (
    create_trace_runtime,
    execute_operations,
    report_runtime,
)
from lib_dst.schemas import ExecutionReport, TraceTranscript
from lib_dst.seeding import SeedStreams
from lib_runtime.lifecycle import reset_runtime_state
from lib_runtime.virtual_scheduler import VirtualScheduler


SCHEDULER_POLICY_VERSION = 1
QUERY_TIMEOUT_NS = 5_000_000_000


@dataclass(frozen=True)
class DeliveryDecision:
    topic_pattern: str
    occurrence: int
    delay_ns: int
    mode: str = "delay"
    contract: str = "request"

    def __post_init__(self) -> None:
        if self.occurrence < 1:
            raise ValueError("Delivery occurrence must be positive")
        if self.delay_ns < 0:
            raise ValueError("Delivery delay must be non-negative")
        if self.mode not in {"delay", "reorder", "timeout"}:
            raise ValueError(f"Unsupported scheduler mode: {self.mode}")
        if self.contract not in {"request", "reply", "independent-notification"}:
            raise ValueError(f"Unsupported delivery contract: {self.contract}")
        if self.mode == "reorder":
            if self.contract != "independent-notification":
                raise ValueError("Reordering requires an independent notification")
            if not self.topic_pattern.startswith("event."):
                raise ValueError("Only event.* notifications may be reordered")
        if self.mode == "timeout":
            if self.contract != "request" or not self.topic_pattern.startswith("query."):
                raise ValueError("Timeout scenarios are restricted to query requests")
            if self.delay_ns <= QUERY_TIMEOUT_NS:
                raise ValueError("Timeout delivery must fall after the query deadline")

    def to_dict(self) -> dict[str, Any]:
        return {
            "topic_pattern": self.topic_pattern,
            "occurrence": self.occurrence,
            "delay_ns": self.delay_ns,
            "mode": self.mode,
            "contract": self.contract,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "DeliveryDecision":
        return cls(
            topic_pattern=str(value["topic_pattern"]),
            occurrence=int(value["occurrence"]),
            delay_ns=int(value["delay_ns"]),
            mode=str(value.get("mode", "delay")),
            contract=str(value.get("contract", "request")),
        )


@dataclass(frozen=True)
class SchedulerScenario:
    decisions: tuple[DeliveryDecision, ...] = ()
    probe_query: str | None = None
    probe_notifications: tuple[str, ...] = ()
    expect_timeout: bool = False
    policy_version: int = SCHEDULER_POLICY_VERSION

    def __post_init__(self) -> None:
        if self.policy_version != SCHEDULER_POLICY_VERSION:
            raise ValueError(f"Unsupported scheduler policy: {self.policy_version}")
        identities = [
            (decision.topic_pattern, decision.occurrence)
            for decision in self.decisions
        ]
        if len(identities) != len(set(identities)):
            raise ValueError("Duplicate scheduler decision occurrence")
        has_timeout = any(decision.mode == "timeout" for decision in self.decisions)
        if self.expect_timeout != has_timeout:
            raise ValueError("expect_timeout must match the recorded timeout decision")
        if self.expect_timeout and self.probe_query is None:
            raise ValueError("Timeout scenarios require a non-mutating query probe")
        if any(not topic.startswith("event.") for topic in self.probe_notifications):
            raise ValueError("Scheduler notification probes must use event.* topics")

    def to_dict(self) -> dict[str, Any]:
        return {
            "policy_version": self.policy_version,
            "decisions": [decision.to_dict() for decision in self.decisions],
            "probe_query": self.probe_query,
            "probe_notifications": list(self.probe_notifications),
            "expect_timeout": self.expect_timeout,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "SchedulerScenario":
        return cls(
            policy_version=int(value.get("policy_version", SCHEDULER_POLICY_VERSION)),
            decisions=tuple(
                DeliveryDecision.from_dict(decision)
                for decision in value.get("decisions", ())
            ),
            probe_query=value.get("probe_query"),
            probe_notifications=tuple(value.get("probe_notifications", ())),
            expect_timeout=bool(value.get("expect_timeout", False)),
        )


class TraceDeliveryPolicy:
    def __init__(self, scenario: SchedulerScenario) -> None:
        self.scenario = scenario
        self._counts: dict[str, int] = {}

    def delay_ns(self, topic: str, event: Any) -> int:
        matching_patterns = {
            decision.topic_pattern
            for decision in self.scenario.decisions
            if _topic_matches(decision.topic_pattern, topic)
        }
        selected: list[DeliveryDecision] = []
        for pattern in sorted(matching_patterns):
            occurrence = self._counts.get(pattern, 0) + 1
            self._counts[pattern] = occurrence
            selected.extend(
                decision
                for decision in self.scenario.decisions
                if decision.topic_pattern == pattern
                and decision.occurrence == occurrence
            )
        if len(selected) > 1:
            raise ValueError(f"Overlapping delivery decisions for topic {topic}")
        return selected[0].delay_ns if selected else 0


@dataclass(frozen=True)
class ScheduledExecutionReport:
    trace_sha256: str
    execution: ExecutionReport
    deliveries: tuple[dict[str, Any], ...]
    requests: tuple[dict[str, Any], ...]
    probe_query: str | None
    probe_result: Any
    expected_timeout: bool
    virtual_time_ns: int

    @property
    def passed(self) -> bool:
        timeout_observed = any(not request["completed"] for request in self.requests)
        probe_contract = not self.expected_timeout or self.probe_result is None
        return (
            self.execution.oracle_passed
            and timeout_observed == self.expected_timeout
            and _causality_valid(self.deliveries)
            and probe_contract
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_sha256": self.trace_sha256,
            "passed": self.passed,
            "execution": self.execution.to_dict(),
            "deliveries": list(self.deliveries),
            "requests": list(self.requests),
            "probe_query": self.probe_query,
            "probe_result": self.probe_result,
            "expected_timeout": self.expected_timeout,
            "virtual_time_ns": self.virtual_time_ns,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), sort_keys=True, separators=(",", ":"))


def with_scheduler(
    trace: TraceTranscript, scenario: SchedulerScenario
) -> TraceTranscript:
    metadata = dict(trace.metadata)
    metadata["scheduler"] = scenario.to_dict()
    master_seed = metadata.get("master_seed", 0)
    subseeds = dict(metadata.get("subseeds", {}))
    subseeds["scheduler"] = f"{SeedStreams(master_seed).seed('scheduler'):032x}"
    metadata["subseeds"] = subseeds
    return replace(trace, metadata=metadata)


def generate_scheduler_scenario(
    master_seed: int,
    mode: str = "delay",
) -> SchedulerScenario:
    """Generate one domain-isolated, fully materialized scheduler scenario."""
    rng = SeedStreams(master_seed).random("scheduler")
    if mode == "delay":
        return SchedulerScenario(
            decisions=(
                DeliveryDecision(
                    "request.command", 1, rng.randint(1, 1_000), "delay", "request"
                ),
                DeliveryDecision(
                    "reply.command.*.*", 1, rng.randint(1, 1_000), "delay", "reply"
                ),
            )
        )
    if mode == "reorder":
        later = rng.randint(1, 1_000)
        return SchedulerScenario(
            decisions=(
                DeliveryDecision(
                    "event.probe.a", 1, later, "reorder", "independent-notification"
                ),
                DeliveryDecision(
                    "event.probe.b", 1, 0, "reorder", "independent-notification"
                ),
            ),
            probe_notifications=("event.probe.a", "event.probe.b"),
        )
    if mode == "timeout":
        return SchedulerScenario(
            decisions=(
                DeliveryDecision(
                    "query.workspace.summary",
                    1,
                    QUERY_TIMEOUT_NS + rng.randint(1, 1_000),
                    "timeout",
                    "request",
                ),
            ),
            probe_query="workspace_summary",
            expect_timeout=True,
        )
    raise ValueError(f"Unsupported generated scheduler mode: {mode}")


def scenario_from_trace(trace: TraceTranscript) -> SchedulerScenario:
    value = trace.metadata.get("scheduler")
    if value is None:
        return SchedulerScenario()
    return SchedulerScenario.from_dict(value)


def _canonical_schedule(bus: ScheduledMessageBus) -> tuple[dict[str, Any], ...]:
    message_labels: dict[str, str] = {}
    for record in bus.delivery_records:
        if record.message_id is not None and record.message_id not in message_labels:
            message_labels[record.message_id] = f"message-{len(message_labels):04d}"
    delivered = sorted(
        (record for record in bus.delivery_records if record.delivered_ns is not None),
        key=lambda record: (record.delivered_ns, record.sequence),
    )
    delivery_order = {record.event_id: index for index, record in enumerate(delivered)}
    return tuple(
        {
            "scheduled_order": record.sequence,
            "delivery_order": delivery_order.get(record.event_id),
            "scheduled_ns": record.scheduled_ns,
            "delivered_ns": record.delivered_ns,
            "topic": _canonical_topic(record.topic),
            "message_type": record.message_type,
            "message": message_labels.get(record.message_id),
            "caused_by": message_labels.get(record.causation_id),
        }
        for record in bus.delivery_records
    )


def _canonical_requests(bus: ScheduledMessageBus) -> tuple[dict[str, Any], ...]:
    labels: dict[str, str] = {}
    for record in bus.delivery_records:
        if record.message_id is not None and record.message_id not in labels:
            labels[record.message_id] = f"message-{len(labels):04d}"
    return tuple(
        {
            "request": labels.get(record.request_message_id),
            "topic": _canonical_topic(record.topic),
            "started_ns": record.started_ns,
            "deadline_ns": record.deadline_ns,
            "completed": record.completed,
            "completed_ns": record.completed_ns,
        }
        for record in bus.request_records
    )


def _canonical_topic(topic: str) -> str:
    if topic.startswith("reply.command."):
        return "reply.command"
    if topic.startswith("reply.query."):
        return "reply.query"
    return topic


def _causality_valid(deliveries: tuple[dict[str, Any], ...]) -> bool:
    delivered_by_message: dict[str, int] = {}
    for delivery in deliveries:
        message = delivery["message"]
        order = delivery["delivery_order"]
        if message is not None and order is not None:
            delivered_by_message.setdefault(message, order)
    for delivery in deliveries:
        parent = delivery["caused_by"]
        child_order = delivery["delivery_order"]
        if parent is None or child_order is None:
            continue
        parent_order = delivered_by_message.get(parent)
        if parent_order is None or parent_order >= child_order:
            return False
    return True


def execute_scheduled_trace(trace: TraceTranscript) -> ScheduledExecutionReport:
    scenario = scenario_from_trace(trace)
    scheduler = VirtualScheduler()
    bus = ScheduledMessageBus(scheduler, TraceDeliveryPolicy(scenario))
    runtime = create_trace_runtime(
        trace,
        bus=bus,
        install_global_bus=False,
    )
    try:
        execute_operations(runtime, trace)
        execution = report_runtime(runtime, trace)
        probe_result = None
        if scenario.probe_query is not None:
            probe_result = runtime.session.query(scenario.probe_query)
        for index, topic in enumerate(scenario.probe_notifications):
            bus.publish(
                topic,
                MessageEnvelope(
                    message_id=f"scheduler-probe-{index:04d}",
                    message_type="event",
                    topic=topic,
                    correlation_id="scheduler-probe",
                    session_id=None,
                    client_type="dst",
                    workspace_id=None,
                    actor_id=None,
                    timestamp=0.0,
                    payload={"probe": index},
                    context=None,
                    status="succeeded",
                ),
            )
        if not scenario.expect_timeout:
            scheduler.run_all()
        return ScheduledExecutionReport(
            trace_sha256=trace.sha256(),
            execution=execution,
            deliveries=_canonical_schedule(bus),
            requests=_canonical_requests(bus),
            probe_query=scenario.probe_query,
            probe_result=probe_result,
            expected_timeout=scenario.expect_timeout,
            virtual_time_ns=scheduler.now_ns,
        )
    finally:
        reset_runtime_state(runtime)
