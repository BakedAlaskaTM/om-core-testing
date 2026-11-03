"""Recorded, type-preserving mutations for deterministic corpus evolution."""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any, Iterable

from lib_dst.ast import from_dict, render
from lib_dst.faults import FaultDecision
from lib_dst.oracle import canonical, evaluate
from lib_dst.schemas import TraceOperation, TraceTranscript
from lib_dst.scheduling import QUERY_TIMEOUT_NS, SchedulerScenario
from lib_dst.seeding import SeedStreams
from lib_dst.validation import validate_trace
from lib_openm.rule_eval import _Parser, _tokenise


MUTATION_VERSION = 1
_KINDS = {
    "hardvalue",
    "ast_literal",
    "ast_operator",
    "ast_reference",
    "recalculation",
    "scheduler_delay",
    "fault",
}


@dataclass(frozen=True)
class MutationDecision:
    parent_sha256: str
    mutation_seed: str
    mutation_index: int
    kind: str
    target: dict[str, Any]
    before: Any
    after: Any
    version: int = MUTATION_VERSION

    def __post_init__(self) -> None:
        if self.version != MUTATION_VERSION:
            raise ValueError(f"Unsupported mutation version: {self.version}")
        if len(self.parent_sha256) != 64:
            raise ValueError("Mutation parent must be a SHA-256 digest")
        int(self.parent_sha256, 16)
        int(self.mutation_seed, 16)
        if self.mutation_index < 0:
            raise ValueError("Mutation index cannot be negative")
        if self.kind not in _KINDS:
            raise ValueError(f"Unsupported mutation kind: {self.kind}")

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": self.version,
            "parent_sha256": self.parent_sha256,
            "mutation_seed": self.mutation_seed,
            "mutation_index": self.mutation_index,
            "kind": self.kind,
            "target": self.target,
            "before": self.before,
            "after": self.after,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> "MutationDecision":
        return cls(
            version=int(value.get("version", MUTATION_VERSION)),
            parent_sha256=str(value["parent_sha256"]),
            mutation_seed=str(value["mutation_seed"]),
            mutation_index=int(value["mutation_index"]),
            kind=str(value["kind"]),
            target=dict(value["target"]),
            before=value.get("before"),
            after=value.get("after"),
        )


def mutation_decisions_from_trace(
    trace: TraceTranscript,
) -> tuple[MutationDecision, ...]:
    decisions = tuple(
        MutationDecision.from_dict(value)
        for value in trace.metadata.get("mutations", ())
    )
    indices = [decision.mutation_index for decision in decisions]
    if len(indices) != len(set(indices)):
        raise ValueError("Duplicate mutation index")
    return decisions


def available_mutation_kinds(trace: TraceTranscript) -> tuple[str, ...]:
    ast_value = trace.metadata.get("ast", {})
    nodes = tuple(_walk_ast(ast_value)) if isinstance(ast_value, dict) else ()
    kinds: list[str] = []
    if any(operation.kind == "set_cell_hardvalue" for operation in trace.operations):
        kinds.append("hardvalue")
    if any(node.get("type") == "number" for _, node in nodes):
        kinds.append("ast_literal")
    if any(node.get("type") == "binary" for _, node in nodes):
        kinds.append("ast_operator")
    references = [node for _, node in nodes if node.get("type") == "reference"]
    hardvalue_handles = {
        str(handle)
        for operation in trace.operations
        if operation.kind == "set_cell_hardvalue"
        for handle in operation.args.get("address", ())
    }
    if references and any(
        hardvalue_handles - {str(node.get("handle"))} for node in references
    ):
        kinds.append("ast_reference")
    kinds.append("recalculation")
    scheduler = trace.metadata.get("scheduler")
    if isinstance(scheduler, dict) and scheduler.get("decisions"):
        kinds.append("scheduler_delay")
    if trace.metadata.get("faults"):
        kinds.append("fault")
    return tuple(sorted(kinds))


def mutate_trace(
    trace: TraceTranscript,
    master_seed: int | str,
    mutation_index: int,
    *,
    kind: str | None = None,
) -> tuple[TraceTranscript, MutationDecision]:
    """Create and record one deterministic mutation."""

    validate_trace(trace)
    streams = SeedStreams(master_seed)
    domain = f"mutation.{mutation_index}"
    seed = streams.seed(domain)
    rng = streams.random(domain)
    available = available_mutation_kinds(trace)
    selected = kind or rng.choice(available)
    if selected not in available:
        raise ValueError(f"Mutation kind {selected!r} is unavailable for this trace")
    decision = _draw_decision(trace, selected, mutation_index, seed, rng)
    return apply_mutation_decision(trace, decision), decision


