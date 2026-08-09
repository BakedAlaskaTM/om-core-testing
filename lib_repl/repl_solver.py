"""REPL adapter for solver subcommands.

Translates REPL syntax into canonical commands via ClientSession.
No solver execution and no runtime imports — thin adapter only.

REPL syntax (interactive mode):
    solver new                       - Reset draft problem spec
    solver algorithm <id>            - Set algorithm (auto, cobyla, slsqp, etc.)
    solver variable add <ref> [lb] [ub]  - Add decision variable
        lb/ub can be floats OR cell refs (e.g. C:Dim.lb)
    solver variable remove <idx>     - Remove variable by index
    solver variable list             - List variables
    solver objective add <ref> <min|max|ref>  - Add objective
        direction can be literal min/max OR a cell ref
    solver objective remove <idx>    - Remove objective by index
    solver objective list            - List objectives
    solver constraint add <ref> <type> <bound> [bound2]  - Add constraint (classic)
    solver constraint add <ref> <min_ref|num|null> <max_ref|num|null>  - Add constraint (cell-ref)
    solver constraint remove <idx>   - Remove constraint by index
    solver constraint list           - List constraints
    solver option set <key> <val>    - Set solver option
    solver option list               - List solver options
    solver option clear              - Clear all options
    solver limit set <key> <val|ref> - Set limit (val can be float OR cell ref)
    solver limit list                - List limits
    solver show                      - Show full draft spec
    solver run                       - Run draft spec
    solver run <json_problem_spec>   - Run inline JSON (advanced)
    solver status <job_id>           - Poll job status
    solver result <job_id>           - Get job result
    solver cancel <job_id>           - Cancel running job
    solver apply <job_id> [index]    - Apply result to workspace
    solver dump [job_id] [--cube <name>] - Dump result into 3D cube (default RESULT)
    solver backends                  - List backends
    solver algorithms [backend_id]   - List algorithms
    solver export <job_id> [path]     - Export diagnostic report

Cell reference format: <cube_name>:<dim_name>.<item_name>
Example: C:Input.x1
"""

from __future__ import annotations

import json
import shlex
from typing import Any


