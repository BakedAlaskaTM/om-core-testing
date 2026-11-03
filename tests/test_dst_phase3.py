from __future__ import annotations

import threading
from dataclasses import replace

import pytest

from lib_command.core.message_bus import MessageBus, MessageEnvelope, get_message_bus
from lib_command.core.scheduled_message_bus import ScheduledMessageBus
from lib_dst.executor import create_trace_runtime, execute_operations
from lib_dst.generator import generate_trace
from lib_dst.runner import run_scheduled_in_worker
from lib_dst.scheduling import (
    DeliveryDecision,
    SchedulerScenario,
    TraceDeliveryPolicy,
    execute_scheduled_trace,
    generate_scheduler_scenario,
    scenario_from_trace,
    with_scheduler,
)
from lib_dst.schemas import TraceTranscript
from lib_dst.validation import validate_trace
from lib_runtime.lifecycle import reset_runtime_state
from lib_runtime.virtual_scheduler import VirtualScheduler


@pytest.fixture(autouse=True)
def clean_runtime_state():
    reset_runtime_state()
    yield
    reset_runtime_state()


def _envelope(
    topic: str,
    message_id: str,
    *,
    message_type: str = "event",
    causation_id: str | None = None,
    reply_to: str | None = None,
) -> MessageEnvelope:
    return MessageEnvelope(
        message_id=message_id,
        message_type=message_type,
        topic=topic,
        correlation_id="correlation",
        session_id="session",
        client_type="test",
        workspace_id=None,
        actor_id=None,
        timestamp=0.0,
        payload={},
        context=None,
        causation_id=causation_id,
        reply_to=reply_to,
    )


def test_virtual_scheduler_uses_time_sequence_and_id_total_order() -> None:
    scheduler = VirtualScheduler()
    observed: list[str] = []

    first = scheduler.call_at(10, lambda: observed.append("first"))
    second = scheduler.call_at(10, lambda: observed.append("second"))
    third = scheduler.call_at(10, lambda: observed.append("third"))
    scheduler.run_all()

    assert (first, second, third) == (
        "event-00000000",
        "event-00000001",
        "event-00000002",
    )
    assert observed == ["first", "second", "third"]
    assert scheduler.now_ns == 10


def test_scheduled_request_pumps_request_and_causal_reply() -> None:
    scenario = SchedulerScenario(
        decisions=(
            DeliveryDecision("request.test", 1, 7, "delay", "request"),
            DeliveryDecision("reply.test", 1, 11, "delay", "reply"),
        )
    )
    scheduler = VirtualScheduler()
    bus = ScheduledMessageBus(scheduler, TraceDeliveryPolicy(scenario))

    def handle(request: MessageEnvelope) -> None:
        bus.publish(
            "reply.test",
            _envelope(
                "reply.test",
                "reply",
                message_type="reply",
                causation_id=request.message_id,
            ),
        )

    bus.subscribe("request.test", handle)
    reply = bus.request(
        "request.test",
        _envelope("request.test", "request", message_type="query"),
        "reply.test",
        timeout=1.0,
    )

    assert reply.message_id == "reply"
    assert scheduler.now_ns == 18
    assert bus.request_records[0].completed is True
    assert [record.delivered_ns for record in bus.delivery_records] == [7, 18]


def test_independent_notifications_can_reorder_by_recorded_delay() -> None:
    scenario = SchedulerScenario(
        decisions=(
            DeliveryDecision(
                "event.probe.a", 1, 10, "reorder", "independent-notification"
            ),
            DeliveryDecision(
                "event.probe.b", 1, 0, "reorder", "independent-notification"
            ),
        )
    )
    scheduler = VirtualScheduler()
    bus = ScheduledMessageBus(scheduler, TraceDeliveryPolicy(scenario))
    observed: list[str] = []
    bus.subscribe("event.*.*", lambda event: observed.append(event.topic))

    bus.publish("event.probe.a", _envelope("event.probe.a", "a"))
    bus.publish("event.probe.b", _envelope("event.probe.b", "b"))
    scheduler.run_all()

    assert observed == ["event.probe.b", "event.probe.a"]


def test_scheduler_policy_round_trips_in_trace_metadata() -> None:
    scenario = SchedulerScenario(
        decisions=(DeliveryDecision("request.command", 1, 25),)
    )
    trace = with_scheduler(generate_trace(4), scenario)

    restored = TraceTranscript.from_json(trace.to_json())

    assert scenario_from_trace(restored) == scenario
    validate_trace(restored)


