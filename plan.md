# Deterministic Simulation Testing for OM Core

## 1. Purpose and Scope

This document defines an implementation plan for application-level Deterministic
Simulation Testing (DST) of OM Core. The harness will generate valid workspaces and
command sequences, execute them reproducibly, and verify engine behavior with
differential, metamorphic, replay, and fresh-engine convergence checks.

The first implementation targets the local Python engine in serial mode. OM Core's
local command bus is currently synchronous, while solver jobs, remote-engine socket
traffic, subprocesses, and multithreaded recalculation introduce separate concurrency
models. Those systems are added only after their clock, transport, and scheduling
boundaries are explicitly injectable.

The target is reproducibility of observable behavior, not byte-for-byte identity of
runtime internals. A failure is reproducible when the same environment descriptor and
trace produce the same canonical result, error, and invariant violation.

### Initial scope

- Local Python `Engine` and a newly constructed `Workspace`.
- Serial recalculation and dependency tracking.
- Commands executed through a real `CommandSession` where the command is supported.
- In-memory workspace persistence and fresh JSON round trips.
- Valid OM Core rule expressions and `CellError` values.
- Process-isolated seeds with deterministic trace replay.

### Deferred scope

- Remote engine and socket transport.
- Solver subprocesses and third-party numerical backends.
- GUI event-loop scheduling.
- Multithreaded recalculation.
- Arbitrary filesystem virtualization.

These are not considered deterministic until they receive the same provider and
scheduler boundaries described below.

---

## 2. OM Core Constraints That Shape the Harness

The design must preserve the behavior of the existing runtime rather than model a
different asynchronous application.

1. The in-process `MessageBus` publishes synchronously and invokes subscribers inline.
2. `MessageBus.request()` waits on a real `threading.Event`; delayed virtual delivery
   cannot be introduced without changing request pumping.
3. Several command and engine components obtain the global bus directly.
4. The dependency graph stores edges and dirty nodes in sets.
5. IDs, timestamps, random functions, session metadata, and volatile formula functions
   currently call standard-library providers directly in multiple modules.
6. `PYTHONHASHSEED` is fixed when a Python interpreter starts.
7. Full recalculation can reuse an existing dependency graph; it is not equivalent to
   constructing a fresh engine from serialized workspace state.
8. Runtime composition currently creates JSON and SQLite persistence adapters directly,
   although in-memory workspace adapters already exist.

Consequently, the harness uses worker processes for environment isolation, disables
multithreaded calculation initially, and adds narrow dependency-injection seams before
simulating bus latency or reordering.

---

## 3. Architecture

```text
Master seed + environment descriptor
                  |
                  v
       Key-derived independent seeds
       |          |          |
       v          v          v
   workload   scheduler    faults
       |          |          |
       +----------+----------+
                  v
        Operation trace generator
                  |
                  v
       Deterministic worker process
       - real Workspace and Engine
       - real CommandSession
       - serial calculation
       - in-memory persistence
       - optional virtual scheduler
                  |
                  v
         Canonical observation
                  |
          +-------+--------+
          |       |        |
          v       v        v
       oracle  metamorphic fresh engine
```

The operation trace is the source of truth. Bus traffic is diagnostic evidence, not the
replay format, because message envelopes contain runtime context objects and generated
metadata that are neither stable nor safely serializable.

---

## 4. Deterministic Context and Process Isolation

### 4.1 Seed derivation

A master seed derives independent sub-seeds using a stable keyed hash. Adding a random
draw to one domain must not perturb another domain.

| Domain | Controls |
| --- | --- |
| `environment` | Worker environment, initial logical epoch, hash seed. |
| `schema` | Dimension, item, cube, and rule topology. |
| `values` | Hard values and formula constants. |
| `operations` | Command selection and target selection. |
| `scheduler` | Equal-time ordering, delays, and permitted yields. |
| `faults` | Fault site, occurrence count, and fault outcome. |
| `mutation` | Trace mutation and shrinking choices. |

Use SHA-256 or BLAKE2b over an unambiguous encoding such as
`dst-v1\0<master-seed>\0<domain>`. Record the derivation version in every trace.

### 4.2 Worker model

Each seed runs in a fresh worker process, or in a worker that performs a proven complete
runtime reset between seeds. The initial implementation should prefer one process per
seed batch and periodically recycle workers.

Before process start, set:

- `PYTHONHASHSEED` from the environment sub-seed;
- a locale and timezone recorded in the environment descriptor;
- OM Core debug/configuration flags explicitly;
- calculation mode to serial;
- remote-engine and solver execution off unless a scenario explicitly targets them.

Parallelism means multiple isolated worker processes. It does not mean multiple DST
scenarios sharing the singleton bus, gateway, service registry, or engine in one Python
interpreter.

### 4.3 Injectable providers

Add a small `RuntimeProviders` dependency at the runtime composition root:

```python
@dataclass(frozen=True)
class RuntimeProviders:
    clock: Clock
    ids: IdProvider
    random: RandomProvider
```

Provider responsibilities:

- `Clock`: wall time, monotonic time, and sleep/deadline behavior.
- `IdProvider`: workspace/model IDs, session IDs, message IDs, and correlation IDs.
- `RandomProvider`: volatile rule functions and any engine-generated random data.

Do not monkeypatch individual modules as the long-term architecture. A short Phase 0
spike may monkeypatch to inventory call sites, but replay correctness must rely on
explicit providers.

### 4.4 Stable iteration

Sort at nondeterministic boundaries, particularly:

- dependency graph `dirty_keys()`, `precedents_of()`, and `dependents_of()`;
- ready/frontier queues with a documented tie-break key;
- generated symbol tables and trace serialization;
- canonical workspace observations.

Sorting only the workload generator is insufficient because graph sets influence
recalculation order. The stable order must not change rule precedence semantics.

---

## 5. Runtime and Scheduler Integration

### 5.1 Stage A: deterministic synchronous execution

The first useful harness keeps the existing bus synchronous. It generates an operation,
executes it to completion through the real session, records the result, and then advances
to the next operation. This covers schema mutation, values, rules, incremental
recalculation, save/load, and error contracts without pretending that the local bus is
asynchronous.

### 5.2 Stage B: injectable bus

Refactor runtime construction so it can accept:

- `MessageBus`;
- workspace and snapshot persistence adapters;
- `RuntimeProviders`;
- optional services such as timeline and solver.

Remove direct `get_message_bus()` calls from execution paths used by DST by threading the
injected bus through the executor, publishers, gateway, query service, and command
service. Production defaults continue to use the existing singleton.

### 5.3 Stage C: scheduler-aware request/reply

Introduce a scheduler contract rather than placing a delayed queue behind the existing
blocking `request()` method:

```python
class Scheduler(Protocol):
    def call_at(self, when_ns: int, callback: Callable[[], None]) -> EventId: ...
    def run_next(self) -> bool: ...
    def run_until(self, predicate: Callable[[], bool], deadline_ns: int) -> bool: ...
```

The simulated request path must pump scheduled events until its correlated reply arrives
or the virtual deadline expires. It must never wait on a real `threading.Event`. Queue
items use `(logical_time_ns, deterministic_sequence, event_id)` as their total ordering.
Use integer nanoseconds rather than floating-point timestamps.

Only after this seam exists may scheduler scenarios delay or reorder messages. Reordering
must respect explicitly declared causal constraints, such as a reply occurring after its
request handler.

---

## 6. Persistence Isolation

Use `InMemoryJsonAdapter` for the normal harness. It exercises OM Core's production JSON
serialization shape while avoiding workspace files. Add in-memory snapshot/timeline
adapters where timeline commands are in scope.

Runtime factory functions must accept adapters instead of always creating
`JsonFileAdapter` and `SQLiteSnapshotStoreAdapter`. Configuration, global UDF discovery,
logs, and subprocess artifacts are disabled or redirected to a per-worker temporary
directory.

A general virtual filesystem is not required for the initial harness. Prefer explicit
ports for workspace persistence, snapshots, configuration, and UDF discovery. This
makes fault sites precise and keeps production I/O behavior testable.

Storage faults are injected by a decorator around an adapter, for example:

- fail before write;
- fail after serialization but before commit;
- return a truncated or invalid payload on load;
- fail on the Nth operation.

Every storage fault must declare its expected contract: atomic preservation, a structured
command error, or successful recovery. Do not assert convergence after a fault whose
documented semantics permit lost state.

---

## 7. Workload Generation

### 7.1 Symbol table

Maintain a typed symbol table updated only after successful operations:

```text
Workspace
|- Dimensions
|  |- Region: US, EU, APAC
|  `- Year: 2025, 2026
|- Cubes
|  |- Sales(Region, Year)
|  `- Expenses(Region, Year)
`- Rules and address masks
```

Store stable logical handles in traces rather than generated runtime UUIDs. During
execution, resolve handles such as `cube:sales` or `item:region/us` to the IDs created by
that worker's `IdProvider`.

### 7.2 OM Core expression grammar

Generate OM Core AST nodes, then serialize them using OM Core syntax. Examples:

- same-cube reference: `[Region.US, Year.2026]`;
- cube-qualified reference: `Sales::[Region.US, Year.2026]`;
- dimension item reference: `Region.US`;
- sequential accessor: `Year[PREV]`;
- functions: `SUM(...)`, `AVG(...)`, `IF(...)`, `ROUND(...)`.

Do not generate spreadsheet syntax such as `Sales[Region='US']`.

Use the production tokenizer/parser as a validation gate before applying a generated
rule. Parser acceptance establishes syntax validity; the separate reference interpreter
still establishes semantic expectations for its supported subset.

### 7.3 Typed, depth-bounded generation

Each AST node carries an output type and domain restrictions. Initially support:

- finite numeric literals;
- numeric cell references;
- `+`, `-`, `*`, and guarded `/`;
- numeric comparisons;
- `IF` with compatible branches;
- `SUM`, `AVG`, and `ROUND` for cases the oracle models exactly.

At depth 0-1, prefer references and binary expressions. At deeper levels, increase the
probability of literals and references and enforce a hard maximum node count. Record the
AST in the trace, not only its rendered string.

Expand later to strings, ranges, sequential accessors, hierarchy navigation, volatile
functions, UDFs, and error-producing expressions. Each expansion requires matching
oracle or metamorphic semantics.

### 7.4 Scenario modes

1. `acyclic_numeric`: references only previously available cells/cubes.
2. `incremental_graph`: edit precedents after dependency edges have been established.
3. `cycle_contract`: deliberately create self and multi-cell cycles and expect `#CIRC!`.
4. `error_contract`: missing references, division by zero, syntax rejection, and shape
   errors with the appropriate `CellError` or command failure.
5. `persistence_roundtrip`: serialize, deserialize, reconstruct, and compare.
6. `scheduler_contract`: enabled only after the scheduler-aware bus exists.
7. `fault_contract`: applies only named, documented transport or storage faults.

---

## 8. Trace Format and Replay

Use a versioned JSON schema:

```json
{
  "schema_version": 1,
  "seed_derivation_version": "dst-v1",
  "master_seed": "0x8f3a219b",
  "environment": {
    "python": "3.12",
    "python_hash_seed": 12345,
    "timezone": "UTC",
    "engine_mode": "python",
    "recalculation": "serial"
  },
  "operations": [
    {
      "step": 0,
      "kind": "create_dimension",
      "target": "dimension:region",
      "args": {"name": "Region"}
    }
  ]
}
```

An operation records logical targets, normalized arguments, expected fault decisions,
and optional virtual timing. Do not serialize live execution contexts, callbacks, bus
subscriber identities, or arbitrary Python objects.

Replay modes:

- `seed`: regenerate and execute, then optionally verify the generated trace hash;
- `trace`: execute the recorded operations without consulting workload PRNGs;
- `prefix`: execute through step N for debugging and shrinking.

Record the repository revision, trace schema version, Python version, relevant dependency
versions, and OM Core configuration. A replay tool must reject or clearly warn about an
incompatible environment rather than silently claiming determinism.

---

## 9. Canonical Observations and Invariants

### 9.1 Canonical observation

Compare user-visible semantic state, not raw object graphs. The canonical snapshot
contains:

- dimensions, items, cubes, and rules keyed by logical name/handle;
- ordered dimension membership and rule precedence where order is semantic;
- hard values and calculated values for the scenario's observation set;
- `CellError.code` for error cells;
- command outcome and normalized error category;
- dependency relationships only in graph-specific invariants, sorted canonically.

Exclude generated IDs, message/correlation IDs, timestamps, logs, caches, profiling
durations, session access times, and incidental dictionary order.

### 9.2 Differential oracle

The reference interpreter must be independent of OM Core's evaluator. It implements only
the declared typed subset and models OM Core error propagation deliberately. Never use
the same evaluator for the system under test and the oracle.

Numeric comparison uses:

```python
math.isclose(actual, expected, rel_tol=1e-9, abs_tol=1e-12)
```

Tune tolerances per operation when aggregation order or external numeric libraries make
that necessary. Compare integers and error codes exactly. Handle signed zero, NaN, and
infinity explicitly rather than allowing them through ordinary tolerance checks.