class REPLSolverMixin:
    """Mixin for OpenMREPL providing solver subcommands.

    Expects the host class to have:
    - ``self.session``: a ClientSession with ``execute()`` and ``query()``.
    - ``self.println(msg)``: output method.
    """

    def do_solver(self, args: str) -> Any:
        """Handle ``solver`` subcommands."""
        return self.cmd_solver(args)

    def cmd_solver(self, args: str) -> Any:
        """Handle ``solver`` subcommands."""
        parts = shlex.split(args) if args else []
        if not parts:
            self._solver_print_usage()
            return None

        subcommand = parts[0]
        rest = parts[1:]

        if subcommand == "run":
            return self._solver_run(rest)
        elif subcommand == "status":
            return self._solver_status(rest)
        elif subcommand == "result":
            return self._solver_result(rest)
        elif subcommand == "cancel":
            return self._solver_cancel(rest)
        elif subcommand == "apply":
            return self._solver_apply(rest)
        elif subcommand == "backends":
            return self._solver_backends()
        elif subcommand == "algorithms":
            return self._solver_algorithms(rest)
        elif subcommand == "constraint":
            return self._solver_constraint(rest)
        elif subcommand == "option":
            return self._solver_option(rest)
        elif subcommand == "dump":
            return self._solver_dump(rest)
        elif subcommand == "export":
            return self._solver_export(rest)
        elif subcommand == "new":
            return self._solver_new(rest)
        elif subcommand == "algorithm":
            return self._solver_algorithm(rest)
        elif subcommand == "variable":
            return self._solver_variable(rest)
        elif subcommand == "objective":
            return self._solver_objective(rest)
        elif subcommand == "limit":
            return self._solver_limit(rest)
        elif subcommand == "show":
            return self._solver_show(rest)
        else:
            self.println(f"Unknown solver subcommand: {subcommand}")
            self._solver_print_usage()
            return None

    def _solver_print_usage(self) -> None:
        self.println("Usage: solver <subcommand> [args]")
        self.println("")
        self.println("  Problem setup (interactive):")
        self.println("    solver new                        - Reset draft problem spec")
        self.println("    solver algorithm <id>             - Set algorithm")
        self.println("    solver variable add <ref> [lb] [ub]  - Add decision variable")
        self.println("    solver variable remove <idx>      - Remove variable")
        self.println("    solver variable list             - List variables")
        self.println("    solver objective add <ref> <min|max>  - Add objective")
        self.println("    solver objective remove <idx>     - Remove objective")
        self.println("    solver objective list             - List objectives")
        self.println("    solver constraint add <ref> <type> <bound>  - Add constraint")
        self.println("    solver constraint remove <idx>    - Remove constraint")
        self.println("    solver constraint list            - List constraints")
        self.println("    solver limit set <key> <val>      - Set limit")
        self.println("    solver limit list                 - List limits")
        self.println("    solver option set <key> <val>     - Set option")
        self.println("    solver option list                - List options")
        self.println("    solver show                       - Show full draft spec")
        self.println("    solver run                        - Run draft spec")
        self.println("")
        self.println("  Job management:")
        self.println("    solver run <json>                 - Run inline JSON (advanced)")
        self.println("    solver status <job_id>            - Poll job status")
        self.println("    solver result <job_id>            - Get job result")
        self.println("    solver cancel <job_id>            - Cancel running job")
        self.println("    solver apply <job_id> [index]   - Apply result to workspace")
        self.println("    solver dump [job_id] [--cube <name>] - Dump result into 3D cube (default RESULT)")
        self.println("    solver backends                   - List backends")
        self.println("    solver algorithms [backend_id]    - List algorithms")
        self.println("    solver export <job_id> [path]     - Export diagnostic report")
        self.println("")
        self.println("  Cell ref format: <cube_name>:<dim_name>.<item_name>")
        self.println("  Example: C:Input.x1")
        self.println("")
        self.println("Type 'help solver <subcommand>' for detailed help.")

    # --- Help topics ------------------------------------------------------

    def help_solver(self) -> None:
        self._solver_print_usage()

    def help_solver_run(self) -> None:
        self.println("solver run                - Run the draft problem spec")
        self.println("solver run <json_spec>    - Run inline JSON (advanced)")
        self.println("")
        self.println("With no arguments, runs the current draft spec built via")
        self.println("'solver variable/objective/constraint' commands.")
        self.println("Returns a job_id immediately. Poll with 'solver status <job_id>'.")
        self.println("")
        self.println("With a JSON argument, runs that spec directly (advanced users).")

    def help_solver_status(self) -> None:
        self.println("solver status <job_id>")
        self.println("")
        self.println("Poll the status of a running or completed solver job.")
        self.println("Returns: job status (running/cancelling/finished),")
        self.println("         termination status (optimal/feasible/infeasible/")
        self.println("         cancelled/timeout/failed),")
        self.println("         evaluation count, and message.")

    def help_solver_result(self) -> None:
        self.println("solver result <job_id>")
        self.println("")
        self.println("Retrieve the result of a finished solver job.")
        self.println("Returns: termination status, variable values, objective values,")
        self.println("         whether the result is applicable, and apply state.")
        self.println("The result can be applied with 'solver apply <job_id>' (single-objective)")
        self.println("or 'solver apply <job_id> <index>' (multi-objective Pareto front).")

    def help_solver_cancel(self) -> None:
        self.println("solver cancel <job_id>")
        self.println("")
        self.println("Request cancellation of a running solver job.")
        self.println("For in-process mode: cooperative cancellation via callback.")
        self.println("For subprocess mode: IPC cancel + SIGTERM/SIGKILL if unresponsive.")
        self.println("Poll with 'solver status <job_id>' to see when cancellation completes.")

    def help_solver_apply(self) -> None:
        self.println("solver apply <job_id> [index|apply_request_id]")
        self.println("")
        self.println("Apply the solver result to the workspace, writing optimized")
        self.println("variable values back to the model cells.")
        self.println("")
        self.println("For single-objective results, the solution is applied directly.")
        self.println("For multi-objective (Pareto front) results, specify an index")
        self.println("to select which Pareto optimal solution to apply.")
        self.println("Only results with termination status 'optimal' or 'feasible'")
        self.println("can be applied (unless allow_nonoptimal is set in policy).")
        self.println("Optionally specify a custom apply_request_id for tracking.")

    def help_solver_backends(self) -> None:
        self.println("solver backends")
        self.println("")
        self.println("List all registered solver backends and their supported algorithms.")
        self.println("Currently available: scipy (COBYLA, SLSQP, trust-constr,")
        self.println("  Nelder-Mead, Powell, linprog, and BOBYQA/NEWUOA/LINCOA")
        self.println("  with COBYLA fallback).")

    def help_solver_algorithms(self) -> None:
        self.println("solver algorithms [backend_id]")
        self.println("")
        self.println("List algorithms available in a backend (default: scipy).")
        self.println("Shows: algorithm ID, bounds support, constraint support,")
        self.println("       derivative requirements, and cancellation capability.")
        self.println("Use these algorithm IDs in the 'algorithm' field of 'solver run'.")

    def help_solver_constraint(self) -> None:
        self.println("solver constraint add <ref> <type> <bound>")
        self.println("solver constraint remove <idx>")
        self.println("solver constraint list")
        self.println("")
        self.println("Manage constraints in the draft problem spec.")
        self.println("")
        self.println("<ref> is a cell reference: <cube>:<dim>.<item>")
        self.println("<type> is one of: lower, upper, range, equality")
        self.println("<bound> is a float (for lower/upper/equality)")
        self.println("         or two floats (for range: lower upper)")
        self.println("")
        self.println("Examples:")
        self.println("  solver constraint add C:Input.g1 upper 10.0")
        self.println("  solver constraint add C:Input.g1 lower 5.0")
        self.println("  solver constraint add C:Input.g1 range 5.0 10.0")
        self.println("  solver constraint add C:Input.g1 equality 7.5")
        self.println("")
        self.println("Types: 'lower' (g >= bound), 'upper' (g <= bound),")
        self.println("       'range' (lower <= g <= upper), 'equality' (g == bound)")

    def help_solver_option(self) -> None:
        self.println("solver option <set|list|clear> [args]")
        self.println("")
        self.println("Manage solver options in the draft problem spec:")
        self.println("  solver option set <key> <value>  - Set an option (JSON or string)")
        self.println("  solver option list               - Show current options")
        self.println("  solver option clear              - Clear all options")
        self.println("")
        self.println("Common options: tol, maxiter, rhobeg (COBYLA),")
        self.println("  gtol (trust-constr), xatol/fatol (Nelder-Mead/Powell).")

    def help_solver_export(self) -> None:
        self.println("solver export <job_id> [file_path]")
        self.println("")
        self.println("Export a diagnostic report for a solver job as JSON.")
        self.println("Includes: job metadata, telemetry, termination status,")
        self.println("          variable/objective values, fingerprints, apply state.")
        self.println("If file_path is given, writes atomically to that path.")
        self.println("Otherwise returns the report dict in-memory.")

    def help_solver_new(self) -> None:
        self.println("solver new")
        self.println("")
        self.println("Reset the draft problem spec to defaults.")
        self.println("Clears all variables, objectives, constraints, options, and limits.")
        self.println("Default backend is 'scipy', default algorithm is 'auto'.")

    def help_solver_algorithm(self) -> None:
        self.println("solver algorithm <id>")
        self.println("")
        self.println("Set the optimization algorithm for the draft problem spec.")
        self.println("Use 'solver algorithms [backend_id]' to see available IDs.")
        self.println("")
        self.println("SciPy backend (default):")
        self.println("  auto         - Automatic selection based on problem structure")
        self.println("  cobyla       - Derivative-free, handles all constraint types")
        self.println("  slsqp        - Gradient-based, good for smooth constrained problems")
        self.println("  trust-constr - Robust for large-scale constrained problems")
        self.println("  linprog      - Linear programming (requires problem_class=linear)")
        self.println("")
        self.println("pymoo backend (multi-objective):")
        self.println("  nsga2        - NSGA-II, popular multi-objective evolutionary algorithm")
        self.println("  nsga3        - NSGA-III, for many-objective (4+ objectives)")
        self.println("  moead        - Decomposition-based multi-objective")
        self.println("  sms-emoa     - Hypervolume-based selection")
        self.println("  ga           - Single-objective genetic algorithm")
        self.println("")
        self.println("Use 'solver option set backend pymoo' to switch backends.")

    def help_solver_variable(self) -> None:
        self.println("solver variable add <ref> [lower_bound] [upper_bound]")
        self.println("solver variable remove <idx>")
        self.println("solver variable list")
        self.println("")
        self.println("Manage decision variables in the draft problem spec.")
        self.println("")
        self.println("<ref> is a cell reference: <cube>:<dim>.<item>")
        self.println("  For multi-dimension cubes, use colons between dim.item pairs:")
        self.println("  e.g. PF:Stock.S01:Metric.weight")
        self.println("")
        self.println("Bounds are optional (default: no bounds = unbounded).")
        self.println("  solver variable add C:Input.x1 0 100   # bounds [0, 100]")
        self.println("  solver variable add C:Input.x2          # unbounded")
        self.println("")
        self.println("remove takes the index shown by 'solver variable list'.")

    def help_solver_objective(self) -> None:
        self.println("solver objective add <ref> <min|max>")
        self.println("solver objective remove <idx>")
        self.println("solver objective list")
        self.println("")
        self.println("Manage objectives in the draft problem spec.")
        self.println("")
        self.println("<ref> is a cell reference (same format as solver variable).")
        self.println("Direction must be 'min' (minimize) or 'max' (maximize).")
        self.println("")
        self.println("Example:")
        self.println("  solver objective add C:Input.obj max")
        self.println("")
        self.println("remove takes the index shown by 'solver objective list'.")

    def help_solver_show(self) -> None:
        self.println("solver show")
        self.println("")
        self.println("Display the current draft problem spec in readable format.")
        self.println("Shows backend, algorithm, variables, objectives, constraints,")
        self.println("options, and limits.")

    def help_solver_limit(self) -> None:
        self.println("solver limit set <key> <value>")
        self.println("solver limit list")
        self.println("")
        self.println("Manage solver limits (stopping criteria) in the draft problem spec.")
        self.println("")
        self.println("Common limits:")
        self.println("  max_iterations         - Maximum number of iterations (default: 1000)")
        self.println("  tol                    - Tolerance for convergence (default: 1e-6)")
        self.println("  max_wall_time_seconds  - Wall-clock timeout in seconds (default: 300)")
        self.println("")
        self.println("Example:")
        self.println("  solver limit set max_iterations 500")
        self.println("  solver limit set tol 1e-8")

    def _solver_run(self, rest: list[str]) -> Any:
        wait = False
        if rest and rest[0] == "--wait":
            wait = True
            rest = rest[1:]

        if not rest:
            # Run the draft spec.
            draft = self._ensure_draft_spec()
            if not draft["variables"]:
                self.println("No variables in draft spec. Use 'solver variable add' first.")
                return None
            if not draft["objectives"]:
                self.println("No objectives in draft spec. Use 'solver objective add' first.")
                return None
            problem_spec = dict(draft)
        else:
            # Inline JSON mode (advanced).
            try:
                problem_spec = json.loads(" ".join(rest))
            except json.JSONDecodeError as e:
                self.println(f"Invalid JSON: {e}")
                return None

        result = self.session.execute("run_solver", problem_spec=problem_spec)
        if not result.success:
            self.println(f"Error: {result.error}")
            return None

        job_id = result.data["job_id"]
        self.println(f"Solver job started: {job_id}")

        if not wait:
            self.println(f"  Poll with: solver status {job_id}")
            self.println(f"  Get result with: solver result {job_id}")
            self.println(f"  Cancel with: solver cancel {job_id}")
            return None

        import time
        max_polls = 2400
        poll_interval = 0.5
        limits = problem_spec.get("limits", {})
        def _limit_num(key: str, default: int) -> int:
            v = limits.get(key, default)
            if isinstance(v, dict):
                return default
            return int(v)
        max_iterations = _limit_num("max_iterations", 1000)
        backend = problem_spec.get("backend", "scipy")
        if backend == "pymoo":
            pop_size = _limit_num("pop_size", 100)
            total_evals = pop_size * max_iterations
        else:
            total_evals = max_iterations
        start_time = time.time()
        last_evals = 0
        last_print_time = start_time
        for _ in range(max_polls):
            try:
                status = self.session.query("solver_status", job_id=job_id)
            except Exception as e:
                self.println(f"Error polling status: {e}")
                return None
            if not isinstance(status, dict):
                self.println(f"Error polling status: {status}")
                return None
            st = status.get("status")
            if st in ("finished", "failed", "cancelled"):
                self.println(f"Solver {st}: {status.get('termination_status', '')}")
                if status.get("message"):
                    self.println(f"  {status['message']}")
                if st == "finished":
                    try:
                        res = self.session.query("solver_result", job_id=job_id)
                    except Exception as e:
                        self.println(f"  (error fetching result: {e})")
                        return None
                    if isinstance(res, dict):
                        if res.get("is_multi_objective"):
                            front = res.get("pareto_front") or []
                            self.println(f"  Pareto front: {len(front)} solutions")
                            for i, pt in enumerate(front):
                                obj_str = ", ".join(f"{v:.6g}" for v in pt.get("objective_values", []))
                                self.println(f"    [{i}]  objectives=[{obj_str}]")
                            self.println("")
                            self.println(f"  Decision vectors:")
                            for i, pt in enumerate(front):
                                var_str = ", ".join(f"{v:.6g}" for v in pt.get("variable_values", []))
                                self.println(f"    [{i}]  variables=[{var_str}]")
                            self.println(f"  Apply with: solver apply {job_id} <index>")
                        else:
                            sol = res.get("solution")
                            if sol:
                                self.println(f"  Variables: {sol.get('variable_values')}")
                                self.println(f"  Objectives: {sol.get('objective_values')}")
                            self.println(f"  Evaluations: {status.get('n_evaluations', 0)}")
                            if res.get("applicable"):
                                apply_res = self.session.execute(
                                    "apply_solver_result", job_id=job_id
                                )
                                if apply_res.success:
                                    self.println(f"  Applied to workspace (revision={apply_res.data.get('committed_revision')})")
                                else:
                                    self.println(f"  Apply failed: {apply_res.error}")
                            else:
                                self.println(f"  Result not applicable. Apply manually: solver apply {job_id}")
                    else:
                        self.println(f"  (could not fetch result: {res})")
                return None
            # Progress update every 3 seconds
            now = time.time()
            if now - last_print_time >= 3.0:
                n_evals = status.get("n_evaluations", 0)
                elapsed = now - start_time
                if n_evals > 0 and n_evals < total_evals:
                    pct = n_evals / total_evals * 100
                    eta = elapsed / n_evals * (total_evals - n_evals)
                    self.println(f"  [{elapsed:.0f}s] {n_evals}/{total_evals} evals ({pct:.0f}%) — ETA {eta:.0f}s")
                elif n_evals > 0:
                    rate = n_evals / elapsed if elapsed > 0 else 0
                    self.println(f"  [{elapsed:.0f}s] {n_evals} evals ({rate:.1f}/s)")
                else:
                    self.println(f"  [{elapsed:.0f}s] running...")
                last_print_time = now
                last_evals = n_evals
            time.sleep(poll_interval)

        self.println(f"Solver job {job_id} timed out after {max_polls * poll_interval}s")
        self.println(f"  Poll manually with: solver status {job_id}")
        return None

    def _solver_status(self, rest: list[str]) -> Any:
        if not rest:
            jobs = self.session.query("solver_job_list")
            if isinstance(jobs, list) and len(jobs) > 0:
                self.println(f"Solver jobs ({len(jobs)}):")
                for j in jobs:
                    st = j.get("status", "?")
                    jid = j.get("job_id", "?")
                    term = j.get("termination_status") or ""
                    msg = j.get("message") or ""
                    ev = j.get("n_evaluations", 0)
                    line = f"  {jid}  status={st}"
                    if term:
                        line += f"  term={term}"
                    if ev:
                        line += f"  evals={ev}"
                    if msg:
                        line += f"  ({msg})"
                    self.println(line)
            elif isinstance(jobs, list) and len(jobs) == 0:
                self.println("No solver jobs.")
            else:
                self.println(f"Error listing jobs: {jobs}")
            return None
        job_id = rest[0]
        try:
            result = self.session.query("solver_status", job_id=job_id)
        except Exception as e:
            self.println(f"Error: {e}")
            return None
        if isinstance(result, dict):
            self.println(f"Job {job_id}: status={result.get('status')}, "
                         f"termination={result.get('termination_status')}")
            if result.get("status") == "finished":
                self.println(f"  Evaluations: {result.get('n_evaluations', 0)}")
                self.println(f"  Message: {result.get('message', '')}")
        else:
            self.println(f"Error: {result}")
        return None

    def _solver_result(self, rest: list[str]) -> Any:
        if not rest:
            self.println("Usage: solver result <job_id>")
            return None
        job_id = rest[0]
        result = self.session.query("solver_result", job_id=job_id)
        if isinstance(result, dict):
            self.println(f"Job {job_id}:")
            self.println(f"  Termination: {result.get('termination_status')}")
            self.println(f"  Applicable: {result.get('applicable')}")
            self.println(f"  Apply state: {result.get('apply_state')}")

            if result.get("is_multi_objective"):
                front = result.get("pareto_front") or []
                self.println(f"  Pareto front ({len(front)} solutions):")
                for i, pt in enumerate(front):
                    obj_str = ", ".join(f"{v:.6g}" for v in pt.get("objective_values", []))
                    self.println(f"    [{i}]  objectives=[{obj_str}]")
                self.println("")
                self.println(f"  Decision vectors:")
                for i, pt in enumerate(front):
                    var_str = ", ".join(f"{v:.6g}" for v in pt.get("variable_values", []))
                    self.println(f"    [{i}]  variables=[{var_str}]")
                self.println(f"  Apply with: solver apply {job_id} <index>")
            else:
                sol = result.get("solution")
                if sol:
                    self.println(f"  Variables: {sol.get('variable_values')}")
                    self.println(f"  Objectives: {sol.get('objective_values')}")
                    if sol.get('constraint_values') is not None:
                        self.println(f"  Constraints: {sol.get('constraint_values')}")
        else:
            self.println(f"Error: {result}")
        return None

    def _solver_cancel(self, rest: list[str]) -> Any:
        if not rest:
            self.println("Usage: solver cancel <job_id>")
            return None
        job_id = rest[0]
        result = self.session.execute("cancel_solver", job_id=job_id)
        if result.success:
            self.println(f"Cancel requested for job {job_id}: {result.data.get('status')}")
        else:
            self.println(f"Error: {result.error}")
        return None

    def _solver_apply(self, rest: list[str]) -> Any:
        pareto_index = None
        apply_request_id = None
        if rest:
            job_id = rest[0]
            if len(rest) > 1:
                # Could be an index (multi-objective) or an apply_request_id (single-objective).
                # Try to parse as int first — if it succeeds, treat as pareto_index.
                try:
                    pareto_index = int(rest[1])
                except ValueError:
                    apply_request_id = rest[1]
            if len(rest) > 2 and apply_request_id is not None:
                # rest[1] was apply_request_id, rest[2] could be index — unlikely but handle.
                pass
        else:
            jobs = self.session.query("solver_job_list")
            if not isinstance(jobs, list) or not jobs:
                self.println("No solver jobs to apply.")
                return None
            finished = [j for j in jobs if j.get("status") == "finished" and j.get("has_result")]
            if not finished:
                self.println("No finished solver jobs with results to apply.")
                return None
            job_id = finished[-1]["job_id"]
            self.println(f"Auto-applying most recent finished job: {job_id}")
        result = self.session.execute(
            "apply_solver_result",
            job_id=job_id,
            apply_request_id=apply_request_id,
            pareto_index=pareto_index,
        )
        if result.success:
            data = result.data
            self.println(f"Applied: revision={data.get('committed_revision')}, "
                         f"already_applied={data.get('already_applied')}")
        else:
            self.println(f"Error: {result.error}")
        return None

    def help_solver_dump(self) -> None:
        self.println("solver dump [job_id] [--cube <name>]")
        self.println("")
        self.println("Dump solver result into a 3D cube (default RESULT) with dimensions:")
        self.println("  ResultTag.<YYYYMMDD_HHMMSS>  — timestamp of this dump command")
        self.println("  ResultPoint.<p0,p1,...>      — Pareto front index (or p0 for single-objective)")
        self.println("  ResultMetric.<x1,...,obj1,...> — variable and objective names")
        self.println("")
        self.println("If job_id is omitted, uses the most recent finished job.")
        self.println("If --cube is omitted, defaults to RESULT.")
        self.println("Creates dimensions/cube if they don't exist.")
        self.println("New tags/points/metrics are appended to existing dimensions.")

    def _solver_dump(self, rest: list[str]) -> Any:
        """Dump solver result into a 3D cube (default RESULT) with ResultTag:ResultPoint:ResultMetric dims."""
        import time as _time

        # Parse args: optional job_id and --cube <name>.
        cube_name = "RESULT"
        job_id: str | None = None
        i = 0
        while i < len(rest):
            if rest[i] == "--cube" and i + 1 < len(rest):
                cube_name = rest[i + 1]
                i += 2
            elif rest[i].startswith("--cube="):
                cube_name = rest[i].split("=", 1)[1]
                i += 1
            elif job_id is None:
                job_id = rest[i]
                i += 1
            else:
                i += 1

        # Resolve job_id — default to latest finished job.
        if job_id is None:
            jobs = self.session.query("solver_job_list")
            if not isinstance(jobs, list) or not jobs:
                self.println("No solver jobs to dump.")
                return None
            finished = [j for j in jobs if j.get("status") == "finished" and j.get("has_result")]
            if not finished:
                self.println("No finished solver jobs with results to dump.")
                return None
            job_id = finished[-1]["job_id"]
            self.println(f"Dumping most recent finished job: {job_id}")

        # Fetch result.
        result = self.session.query("solver_result", job_id=job_id)
        if not isinstance(result, dict):
            self.println(f"Error fetching result: {result}")
            return None

        is_multi = result.get("is_multi_objective")
        if is_multi:
            front = result.get("pareto_front") or []
            if not front:
                self.println("Pareto front is empty — nothing to dump.")
                return None
            points = front
        else:
            sol = result.get("solution")
            if not sol:
                self.println("No solution in result — nothing to dump.")
                return None
            points = [sol]

        # Extract variable and objective param names from the draft spec.
        draft = self._ensure_draft_spec()
        var_specs = draft.get("variables", [])
        obj_specs = draft.get("objectives", [])

        def _dedup(names: list[str]) -> list[str]:
            """Ensure unique names by appending _2, _3, etc. for duplicates."""
            seen: dict[str, int] = {}
            result: list[str] = []
            for n in names:
                if n not in seen:
                    seen[n] = 1
                    result.append(n)
                else:
                    seen[n] += 1
                    result.append(f"{n}_{seen[n]}")
            return result

        var_names: list[str] = []
        for vi, vs in enumerate(var_specs):
            ref = vs.get("cell_ref") or vs.get("ref")
            if ref and "selectors" in ref:
                selectors = ref["selectors"]
                # Variable name = first selector's item value (e.g. x1 from Var.x1).
                var_names.append(list(selectors.values())[0] if selectors else f"var{vi}")
            else:
                var_names.append(f"var{vi}")

        obj_names: list[str] = []
        for oi, ospec in enumerate(obj_specs):
            ref = ospec.get("cell_ref") or ospec.get("ref")
            if ref and "selectors" in ref:
                selectors = ref["selectors"]
                # Objective name = last selector's item value (e.g. obj1 from Metric.obj1).
                obj_names.append(list(selectors.values())[-1] if selectors else f"obj{oi}")
            else:
                obj_names.append(f"obj{oi}")

        all_params = _dedup(var_names + obj_names)

        # Generate timestamp tag.
        tag = _time.strftime("%Y%m%d_%H%M%S")

        try:
            written = self._do_dump(points, all_params, tag, cube_name)
        except Exception as e:
            self.println(f"Error during dump: {e}")
            return None

        self.println(f"Dumped {len(points)} solution(s) x {len(all_params)} metrics = {written} cells into {cube_name} cube.")
        self.println(f"  ResultTag: {tag}")
        self.println(f"  ResultPoints: {len(points)}")
        self.println(f"  ResultMetrics: {', '.join(all_params)}")
        return None

    def _do_dump(self, points: list, all_params: list[str], tag: str, cube_name: str) -> int:
        """Create dims/cube and write solver results. Returns count of cells written."""
        # Query existing dimensions to check which items already exist.
        dim_data = self.session.query("dimension_list")
        if not dim_data:
            raise RuntimeError("could not query dimension list")

        # Build dim_name → dim_id and dim_id → {item_name: item_id} maps.
        dim_name_to_id: dict[str, str] = {}
        item_name_to_id: dict[str, dict[str, str]] = {}
        for d in dim_data.get("dimensions", []):
            d_id = d.get("id", "")
            d_name = d.get("name", d_id)
            dim_name_to_id[d_name] = d_id
            item_name_to_id[d_id] = {}
            for item in d.get("item_list", []):
                item_name_to_id[d_id][item.get("name", "")] = item.get("id", "")

        def _ensure_dim(dim_name: str) -> str:
            """Create dimension if missing, return its ID."""
            if dim_name in dim_name_to_id:
                return dim_name_to_id[dim_name]
            result = self.session.execute("create_dimension", name=dim_name, dim_type="set")
            if not result.success:
                raise RuntimeError(f"create_dimension '{dim_name}' failed: {result.error}")
            # Re-query to get the new dim ID.
            d2 = self.session.query("dimension_list")
            if not d2:
                raise RuntimeError("dimension_list query failed after create")
            for d in d2.get("dimensions", []):
                if d.get("name", d.get("id", "")) == dim_name:
                    dim_name_to_id[dim_name] = d.get("id", dim_name)
                    item_name_to_id[d.get("id", dim_name)] = {}
                    return d.get("id", dim_name)
            return dim_name

        def _ensure_items(dim_name: str, items: list[str]) -> None:
            """Add items that don't already exist in the dimension."""
            dim_id = dim_name_to_id.get(dim_name, dim_name)
            existing = set(item_name_to_id.get(dim_id, {}).keys())
            new_items = [it for it in items if it not in existing]
            if not new_items:
                return
            entries = [{"dim_id": dim_id, "name": it, "position": "append"} for it in new_items]
            result = self.session.execute("create_dimension_items_batch", entries=entries)
            if not result.success:
                raise RuntimeError(f"create_dimension_items_batch for '{dim_name}' failed: {result.error}")
            # Use returned IDs directly to update cache.
            returned_ids = (result.data or {}).get("ids", [])
            for it_name, it_id in zip(new_items, returned_ids):
                item_name_to_id[dim_id][it_name] = it_id

        # Ensure dimensions exist (Result-prefixed to avoid conflicts with existing dims).
        _ensure_dim("ResultTag")
        _ensure_dim("ResultPoint")
        _ensure_dim("ResultMetric")

        # Ensure items exist (only new ones are appended).
        point_items = [f"p{i}" for i in range(len(points))]
        _ensure_items("ResultTag", [tag])
        _ensure_items("ResultPoint", point_items)
        _ensure_items("ResultMetric", all_params)

        # Create cube if it doesn't exist.
        result = self.session.execute("create_cube", name=cube_name, dimension_ids=["ResultTag", "ResultPoint", "ResultMetric"])
        if result.success:
            cube_id = result.data.get("id", cube_name)
        else:
            # Cube might already exist — try to query it.
            cube_id = cube_name

        # Query cube detail to get dimension order.
        cube_detail = self.session.query("cube_detail", cube_id=cube_id)
        if not cube_detail:
            raise RuntimeError(f"could not query {cube_name} cube detail")

        cube_dim_ids = cube_detail.get("dimension_ids", [])

        # Write cells.
        written = 0
        failed = 0
        for pi, pt in enumerate(points):
            var_vals = pt.get("variable_values", [])
            obj_vals = pt.get("objective_values", [])
            all_vals = list(var_vals) + list(obj_vals)

            for param_idx, param_name in enumerate(all_params):
                if param_idx >= len(all_vals):
                    continue
                val = all_vals[param_idx]

                # Build address aligned to cube dimension order (skip @ dim).
                addr_list: list[str] = []
                for dim_id in cube_dim_ids:
                    if dim_id == "@":
                        continue
                    dim_name = None
                    for dn, did in dim_name_to_id.items():
                        if did == dim_id:
                            dim_name = dn
                            break
                    if dim_name == "ResultTag":
                        addr_list.append(item_name_to_id.get(dim_id, {}).get(tag, ""))
                    elif dim_name == "ResultPoint":
                        addr_list.append(item_name_to_id.get(dim_id, {}).get(f"p{pi}", ""))
                    elif dim_name == "ResultMetric":
                        addr_list.append(item_name_to_id.get(dim_id, {}).get(param_name, ""))
                    else:
                        # Unknown dimension — use first item.
                        items = item_name_to_id.get(dim_id, {})
                        addr_list.append(next(iter(items.values()), ""))

                r = self.session.execute(
                    "set_cell_hardvalue_by_addr",
                    cube_id=cube_id,
                    addr=addr_list,
                    value=str(val),
                )
                if r.success:
                    written += 1
                else:
                    failed += 1
                    if failed <= 3:
                        self.println(f"  Warning: write failed {tag}/p{pi}/{param_name}: {r.error}")

        if failed:
            self.println(f"  Warning: {failed} writes failed.")

        # Ensure a view exists for the result cube so the user can see it.
        # Layout: ResultMetric on rows, ResultPoint on cols, ResultTag + @ on page.
        dim_name_to_id_rev: dict[str, str] = {}
        for dn, did in dim_name_to_id.items():
            dim_name_to_id_rev[did] = dn
        row_dims: list[str] = []
        col_dims: list[str] = []
        page_dims: list[str] = []
        for did in cube_dim_ids:
            dn = dim_name_to_id_rev.get(did, "")
            if dn == "ResultMetric":
                row_dims = [did]
            elif dn == "ResultPoint":
                col_dims = [did]
            elif dn == "ResultTag" or did == "@":
                page_dims.append(did)
        if not row_dims and user_dim_ids:
            row_dims = [user_dim_ids[0]]
        if not col_dims and len(user_dim_ids) > 1:
            col_dims = [user_dim_ids[1]]
        if not page_dims:
            page_dims = ["@"]
        view_result = self.session.execute(
            "create_view",
            name=f"View of {cube_name}",
            cube_id=cube_id,
            row_dims=row_dims,
            col_dims=col_dims,
            page_dim_ids=page_dims,
        )
        if not view_result.success:
            self.println(f"  Warning: could not create view for {cube_name}: {view_result.error}")

        return written

    def _solver_backends(self) -> Any:
        result = self.session.query("solver_backend_list")
        if isinstance(result, list):
            for b in result:
                self.println(f"  {b['backend_id']}: algorithms={b['algorithms']}")
        else:
            self.println(f"Error: {result}")
        return None

    _ALGORITHM_DESCRIPTIONS: dict[tuple[str, str], list[str]] = {
        ("scipy", "auto"): [
            "Automatic algorithm selection. Inspects the problem structure and",
            "chooses the most suitable solver. If option problem_class='linear'",
            "is set, selects linprog (simplex/interior-point LP). Otherwise",
            "defaults to cobyla (derivative-free, handles all constraint types).",
        ],
        ("scipy", "cobyla"): [
            "Constrained optimization by linear approximation. Derivative-free",
            "method that models the objective and constraints as linear",
            "functions in a trust region. Good for small-to-medium problems",
            "with inequality and equality constraints.",
        ],
        ("scipy", "bobyqa"): [
            "Bound-constrained optimization by quadratic approximation.",
            "Derivative-free method using quadratic models in a trust region.",
            "Supports bound constraints only — no inequality or equality.",
        ],
        ("scipy", "newuoa"): [
            "New unconstrained optimization by quadratic approximation.",
            "Derivative-free method for unconstrained problems. No bounds,",
            "no constraints. Good for smooth but noisy objectives.",
        ],
        ("scipy", "lincoa"): [
            "Linearly constrained optimization by quadratic approximation.",
            "Derivative-free method supporting linear inequality constraints",
            "and bounds. No equality constraints.",
        ],
        ("scipy", "linprog"): [
            "Linear programming solver. Uses the simplex or interior-point",
            "method for linear objectives with linear constraints. The only",
            "algorithm designed specifically for LP problems. Supports bounds,",
            "inequality and equality constraints.",
        ],
        ("scipy", "slsqp"): [
            "Sequential least squares programming. Gradient-based method that",
            "handles bounds, inequality and equality constraints. Requires",
            "derivatives (approximated numerically if not provided). Good for",
            "medium-scale constrained smooth problems.",
        ],
        ("scipy", "trust-constr"): [
            "Trust-region constrained optimization. Uses second-order",
            "information to solve constrained problems. Supports bounds,",
            "inequality and equality constraints. Robust for large-scale",
            "smooth problems. Requires derivatives.",
        ],
        ("scipy", "nelder-mead"): [
            "Nelder-Mead simplex method. Derivative-free heuristic for",
            "unconstrained problems. Supports bounds only. Simple but",
            "slow — best for low-dimensional rough objectives.",
        ],
        ("scipy", "powell"): [
            "Powell's conjugate direction method. Derivative-free for",
            "unconstrained problems. Supports bounds only. More efficient",
            "than Nelder-Mead for smooth objectives.",
        ],
        ("pymoo", "auto"): [
            "Automatic algorithm selection. For single-objective problems,",
            "selects GA (genetic algorithm). For 2-3 objectives, selects",
            "NSGA-II. For 4+ objectives, selects NSGA-III with reference",
            "directions.",
        ],
        ("pymoo", "nsga2"): [
            "Non-dominated Sorting Genetic Algorithm II. The most popular",
            "multi-objective evolutionary algorithm. Uses elitist sorting",
            "and crowding distance to maintain diversity. Good for 2-3",
            "objectives. Supports bounds and all constraint types.",
        ],
        ("pymoo", "nsga3"): [
            "Non-dominated Sorting Genetic Algorithm III. Extension of",
            "NSGA-II using reference directions for many-objective",
            "optimization (4+ objectives). Better diversity maintenance",
            "than NSGA-II for high-dimensional Pareto fronts.",
        ],
        ("pymoo", "moead"): [
            "Multi-Objective Evolutionary Algorithm based on Decomposition.",
            "Decomposes the multi-objective problem into scalar subproblems",
            "using weight vectors. Good for problems with a known preference",
            "structure. Supports bounds and all constraint types.",
        ],
        ("pymoo", "sms-emoa"): [
            "S-Metric Selection Evolutionary Multiobjective Algorithm.",
            "Uses hypervolume contribution for environmental selection.",
            "Produces well-distributed Pareto fronts but is computationally",
            "more expensive per generation. Good for 2-3 objectives.",
        ],
        ("pymoo", "ga"): [
            "Genetic Algorithm (single-objective). Evolutionary optimizer",
            "using crossover and mutation. Derivative-free, handles bounds",
            "and all constraint types. Good for non-smooth or discontinuous",
            "single-objective problems.",
        ],
    }

    def _solver_algorithms(self, rest: list[str]) -> Any:
        indent = "    "

        def _print_backend_algos(bid: str) -> None:
            result = self.session.query("solver_algorithm_list", backend_id=bid)
            if not isinstance(result, list):
                self.println(f"Error querying {bid}: {result}")
                return
            linear = [a for a in result if a.get("linear")]
            nonlinear = [a for a in result if not a.get("linear")]
            if linear:
                self.println("  Linear:")
                for a in linear:
                    aid = a['algorithm_id']
                    self.println(f"    {aid}: bounds={a['supports_bounds']}, "
                                 f"ineq={a['supports_inequality_constraints']}, eq={a['supports_equality_constraints']}")
                    desc = self._ALGORITHM_DESCRIPTIONS.get((bid, aid))
                    if desc:
                        for line in desc:
                            self.println(f"{indent}    {line}")
            if nonlinear:
                self.println("  Non-linear:")
                for a in nonlinear:
                    aid = a['algorithm_id']
                    self.println(f"    {aid}: bounds={a['supports_bounds']}, "
                                 f"ineq={a['supports_inequality_constraints']}, eq={a['supports_equality_constraints']}")
                    desc = self._ALGORITHM_DESCRIPTIONS.get((bid, aid))
                    if desc:
                        for line in desc:
                            self.println(f"{indent}    {line}")

        if rest:
            backend_id = rest[0]
            self.println(f"Backend: {backend_id}")
            _print_backend_algos(backend_id)
        else:
            backends = self.session.query("solver_backend_list")
            if not isinstance(backends, list):
                self.println(f"Error: {backends}")
                return None
            for b in backends:
                bid = b["backend_id"]
                self.println(f"Backend: {bid}")
                _print_backend_algos(bid)
                self.println("")
        return None

    def _solver_export(self, rest):
        if not rest:
            self.println("Usage: solver export <job_id> [file_path]"); return None
        job_id = rest[0]
        file_path = rest[1] if len(rest) > 1 else None
        result = self.session.execute("export_solver", job_id=job_id, file_path=file_path)
        if result.success:
            data = result.data
            if data.get("_exported_to"):
                self.println(f"Exported job {job_id} to {data['_exported_to']}")
            else:
                self.println(f"Exported job {job_id} (in-memory)")
        else:
            self.println(f"Error: {result.error}")
        return None

    def _ensure_draft_spec(self):
        if not hasattr(self, "_solver_draft_spec"):
            self._solver_draft_spec = {"backend": "scipy", "algorithm": "auto",
                "variables": [], "objectives": [], "constraints": [], "options": {}, "limits": {}}
        return self._solver_draft_spec

    @staticmethod
    def _parse_cell_ref(ref: str) -> dict | None:
        """Parse a cell reference like 'C:Input.x1' or 'PF:Stock.S01:Metric.weight' into {cube_name, selectors}."""
        if ":" not in ref:
            return None
        parts = ref.split(":")
        cube_name = parts[0].strip()
        selectors = {}
        for part in parts[1:]:
            part = part.strip()
            if "." not in part:
                return None
            dim_name, item_name = part.split(".", 1)
            selectors[dim_name.strip()] = item_name.strip()
        if not cube_name or not selectors:
            return None
        return {"cube_name": cube_name, "selectors": selectors}

    @staticmethod
    def _parse_bound_value(arg: str) -> tuple[dict | float | None, str | None]:
        """Parse a bound argument that can be a cell ref, a float, or null/none.

        Returns (value, error_message).
        - If arg is 'null' or 'none' (case-insensitive), returns (None, None).
        - If arg parses as a cell ref, returns ({cube_name, selectors}, None).
        - If arg parses as a float, returns (float_value, None).
        - Otherwise returns (None, error_message).
        """
        if arg.lower() in ("null", "none"):
            return (None, None)
        cell_ref = REPLSolverMixin._parse_cell_ref(arg)
        if cell_ref is not None:
            return (cell_ref, None)
        try:
            return (float(arg), None)
        except ValueError:
            return (None, f"Cannot parse '{arg}' as cell ref or float")

    def _solver_new(self, rest: list[str]) -> Any:
        self._solver_draft_spec = {"backend": "scipy", "algorithm": "auto",
            "variables": [], "objectives": [], "constraints": [], "options": {}, "limits": {}}
        self.println("Draft spec reset. Use 'solver variable/objective/constraint' to build.")
        return None

    def _solver_algorithm(self, rest: list[str]) -> Any:
        if not rest:
            self.println("Usage: solver algorithm <id>")
            self.println("Available: auto, cobyla, slsqp, trust-constr, nelder-mead, powell, linprog")
            return None
        draft = self._ensure_draft_spec()
        draft["algorithm"] = rest[0]
        self.println(f"Algorithm set to: {rest[0]}")
        return None

    def _solver_variable(self, rest: list[str]) -> Any:
        if not rest:
            self.println("Usage: solver variable <add|remove|list> [args]")
            return None
        sub, args = rest[0], rest[1:]
        draft = self._ensure_draft_spec()
        if sub == "add":
            if not args:
                self.println("Usage: solver variable add <ref> [lower_bound] [upper_bound]")
                self.println("  <ref> = <cube>:<dim>.<item>")
                self.println("  Bounds can be floats OR cell refs (e.g. C:Dim.lb)")
                self.println("  Example: solver variable add C:Input.x1 -10 10")
                self.println("  Example: solver variable add C:Input.x1 C:Dim.lb C:Dim.ub")
                return None
            cell_ref = self._parse_cell_ref(args[0])
            if cell_ref is None:
                self.println(f"Invalid cell ref: {args[0]}")
                self.println("Format: <cube>:<dim>.<item>  e.g. C:Input.x1")
                return None
            var = {"cell_ref": cell_ref}
            if len(args) > 1:
                val, err = self._parse_bound_value(args[1])
                if err: self.println(f"Invalid lower_bound: {err}"); return None
                if val is not None: var["lower_bound"] = val
            if len(args) > 2:
                val, err = self._parse_bound_value(args[2])
                if err: self.println(f"Invalid upper_bound: {err}"); return None
                if val is not None: var["upper_bound"] = val
            draft["variables"].append(var)
            lb_desc = var.get('lower_bound', '-')
            ub_desc = var.get('upper_bound', '-')
            self.println(f"Added variable #{len(draft['variables'])-1}: {args[0]}"
                + (f"  bounds=[{lb_desc}, {ub_desc}]" if 'lower_bound' in var or 'upper_bound' in var else ""))
            return None
        elif sub == "remove":
            if not args:
                self.println("Usage: solver variable remove <index>"); return None
            try: idx = int(args[0])
            except ValueError: self.println("Index must be integer"); return None
            if 0 <= idx < len(draft["variables"]):
                removed = draft["variables"].pop(idx)
                self.println(f"Removed variable #{idx}: {removed}")
                return None
            self.println(f"Index {idx} out of range (0..{len(draft['variables'])-1})")
            return None
        elif sub == "list":
            vars_ = draft["variables"]
            if not vars_: self.println("No variables in draft spec")
            else:
                for i, v in enumerate(vars_):
                    ref = v["cell_ref"]
                    ref_str = f"{ref['cube_name']}:" + ",".join(f"{k}.{v_}" for k,v_ in ref["selectors"].items())
                    bounds = f"  bounds=[{v.get('lower_bound', '-')}, {v.get('upper_bound', '-')}]"
                    self.println(f"  #{i}: {ref_str}{bounds}")
            return None
        else:
            self.println(f"Unknown variable subcommand: {sub}"); return None

    def _solver_objective(self, rest: list[str]) -> Any:
        if not rest:
            self.println("Usage: solver objective <add|remove|list> [args]"); return None
        sub, args = rest[0], rest[1:]
        draft = self._ensure_draft_spec()
        if sub == "add":
            if len(args) < 2:
                self.println("Usage: solver objective add <ref> <min|max|ref>")
                self.println("  <ref> = <cube>:<dim>.<item>")
                self.println("  Direction can be literal 'min'/'max' OR a cell ref")
                self.println("  Example: solver objective add C:Input.obj min")
                self.println("  Example: solver objective add C:Input.obj C:Dim.dir")
                return None
            cell_ref = self._parse_cell_ref(args[0])
            if cell_ref is None:
                self.println(f"Invalid cell ref: {args[0]}")
                self.println("Format: <cube>:<dim>.<item>  e.g. C:Input.obj")
                return None
            # Check if second arg is a literal direction or a cell ref.
            dir_arg = args[1].lower()
            if dir_arg in ("min", "minimize"):
                direction = "minimize"
            elif dir_arg in ("max", "maximize"):
                direction = "maximize"
            else:
                dir_ref = self._parse_cell_ref(args[1])
                if dir_ref is not None:
                    direction = {"direction_ref": dir_ref}
                else:
                    self.println(f"Invalid direction: {args[1]}. Use 'min', 'max', or a cell ref."); return None
            obj = {"cell_ref": cell_ref, "direction": direction}
            draft["objectives"].append(obj)
            dir_desc = direction if isinstance(direction, str) else args[1]
            self.println(f"Added objective #{len(draft['objectives'])-1}: {args[0]} ({dir_desc})")
            return None
        elif sub == "remove":
            if not args:
                self.println("Usage: solver objective remove <index>"); return None
            try: idx = int(args[0])
            except ValueError: self.println("Index must be integer"); return None
            if 0 <= idx < len(draft["objectives"]):
                removed = draft["objectives"].pop(idx)
                self.println(f"Removed objective #{idx}: {removed}")
                return None
            self.println(f"Index {idx} out of range (0..{len(draft['objectives'])-1})")
            return None
        elif sub == "list":
            objs = draft["objectives"]
            if not objs: self.println("No objectives in draft spec")
            else:
                for i, o in enumerate(objs):
                    ref = o["cell_ref"]
                    ref_str = f"{ref['cube_name']}:" + ",".join(f"{k}.{v}" for k,v in ref["selectors"].items())
                    self.println(f"  #{i}: {ref_str}  ({o['direction']})")
            return None
        else:
            self.println(f"Unknown objective subcommand: {sub}"); return None

    def _solver_limit(self, rest: list[str]) -> Any:
        if not rest:
            self.println("Usage: solver limit <set|list> [args]"); return None
        sub, args = rest[0], rest[1:]
        draft = self._ensure_draft_spec()
        if sub == "set":
            if len(args) < 2:
                self.println("Usage: solver limit set <key> <value|ref>"); return None
            key = args[0]
            # Try cell ref first, then JSON, then raw string.
            cell_ref = self._parse_cell_ref(args[1])
            if cell_ref is not None:
                val = {"cell_ref": cell_ref}
            else:
                try: val = json.loads(args[1])
                except json.JSONDecodeError: val = args[1]
            draft["limits"][key] = val
            self.println(f"Set limit {key} = {val}")
            return None
        elif sub == "list":
            limits = draft["limits"]
            if not limits: self.println("No limits set (defaults will be used)")
            else:
                for k, v in limits.items(): self.println(f"  {k} = {v}")
            return None
        else:
            self.println(f"Unknown limit subcommand: {sub}"); return None

    def _solver_show(self, rest: list[str]) -> Any:
        draft = self._ensure_draft_spec()
        self.println(f"Solver Problem Spec")
        self.println(f"  Backend:    {draft.get('backend', 'scipy')}")
        self.println(f"  Algorithm:  {draft.get('algorithm', 'auto')}")
        self.println("")
        vars_ = draft.get("variables", [])
        if vars_:
            self.println(f"  Variables ({len(vars_)}):")
            for i, v in enumerate(vars_):
                ref = v["cell_ref"]
                ref_str = f"{ref['cube_name']}:" + ".".join(f"{k}.{val}" for k, val in ref.get("selectors", {}).items())
                lb = v.get("lower_bound", "-inf")
                ub = v.get("upper_bound", "+inf")
                self.println(f"    [{i}] {ref_str}  bounds=[{lb}, {ub}]")
        else:
            self.println("  Variables: (none)")
        self.println("")
        objs = draft.get("objectives", [])
        if objs:
            self.println(f"  Objectives ({len(objs)}):")
            for i, o in enumerate(objs):
                ref = o["cell_ref"]
                ref_str = f"{ref['cube_name']}:" + ".".join(f"{k}.{val}" for k, val in ref.get("selectors", {}).items())
                self.println(f"    [{i}] {ref_str}  ({o.get('direction', 'minimize')})")
        else:
            self.println("  Objectives: (none)")
        self.println("")
        cons = draft.get("constraints", [])
        if cons:
            self.println(f"  Constraints ({len(cons)}):")
            for i, c in enumerate(cons):
                ref = c["cell_ref"]
                ref_str = f"{ref['cube_name']}:" + ".".join(f"{k}.{val}" for k, val in ref.get("selectors", {}).items())
                ctype = c.get("type", "?")
                if ctype == "range":
                    bound_str = f"[{c.get('lower_bound')}, {c.get('upper_bound')}]"
                else:
                    bound_str = str(c.get("bound", "?"))
                self.println(f"    [{i}] {ref_str}  {ctype} {bound_str}")
        else:
            self.println("  Constraints: (none)")
        opts = draft.get("options", {})
        limits = draft.get("limits", {})
        if opts or limits:
            self.println("")
            if opts:
                self.println(f"  Options: {opts}")
            if limits:
                self.println(f"  Limits: {limits}")
        return None

    def _solver_constraint(self, rest):
        if not rest:
            self.println("Usage: solver constraint <add|remove|list> [args]"); return None
        sub, args = rest[0], rest[1:]
        draft = self._ensure_draft_spec()
        if sub == "add":
            if len(args) < 2:
                self.println("Usage: solver constraint add <ref> <type> <bound> [bound2]")
                self.println("  Or:    solver constraint add <ref> <min_ref|num|null> <max_ref|num|null>")
                self.println("  <ref>  = <cube>:<dim>.<item>  e.g. C:Input.g1")
                self.println("  <type> = lower, upper, range, equality")
                self.println("  <bound> = float or cell ref")
                self.println("")
                self.println("Examples:")
                self.println("  solver constraint add C:Input.g1 upper 10.0")
                self.println("  solver constraint add C:Input.g1 range 5.0 10.0")
                self.println("  solver constraint add C:Input.g1 C:Dim.lb C:Dim.ub")
                self.println("  solver constraint add C:Input.g1 null C:Dim.ub")
                return None
            cell_ref = self._parse_cell_ref(args[0])
            if cell_ref is None:
                self.println(f"Invalid cell ref: {args[0]}")
                self.println("Format: <cube>:<dim>.<item>  e.g. C:Input.g1")
                return None
            # Detect syntax: if args[1] is a known type keyword, use classic syntax.
            con_type_keyword = args[1].lower() if len(args) > 1 else ""
            valid_types = {"lower", "upper", "range", "equality"}
            if con_type_keyword in valid_types:
                # Classic syntax: <ref> <type> <bound> [bound2]
                con_type = con_type_keyword
                if len(args) < 3:
                    self.println(f"Constraint type '{con_type}' needs a bound value")
                    return None
                con = {"cell_ref": cell_ref, "type": con_type}
                if con_type == "range":
                    if len(args) < 4:
                        self.println("Range constraint needs two bounds: <lower> <upper>")
                        return None
                    lb, err = self._parse_bound_value(args[2])
                    if err: self.println(f"Invalid lower bound: {err}"); return None
                    ub, err = self._parse_bound_value(args[3])
                    if err: self.println(f"Invalid upper bound: {err}"); return None
                    con["lower_bound"] = lb
                    con["upper_bound"] = ub
                else:
                    bv, err = self._parse_bound_value(args[2])
                    if err: self.println(f"Invalid bound: {err}"); return None
                    con["bound"] = bv
                draft["constraints"].append(con)
                ref_str = args[0]
                if con_type == "range":
                    self.println(f"Added constraint #{len(draft['constraints'])-1}: {ref_str} {con_type} [{con.get('lower_bound')}, {con.get('upper_bound')}]")
                else:
                    self.println(f"Added constraint #{len(draft['constraints'])-1}: {ref_str} {con_type} {con.get('bound')}")
                return None
            else:
                # Cell-ref syntax: <ref> <min_ref|num|null> <max_ref|num|null>
                if len(args) < 3:
                    self.println("Cell-ref constraint needs: <ref> <min_ref|num|null> <max_ref|num|null>")
                    return None
                lb, err = self._parse_bound_value(args[1])
                if err: self.println(f"Invalid min bound: {err}"); return None
                ub, err = self._parse_bound_value(args[2])
                if err: self.println(f"Invalid max bound: {err}"); return None
                con = {"cell_ref": cell_ref, "type": "range", "lower_bound": lb, "upper_bound": ub}
                draft["constraints"].append(con)
                self.println(f"Added constraint #{len(draft['constraints'])-1}: {args[0]} range [{lb}, {ub}]")
                return None
        elif sub == "remove":
            if not args:
                self.println("Usage: solver constraint remove <index>"); return None
            try: idx = int(args[0])
            except ValueError: self.println("Index must be integer"); return None
            if 0 <= idx < len(draft["constraints"]):
                removed = draft["constraints"].pop(idx)
                self.println(f"Removed constraint #{idx}: {removed}")
                return None
            self.println(f"Index {idx} out of range (0..{len(draft['constraints'])-1})")
            return None
        elif sub == "list":
            cons = draft["constraints"]
            if not cons: self.println("No constraints in draft spec")
            else:
                for i, c in enumerate(cons):
                    ref = c["cell_ref"]
                    ref_str = f"{ref['cube_name']}:" + ",".join(f"{k}.{v}" for k,v in ref["selectors"].items())
                    if c["type"] == "range":
                        self.println(f"  #{i}: {ref_str}  range [{c.get('lower_bound')}, {c.get('upper_bound')}]")
                    else:
                        self.println(f"  #{i}: {ref_str}  {c['type']} {c.get('bound', '')}")
            return None
        else:
            self.println(f"Unknown constraint subcommand: {sub}"); return None

    def _solver_option(self, rest):
        if not rest:
            self.println("Usage: solver option <set|list|clear> [args]"); return None
        sub, args = rest[0], rest[1:]
        draft = self._ensure_draft_spec()
        if sub == "set":
            if len(args) < 2:
                self.println("Usage: solver option set <key> <value>"); return None
            key = args[0]
            val_str = " ".join(args[1:])
            try: val = json.loads(val_str)
            except json.JSONDecodeError: val = val_str
            if key == "backend":
                draft["backend"] = str(val)
                self.println(f"Set backend = {val}")
            else:
                draft["options"][key] = val
                self.println(f"Set option {key} = {val}")
            return None
        elif sub == "list":
            known_options = {
                "problem_class": "Problem classification hint for algorithm selection",
            }
            opts = draft["options"]
            self.println("Solver options:")
            self.println(f"  backend = {draft.get('backend', 'scipy')}")
            self.println(f"    Solver backend (scipy or pymoo)")
            for key, desc in known_options.items():
                val = opts.get(key)
                if val is not None:
                    self.println(f"  {key} = {val}")
                else:
                    self.println(f"  {key} = (not set)")
                self.println(f"    {desc}")
            extra = set(opts.keys()) - set(known_options.keys())
            for key in sorted(extra):
                self.println(f"  {key} = {opts[key]}")
            return None
        elif sub == "clear":
            draft["options"] = {}
            self.println("Cleared all options")
            return None
        else:
            self.println(f"Unknown option subcommand: {sub}"); return None