def test_scheduler_generation_is_key_derived_and_domain_recorded() -> None:
    first = generate_scheduler_scenario(44, "delay")
    trace = with_scheduler(generate_trace(44), first)

    assert generate_scheduler_scenario(44, "delay") == first
    assert generate_scheduler_scenario(45, "delay") != first
    assert "scheduler" in trace.metadata["subseeds"]


def test_scheduled_real_engine_trace_preserves_causality_and_oracle() -> None:
    trace = with_scheduler(
        generate_trace(6),
        SchedulerScenario(
            decisions=(
                DeliveryDecision("request.command", 1, 10, "delay", "request"),
                DeliveryDecision("reply.command.*.*", 1, 20, "delay", "reply"),
            )
        ),
    )

    first = execute_scheduled_trace(trace)
    second = execute_scheduled_trace(trace)

    assert first.passed
    assert first.to_json() == second.to_json()
    assert first.requests[0]["completed_ns"] == 30
    assert all(
        delivery["topic"] not in {"reply.command", "reply.query"}
        or delivery["caused_by"] is not None
        for delivery in first.deliveries
    )
    assert next(
        delivery
        for delivery in first.deliveries
        if delivery["topic"] == "event.dimension.created"
    )["caused_by"] is not None


def test_scheduled_runtime_does_not_publish_to_process_global_bus() -> None:
    global_bus = get_message_bus()
    global_topics: list[str] = []
    global_bus.add_publish_hook(lambda topic, event: global_topics.append(topic))
    trace = with_scheduler(generate_trace(8), SchedulerScenario())
    scheduler = VirtualScheduler()
    scheduled_bus = ScheduledMessageBus(scheduler, TraceDeliveryPolicy(SchedulerScenario()))
    runtime = create_trace_runtime(
        trace,
        bus=scheduled_bus,
        install_global_bus=False,
    )
    try:
        execute_operations(runtime, trace)
        scheduler.run_all()
        assert runtime.session.context.bus is scheduled_bus
        assert get_message_bus() is global_bus
        assert global_topics == []
    finally:
        reset_runtime_state(runtime)


def test_virtual_query_timeout_stops_at_deadline_and_leaves_request_pending() -> None:
    scenario = SchedulerScenario(
        decisions=(
            DeliveryDecision(
                "query.workspace.summary",
                1,
                6_000_000_000,
                "timeout",
                "request",
            ),
        ),
        probe_query="workspace_summary",
        expect_timeout=True,
    )
    report = execute_scheduled_trace(with_scheduler(generate_trace(9), scenario))

    assert report.passed
    assert report.virtual_time_ns == 5_000_000_000
    assert report.requests[-1]["completed"] is False
    assert report.deliveries[-1]["delivered_ns"] is None
    assert report.deliveries[-1]["scheduled_ns"] == 6_000_000_000


def test_scheduled_path_never_constructs_a_threading_event(monkeypatch) -> None:
    def forbidden_event():
        raise AssertionError("threading.Event entered the scheduled request path")

    monkeypatch.setattr(threading, "Event", forbidden_event)
    trace = with_scheduler(generate_trace(10), SchedulerScenario())

    assert execute_scheduled_trace(trace).passed


@pytest.mark.parametrize(
    "decision",
    (
        DeliveryDecision(
            "request.command",
            1,
            17,
            "delay",
            "request",
        ),
        DeliveryDecision(
            "query.workspace.summary",
            1,
            6_000_000_000,
            "timeout",
            "request",
        ),
        DeliveryDecision(
            "event.probe.a",
            1,
            5,
            "reorder",
            "independent-notification",
        ),
    ),
)
def test_scheduled_report_replays_exactly_in_clean_workers(
    decision: DeliveryDecision,
) -> None:
    timeout = decision.mode == "timeout"
    reorder = decision.mode == "reorder"
    scenario = SchedulerScenario(
        decisions=(decision,),
        probe_query="workspace_summary" if timeout else None,
        probe_notifications=("event.probe.a", "event.probe.b") if reorder else (),
        expect_timeout=timeout,
    )
    trace = with_scheduler(generate_trace(12), scenario)

    first = run_scheduled_in_worker(trace, python_hash_seed="3303")
    second = run_scheduled_in_worker(trace, python_hash_seed="3303")

    assert first == second
    assert first["passed"] is True
    if reorder:
        delivered_probes = sorted(
            [
                delivery
                for delivery in first["deliveries"]
                if delivery["topic"].startswith("event.probe.")
            ],
            key=lambda delivery: delivery["delivery_order"],
        )
        assert [delivery["topic"] for delivery in delivered_probes] == [
            "event.probe.b",
            "event.probe.a",
        ]
