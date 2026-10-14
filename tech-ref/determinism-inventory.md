# OM Core Determinism Inventory

This Phase 0 inventory records nondeterministic and process-global behavior relevant to
Deterministic Simulation Testing. It is an implementation map, not a claim that every
site is isolated in Phase 0.

## Clock and Time

- Command envelopes, query replies, profiling, and recording use `time.perf_counter()`
  or `time.time()` throughout `lib_command`.
- Engine profiling and recalculation diagnostics use `time.perf_counter()` in
  `lib_openm/_engine_core.py` and `lib_openm/persistence.py`.
- Session metadata and datastore records use `datetime.now()`.
- Formula functions `NOW` and related compatibility functions read the wall clock in
  `lib_openm/rule_eval/engine.py` and `lib_openm/xls_compat.py`.
- Solver deadlines and result expiry use wall time across `lib_runtime/solver`.

Phase 0 defines `ClockProvider` and carries it in runtime/session context. Migrating the
above call sites is later work; profiling durations and session timestamps are excluded
from Phase 0 canonical observations.

## IDs

- Model IDs are created through `lib_utils.ids.new_id`, backed by UUID4.
- Session, message, correlation, recorder, transport, datastore, and solver IDs contain
  direct UUID4 calls across `lib_command`, `lib_datastore`, `lib_openm`, and
  `lib_runtime`.

Phase 0 defines `IdProvider` but does not route these sites through it. Canonical
observations use logical names and omit generated IDs.

## Randomness and Hash Ordering

- Demo workspace names use module-level `random.choice`.
- `RAND` and `RANDBETWEEN` use module-level random functions in the rule evaluator and
  XLS compatibility layer.
- Solver classification/extraction contains locally seeded NumPy generators.
- Dependency graph edges, reverse edges, and dirty nodes are sets. Their traversal may
  vary with `PYTHONHASHSEED`, which is fixed at interpreter startup.

Phase 0 defines `RandomProvider` and validates ID-independent observations in separate
processes with different hash seeds. Seed derivation and stable dependency ordering are
Phase 1 work.

## Filesystem and Configuration

- Default runtime construction uses JSON workspace persistence and a SQLite snapshot
  store under `OM_SESSIONS_DIR`.
- Configuration is loaded through the `ConfigManager` singleton.
- UDF discovery reads global and workspace directories.
- Logging, recorder export, transport diagnostics, import/export, and solver reports may
  write files.

Phase 0 permits injection of workspace and snapshot adapters. The clean-process probe
uses `InMemoryJsonAdapter` and `InMemorySnapshotAdapter`; it does not virtualize the full
filesystem.

## Threads and Processes

- Engine recalculation can create a `ThreadPoolExecutor` and maintains thread-local
  evaluation state.
- Solver jobs use threads or subprocesses; solver event publication may use a drain
  thread.
- Remote transport and the GUI host create threads, events, locks, sockets, and
  subprocesses.
- The synchronous message bus request path waits on `threading.Event`.

The Phase 0 probe disables the solver and relies on serial recalculation. Virtual
scheduling, remote execution, GUI execution, solver execution, and multithreaded
recalculation remain outside the supported deterministic mode.

## Process-Global State

- Message bus: `lib_command.core.message_bus`.
- Command service: `lib_command.core.bootstrap`.
- Command executor and registry: `lib_command.core.executor` and `registry`.
- Session gateway, manager, store, and view state: `lib_command.core.session_*`.
- Configuration, macro recording, help, profiler registration, and some GUI services
  maintain additional singleton or module-level state.

`lib_runtime.lifecycle.reset_runtime_state()` resets the command/runtime globals required
by the Phase 0 local worker. The command registry remains populated because definitions
are immutable application metadata. Other globals are excluded from the Phase 0 probe
and must be added to lifecycle ownership when their features enter DST scope.