### 9.3 Metamorphic invariants

Apply transformations only when their preconditions hold:

- finite numeric `E`: `E + 0 == E` and `E * 1 == E`;
- finite, nonzero numeric `E`: `E / E == 1` within tolerance;
- compatible pure branches: `IF(True, E1, E2) == E1`;
- commutative finite operands: `A + B == B + A` within tolerance;
- substitution: replace pure scalar subexpression `E` with its oracle value.

Exclude volatile functions, errors, strings, side effects, unstable overflow cases, and
context-dependent range expressions unless a transformation explicitly supports them.

### 9.4 Incremental versus fresh-engine convergence

1. Execute the operation trace incrementally in Workspace A.
2. Serialize Workspace A through `InMemoryJsonAdapter`.
3. Deserialize into a new Workspace B.
4. Construct a new Engine B with empty caches and a new dependency graph.
5. Run full bootstrap/recalculation in serial mode.
6. Compare canonical observations of A and B.

This is the cold convergence invariant. Calling `recalculate_all()` twice on Engine A is
a separate cache/idempotence invariant, not a cold-engine test.

### 9.5 Error contracts

Expected engine values include:

| Condition | Expected result |
| --- | --- |
| Division by zero | `CellError("#DIV/0!")` |
| Missing or invalid reference | `CellError("#REF!")` or validation rejection, depending on operation boundary |
| Circular cell dependency | `CellError("#CIRC!")` |
| Invalid expression syntax | `CellError("#SYNTAX!")` or command validation failure |
| Numeric domain/overflow | `#NUM!` or `#RANGE!` according to the function contract |

The invariant is both the correct structured result and absence of an unhandled process
failure. Do not expect a `CircularDependencyError`; OM Core exposes circular calculation
errors as `#CIRC!` at the cell boundary.

---

## 10. Fault Model

Fault injection is contract-based. A fault point must name the layer, operation, delivery
guarantee, expected recovery behavior, and whether state loss is allowed.

### Supported initial faults

- persistence adapter failure on the Nth save/load;
- malformed persisted JSON on load;
- command handler raises a declared domain exception;
- virtual request timeout after scheduler integration.

### Later transport faults

- delayed event notification;
- dropped non-mutating observation event;
- duplicate delivery of an explicitly idempotent message;
- reordered independent events.

Do not initially duplicate or drop arbitrary mutation commands. OM Core currently has no
general idempotency key or retry contract, so repeated mutations or lost requests may be
valid outcomes rather than engine defects. Add command-level idempotency semantics before
asserting exactly-once behavior.

Every fault decision is stored in the trace. Replay consumes the stored decision instead
of drawing again from the fault PRNG.

---

## 11. Mutation, Shrinking, and Coverage

### 11.1 Trace mutation

Mutators operate on structured operations and ASTs while maintaining symbol-table and
type validity:

- replace a numeric literal with boundary values;
- replace an operator with a type-compatible operator;
- replace a reference with another compatible existing symbol;
- insert/delete a command when its dependencies remain satisfied;
- shift virtual timing after scheduler integration;
- change one recorded fault decision.

Validate the mutated trace before execution.

### 11.2 Shrinking

Shrink in this order:

1. remove operation ranges using delta debugging;
2. remove independent schema objects;
3. reduce dimension sizes and observation sets;
4. simplify AST nodes to children, references, or literals;
5. reduce numeric values toward `0`, `1`, and nearby boundaries;
6. remove scheduler perturbations and faults.

A candidate is retained only if it reproduces the same normalized failure signature.
The shrinker replays from a clean worker state for every candidate.

### 11.3 Coverage guidance

Coverage guidance is a later optimization, not a prerequisite for deterministic replay.
Use a maintained coverage mechanism capable of arcs/branches, or Python monitoring where
it provides equivalent data. Plain `sys.settrace` line hits are not branch-edge coverage.

Normalize coverage to stable `(module, source-location or arc)` identities and exclude
the harness, generated code, GUI, and third-party dependencies. Corpus writes occur in a
single coordinator process using content-addressed trace files and atomic replacement;
workers never concurrently mutate a shared index.

Retain a trace when it adds normalized coverage, exercises a new semantic feature, or
produces a new invariant failure signature.

---

## 12. Phased Roadmap and Exit Criteria

Timings are estimates, not commitments. Each phase has an independently useful exit
criterion.

### Phase 0: determinism inventory and composition seams