def apply_mutation_decision(
    trace: TraceTranscript, decision: MutationDecision
) -> TraceTranscript:
    """Apply a materialized mutation without consulting a PRNG."""

    if trace.sha256() != decision.parent_sha256:
        raise ValueError("Mutation decision parent does not match the supplied trace")

    operations = list(trace.operations)
    metadata = copy.deepcopy(trace.metadata)
    ast_changed = False

    if decision.kind == "hardvalue":
        step = int(decision.target["step"])
        operation = operations[step]
        args = dict(operation.args)
        if args.get("value") != decision.before:
            raise ValueError("Hard-value mutation precondition changed")
        args["value"] = decision.after
        operations[step] = replace(operation, args=args)
    elif decision.kind in {"ast_literal", "ast_operator", "ast_reference"}:
        ast_value = copy.deepcopy(metadata["ast"])
        node = _node_at(ast_value, tuple(decision.target["path"]))
        field = str(decision.target["field"])
        if field == "__node__":
            if node != decision.before:
                raise ValueError("AST mutation precondition changed")
            node.clear()
            node.update(copy.deepcopy(decision.after))
        else:
            if node.get(field) != decision.before:
                raise ValueError("AST mutation precondition changed")
            node[field] = copy.deepcopy(decision.after)
        expression = render(from_dict(ast_value))
        _Parser(_tokenise(expression)).parse()
        metadata["ast"] = ast_value
        for index, operation in enumerate(operations):
            if operation.kind == "set_rule":
                args = dict(operation.args)
                args["expression"] = expression
                operations[index] = replace(operation, args=args)
        ast_changed = True
    elif decision.kind == "recalculation":
        action = str(decision.target["action"])
        if action == "delete":
            step = int(decision.target["step"])
            if operations[step].kind != "run_recalculation":
                raise ValueError("Recalculation deletion precondition changed")
            del operations[step]
        elif action == "insert":
            operations.append(
                TraceOperation(len(operations), "run_recalculation", "runtime", {"scope": "all"})
            )
        else:
            raise ValueError(f"Unsupported recalculation action: {action}")
        operations = [replace(operation, step=index) for index, operation in enumerate(operations)]
    elif decision.kind == "scheduler_delay":
        scheduler = copy.deepcopy(metadata["scheduler"])
        index = int(decision.target["decision_index"])
        if scheduler["decisions"][index]["delay_ns"] != decision.before:
            raise ValueError("Scheduler mutation precondition changed")
        scheduler["decisions"][index]["delay_ns"] = int(decision.after)
        SchedulerScenario.from_dict(scheduler)
        metadata["scheduler"] = scheduler
    elif decision.kind == "fault":
        faults = copy.deepcopy(metadata["faults"])
        index = int(decision.target["decision_index"])
        field = str(decision.target["field"])
        if faults[index][field] != decision.before:
            raise ValueError("Fault mutation precondition changed")
        faults[index][field] = decision.after
        FaultDecision.from_dict(faults[index])
        metadata["faults"] = faults
    else:  # pragma: no cover - guarded by MutationDecision
        raise ValueError(f"Unsupported mutation kind: {decision.kind}")

    candidate = replace(trace, operations=tuple(operations), metadata=metadata)
    if decision.kind == "hardvalue" or ast_changed:
        candidate = _refresh_oracle(candidate)

    metadata = copy.deepcopy(candidate.metadata)
    recorded = list(metadata.get("mutations", ()))
    recorded.append(decision.to_dict())
    metadata["mutations"] = recorded
    features = set(str(value) for value in metadata.get("features", ()))
    features.add(f"mutation:{decision.kind}")
    metadata["features"] = sorted(features)
    subseeds = dict(metadata.get("subseeds", {}))
    subseeds[f"mutation.{decision.mutation_index}"] = decision.mutation_seed
    metadata["subseeds"] = subseeds
    candidate = replace(candidate, metadata=metadata)
    validate_trace(candidate)
    return candidate


def _draw_decision(trace, kind, mutation_index, seed, rng) -> MutationDecision:
    target: dict[str, Any]
    before: Any
    after: Any
    if kind == "hardvalue":
        candidates = [operation for operation in trace.operations if operation.kind == "set_cell_hardvalue"]
        operation = rng.choice(candidates)
        target = {"step": operation.step}
        before = operation.args["value"]
        after = _different(rng, before, (-1, 0, 1, int(before) - 1, int(before) + 1))
    elif kind in {"ast_literal", "ast_operator", "ast_reference"}:
        ast_value = trace.metadata["ast"]
        node_type = {
            "ast_literal": "number",
            "ast_operator": "binary",
            "ast_reference": "reference",
        }[kind]
        candidates = [(path, node) for path, node in _walk_ast(ast_value) if node.get("type") == node_type]
        path, node = rng.choice(candidates)
        if kind == "ast_literal":
            field = "value"
            before = node[field]
            after = _different(rng, before, (-1, 0, 1, int(before) - 1, int(before) + 1))
        elif kind == "ast_operator":
            field = "operator"
            before = node[field]
            after = rng.choice(sorted({"+", "-", "*", "/"} - {str(before)}))
        else:
            field = "__node__"
            before = copy.deepcopy(node)
            hardvalue_handles = sorted(
                {
                    str(handle)
                    for operation in trace.operations
                    if operation.kind == "set_cell_hardvalue"
                    for handle in operation.args.get("address", ())
                    if str(handle) != str(node.get("handle"))
                }
            )
            after = _reference_node(trace, rng.choice(hardvalue_handles))
        target = {"path": list(path), "field": field}
    elif kind == "recalculation":
        recalculations = [operation for operation in trace.operations if operation.kind == "run_recalculation"]
        if recalculations and rng.randrange(2) == 0:
            operation = rng.choice(recalculations)
            target = {"action": "delete", "step": operation.step}
            before, after = operation.to_dict(), None
        else:
            target = {"action": "insert", "step": len(trace.operations)}
            before, after = None, {"kind": "run_recalculation", "scope": "all"}
    elif kind == "scheduler_delay":
        scheduler = trace.metadata["scheduler"]
        index = rng.randrange(len(scheduler["decisions"]))
        item = scheduler["decisions"][index]
        before = int(item["delay_ns"])
        if item["mode"] == "timeout":
            after = QUERY_TIMEOUT_NS + rng.randint(1, 10_000)
        else:
            after = _different(rng, before, (0, 1, 10, 100, 1_000, 10_000))
        target = {"decision_index": index}
    elif kind == "fault":
        faults = trace.metadata["faults"]
        index = rng.randrange(len(faults))
        item = faults[index]
        if item["operation"] == "load":
            target = {"decision_index": index, "field": "outcome"}
            before = item["outcome"]
            after = "malformed_json" if before == "raise" else "raise"
        else:
            target = {"decision_index": index, "field": "occurrence"}
            before = int(item["occurrence"])
            after = 2 if before == 1 else 1
    else:  # pragma: no cover - selected from available kinds
        raise ValueError(kind)
    return MutationDecision(
        parent_sha256=trace.sha256(),
        mutation_seed=f"{seed:032x}",
        mutation_index=mutation_index,
        kind=kind,
        target=target,
        before=before,
        after=after,
    )


def _different(rng, before: Any, values: Iterable[Any]) -> Any:
    candidates = sorted(set(values) - {before}, key=lambda value: (type(value).__name__, repr(value)))
    if not candidates:
        raise ValueError("Mutation has no alternative value")
    return rng.choice(candidates)


def _walk_ast(value: dict[str, Any], path: tuple[Any, ...] = ()):
    yield path, value
    node_type = value.get("type")
    if node_type in {"binary", "comparison"}:
        yield from _walk_ast(value["left"], path + ("left",))
        yield from _walk_ast(value["right"], path + ("right",))
    elif node_type == "negate":
        yield from _walk_ast(value["operand"], path + ("operand",))
    elif node_type == "call":
        for index, argument in enumerate(value["arguments"]):
            yield from _walk_ast(argument, path + ("arguments", index))


def _node_at(value: dict[str, Any], path: tuple[Any, ...]) -> dict[str, Any]:
    current: Any = value
    for part in path:
        current = current[part]
    if not isinstance(current, dict):
        raise ValueError("Mutation path does not identify an AST node")
    return current


def _refresh_oracle(trace: TraceTranscript) -> TraceTranscript:
    expression = from_dict(trace.metadata["ast"])
    values: dict[str, Any] = {}
    for operation in trace.operations:
        if operation.kind == "set_cell_hardvalue":
            address = operation.args.get("address", ())
            if len(address) == 1:
                values[str(address[0])] = operation.args["value"]
    metadata = copy.deepcopy(trace.metadata)
    oracle = dict(metadata["oracle"])
    oracle["expected"] = canonical(evaluate(expression, values))
    metadata["oracle"] = oracle
    return replace(trace, metadata=metadata)


def _reference_node(trace: TraceTranscript, handle: str) -> dict[str, Any]:
    symbols = trace.metadata["symbols"]
    item = symbols[handle]
    dimension = symbols[item["owner"]]
    cube = next(
        operation.args["name"]
        for operation in trace.operations
        if operation.kind == "create_cube"
    )
    return {
        "type": "reference",
        "handle": handle,
        "cube": cube,
        "dimension": dimension["name"],
        "item": item["name"],
    }