- Inventory clock, UUID, random, filesystem, thread, process, and global singleton use.
- Add runtime parameters for bus, providers, workspace persistence, snapshot persistence,
  and optional services.
- Add a reliable runtime reset/teardown path.
- Define canonical observation and environment descriptor schemas.

Exit: two clean worker processes can execute the same hand-authored real-engine trace and
produce identical canonical observations.

### Phase 1: real-engine deterministic core

- Implement key-derived PRNGs and worker-process isolation.
- Build typed symbol table and OM Core grammar generator.
- Execute real operations through `CommandSession` where available.
- Force serial recalculation and stable dependency ordering.
- Implement independent numeric oracle and error contracts.
- Implement trace serialization and exact replay.

Exit: at least 10,000 supported-subset seeds replay deterministically across repeated
runs on the same supported environment.

### Phase 2: persistence and convergence

- Inject `InMemoryJsonAdapter` and in-memory snapshot storage.
- Implement fresh-engine convergence and cache/idempotence invariants.
- Add persistence fault decorators.
- Implement failure signatures and structured shrinking.

Exit: every induced failure produces a standalone minimized trace that reproduces from a
clean process.

### Phase 3: virtual scheduling

- Remove direct global-bus access from the DST execution path.
- Implement scheduler-aware request/reply with virtual deadlines.
- Record causal event relationships and deterministic equal-time ordering.
- Add only contract-valid delay, timeout, and reorder scenarios.

Exit: scheduler scenarios complete without wall-clock sleeps or `threading.Event` waits,
and replay preserves the event order exactly.

### Phase 4: coverage-guided evolution

- Add normalized branch/arc coverage.
- Build coordinator-owned, content-addressed corpus storage.
- Add type-preserving trace and AST mutation.
- Weight corpus selection by rarity, age, size, and semantic feature coverage.

Exit: corpus evolution discovers and minimizes seeded defects and remains deterministic
when mutation decisions are replayed.

### Phase 5: CI and scale

- Add `om-dst run`, `om-dst replay`, and `om-dst shrink` commands.
- Run a small fixed regression corpus on pull requests.
- Run bounded seed batches on scheduled CI workers.
- Publish failing traces, environment descriptors, and canonical diffs as artifacts.
- Add remote engine, solver, GUI, or multithreaded modes one at a time only after each has
  deterministic provider/scheduler boundaries.

Exit: CI runtime and seed throughput are measured empirically. Seed-count targets such as
one million runs are adopted only when observed throughput and infrastructure cost make
them credible.

---

## 13. Minimal Real-Engine Harness Shape

The first prototype must use production objects. This outline is intentionally focused on
the lifecycle rather than duplicating OM Core command DTO details:

```python
def run_trace(trace: TraceTranscript) -> Observation:
    providers = providers_from_trace(trace)
    workspace_adapter = InMemoryJsonAdapter()
    snapshot_adapter = InMemorySnapshotAdapter()

    runtime = create_runtime(
        workspace=Workspace.create("DST"),
        bus=MessageBus(),
        providers=providers,
        workspace_adapter=workspace_adapter,
        snapshot_adapter=snapshot_adapter,
        enable_solver=False,
        enable_multithread_recompute=False,
    )

    symbols = SymbolTable()
    try:
        for operation in trace.operations:
            result = execute_operation(runtime.session, symbols, operation)
            symbols.commit(operation, result)
            verify_step_contract(runtime, operation, result)

        runtime.engine.recalculate_all()
        return canonical_observation(runtime, symbols)
    finally:
        runtime.close()
```

Some parameters and helpers above are implementation work introduced by this plan; they
do not yet exist. That is deliberate and makes the required OM Core integration seams
explicit. A mock engine may test the harness itself, but it cannot serve as evidence that
OM Core satisfies a differential invariant.

---

## 14. Definition of Done

DST is considered established for a supported execution mode when:

1. A trace executes against the real OM Core engine.
2. Repeated execution from clean supported environments yields the same canonical result.
3. Every PRNG decision belongs to a recorded seed domain or is materialized in the trace.
4. Time, IDs, persistence, and scheduling use injected providers in the tested path.
5. The oracle is independent and limited to explicitly supported semantics.
6. Cold convergence uses a newly deserialized workspace and newly constructed engine.
7. Faults correspond to documented delivery or recovery contracts.
8. Failures can be replayed and minimized without relying on the original process state.
9. Parallel workers share no runtime singletons and coordinate corpus writes safely.
10. Unsupported modes are reported explicitly rather than being presented as deterministic.
