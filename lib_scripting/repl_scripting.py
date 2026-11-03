"""
REPL Scripting - Echo, assert, timestamp, variable assignment.

Commands for script control flow, variable management, and debugging.

Supported syntax:
- name=value             : Variable assignment (bash-style, no space around =)
- var name = value       : Explicit variable creation with space-friendly syntax
- $name                  : Variable expansion in commands
- ${name}                : Variable expansion with delimiter
- $(selection)           : Command substitution
- $(command args)        : Execute command, capture output
"""

from __future__ import annotations

import re
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib_repl.repl_core import OpenMREPLCore


class REPLScriptingMixin:
    """Mixin for scripting operations."""

    # ---- Echo ----

    def do_echo(self: OpenMREPLCore, arg: str):
        """
        Print arguments to stdout.
        Usage: echo [text...]
        Example: echo Hello World
                 echo "Variables: $count $name"
        """
        print(arg)

    def _resolve_openm_ref(self, ref: str):
        """Resolve an OpenM rule reference like CubeName::@.value:DimName.ItemName.

        The REPL is a parser only. All cube/dimension/item resolution and
        address construction happens inside the query boundary.
        """
        if not ref or not isinstance(ref, str):
            return None

        ref = ref.strip()
        if "::" not in ref:
            return None

        try:
            cube_name, rest = ref.split("::", 1)
            cube_name = cube_name.strip()

            # Parse channel and selectors from @.value:Dim.Item:Dim.Item
            parts = rest.split(":")
            if not parts:
                return None

            channel_part = parts[0].strip()
            if channel_part == "@":
                channel = "value"
            elif channel_part.startswith("@."):
                channel = channel_part[2:]  # "value", "font_color", etc.
            else:
                return None

            selectors = []
            for i in range(1, len(parts)):
                sel = parts[i].strip()
                if "." not in sel:
                    return None
                dim_name, item_name = sel.split(".", 1)
                dim_name = dim_name.strip()
                item_name = item_name.strip()
                if not dim_name or not item_name:
                    return None
                selectors.append({"dimension": dim_name, "item": item_name})

            dto = self.session.query(
                "cell_value_by_ref",
                cube_name=cube_name,
                channel=channel,
                selectors=selectors,
            )
            if dto and dto.get("error"):
                return None
            return dto.get("value") if dto else None

        except Exception:
            return None
    
    def _safe_assert_parse(self, condition: str) -> bool:
        """Parse and evaluate a safe numeric comparison.

        Accepted forms:
          <value> == <literal>   <value> != <literal>
          <value> >  <number>    <value> >= <number>
          <value> <  <number>    <value> <= <number>
        Rejects anything else cleanly.
        """
        import re

        # Normalize whitespace
        cond = condition.strip()
        if not cond:
            raise ValueError("Empty condition")

        # Match comparison operators (longest first to avoid > matching >=)
        for op in ("==", "!=", ">=", "<=", ">", "<"):
            if op in cond:
                parts = cond.split(op, 1)
                if len(parts) == 2:
                    left = parts[0].strip()
                    right = parts[1].strip()
                    break
        else:
            raise ValueError(f"Unsupported condition: {cond}")

        # Reject function calls or other suspicious syntax
        for operand in (left, right):
            if '(' in operand or ')' in operand:
                raise ValueError(f"Function calls not allowed in assert: {operand}")
            if '__' in operand:
                raise ValueError(f"Dunder names not allowed in assert: {operand}")

        # Parse operands: try numeric first, then bool, then string
        def _parse_operand(val: str):
            try:
                return int(val)
            except ValueError:
                try:
                    return float(val)
                except ValueError:
                    lv = val.lower()
                    if lv == "true":
                        return True
                    if lv == "false":
                        return False
                    # Strip surrounding quotes if present
                    if (val.startswith('"') and val.endswith('"')) or \
                       (val.startswith("'") and val.endswith("'")):
                        return val[1:-1]
                    return val

        left_val = _parse_operand(left)
        right_val = _parse_operand(right)

        if op == "==":
            return left_val == right_val
        if op == "!=":
            return left_val != right_val
        if op == ">=":
            return left_val >= right_val
        if op == "<=":
            return left_val <= right_val
        if op == ">":
            return left_val > right_val
        if op == "<":
            return left_val < right_val
        raise ValueError(f"Unknown operator: {op}")

    def do_assert(self: OpenMREPLCore, arg: str):
        """
        Assert a condition and halt execution on failure.
        Usage: assert <condition> [message]
        Example: assert 5 > 3
                 assert count == 10 "Count should be 10"
                 assert Test::@.value:Measure.A == 10 "A should be 10"

        Returns True to halt script execution on failure.
        """
        if not arg:
            print("Usage: assert <condition> [message]")
            return

        message = None
        if '"' in arg:
            parts = arg.split('"', 2)
            condition_str = parts[0].strip()
            if len(parts) >= 2:
                message = parts[1]
        else:
            condition_str = arg

        try:
            # Resolve OpenM rule references in the condition
            resolved_condition = condition_str
            import re
            openm_refs = re.findall(
                r'[A-Za-z_][A-Za-z0-9_]*::@(?:\.[A-Za-z_][A-Za-z0-9_]*)?:[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*(?:\:[A-Za-z_][A-Za-z0-9_]*\.[A-Za-z_][A-Za-z0-9_]*)*',
                condition_str,
            )
            for ref in openm_refs:
                resolved_value = self._resolve_openm_ref(ref)
                if resolved_value is not None:
                    resolved_condition = resolved_condition.replace(ref, str(resolved_value))

            result = self._safe_assert_parse(resolved_condition)

            if result:
                print(f"assert passed: {condition_str}")
                return False
            else:
                error_msg = message or f"Assertion failed: {condition_str}"
                print(f"ASSERTION FAILED: {error_msg}")
                return True
        except Exception as e:
            error_msg = message or f"Could not evaluate: {condition_str} ({e})"
            print(f"ASSERTION FAILED: {error_msg}")
            return True

    def do_timestamp(self: OpenMREPLCore, arg: str):
        """
        Print current timestamp in ISO format.
        Usage: timestamp [format]
        Example: timestamp          # 2026-05-03T11:42:00+12:00
                 timestamp %Y%m%d_%H%M%S  # 20260503_114200
        """
        from datetime import datetime
        fmt = arg.strip() if arg else '%Y-%m-%dT%H:%M:%S%z'
        ts = datetime.now().strftime(fmt)
        print(ts)
        return ts

    # ---- Vars: list all variables (optionally global) ----

    def do_vars(self: OpenMREPLCore, arg: str):
        """
        List all shell scripting variables.
        Usage: vars [name]
               vars -g             # List global variables
        Example: vars              # List all variables
                 vars count        # Show value of 'count'
        """
        if arg.strip().startswith("-g"):
            # List global variables
            global_vars = self.global_vars
            if not global_vars:
                print("No global variables set.")
                return
            print(f"{'Variable':<25} {'Value':<40} {'Type':<15}")
            print("-" * 80)
            for name, value in sorted(global_vars.items()):
                value_str = str(value)
                if len(value_str) > 38:
                    value_str = value_str[:35] + "..."
                print(f"{name:<25} {value_str:<40} {type(value).__name__:<15}")
            return

        variables = self.variables
        if not arg.strip():
            if not variables:
                print("No variables set.")
                return
            print(f"{'Variable':<25} {'Value':<40} {'Type':<15}")
            print("-" * 80)
            for name, value in sorted(variables.items()):
                value_str = str(value)
                if len(value_str) > 38:
                    value_str = value_str[:35] + "..."
                print(f"{name:<25} {value_str:<40} {type(value).__name__:<15}")
        else:
            name = arg.strip()
            if name in variables:
                print(f"{name} = {variables[name]!r} ({type(variables[name]).__name__})")
            elif name in self.global_vars:
                print(f"{name} = {self.global_vars[name]!r} (global, {type(self.global_vars[name]).__name__})")
            else:
                print(f"Variable '{name}' is not set")

    # ---- Unset: delete a variable ----

    def do_unset(self: OpenMREPLCore, arg: str):
        """
        Delete a shell scripting variable.
        Usage: unset <name>
        Example: unset count
        """
        name = arg.strip()
        if not name:
            print("Usage: unset <name>")
            return
        variables = self.variables
        if name in variables:
            del variables[name]
            print(f"Variable '{name}' deleted")
        else:
            print(f"Variable '{name}' is not set")


    def do_var(self: OpenMREPLCore, arg: str):
        """
        Create a shell scripting variable with explicit 'var' keyword.
        Supports space-friendly syntax: var name = value
        
        Also supports global variables: var -g name = value
        Global variables persist across macro playback.
        
        Usage: var <name> = <value>
               var -g <name> = <value>
        Example: var count = 42
                 var growth_rate = 1.15
                 var items = Jan Feb Mar
                 var name = "hello world"
                 var -g theme_color = #3B82F6
        """
        arg = arg.strip()
        if not arg:
            print("Usage: var <name> = <value>")
            return

        # Check for global flag: var -g name = value
        is_global = False
        if arg.startswith("-g "):
            is_global = True
            arg = arg[3:].strip()

        # Parse: name = value
        match = re.match(r'([a-zA-Z_][a-zA-Z0-9_]*)\s*=\s*(.*)', arg)
        if not match:
            print(f"Usage: var <name> = <value>")
            print(f"Error: Could not parse '{arg}'. Expected 'var name = value'")
            return

        var_name = match.group(1)
        value_str = match.group(2).strip()

        # Phase 6: legacy $var and $(cmd) expansion removed.
        # Normal var assignments are handled by ScriptLineExecutor.
        # This fallback only handles malformed input.
        parsed = self._parse_assignment_value(value_str)

        if is_global:
            self.global_vars[var_name] = parsed
            if isinstance(parsed, list):
                print(f"Global variable '{var_name}' set ({len(parsed)} items)")
            else:
                print(f"Global variable '{var_name}' = {parsed!r}")
        else:
            self.variables[var_name] = parsed
            if isinstance(parsed, list):
                print(f"Variable '{var_name}' set ({len(parsed)} items)")
            else:
                print(f"Variable '{var_name}' = {parsed!r}")

    # ---- Record: macro recording control ----

    def do_record(self: OpenMREPLCore, arg: str):
        """
        Control macro recording.
        Usage: record start <name> [description]            # Start recording (unexpanded)
               record start --expand <name> [description]    # Start recording (expanded)
               record stop                                   # Stop recording
        Example: record start format_sel "Format selection"
                 record start --expand report_q4 "Q4 report"
                 record stop
        """
        arg = arg.strip()
        if not arg:
            print("Usage: record start [--expand] <name> [description]")
            print("       record stop")
            return

        from lib_utils.macro_recorder import get_recorder
        recorder = get_recorder()

        if arg.startswith("start"):
            # Parse: start [--expand] <name> [description]
            parts = arg[5:].strip().split(None, 1)
            expand = False

            # Check for --expand flag
            if parts and parts[0] == "--expand":
                expand = True
                parts = parts[1:] if len(parts) > 1 else []

            if not parts:
                print("Usage: record start [--expand] <name> [description]")
                return

            name = parts[0]
            description = parts[1] if len(parts) > 1 else ""

            if recorder.start_recording(name, description, expand=expand):
                mode = "expanded (static)" if expand else "unexpanded (dynamic)"
                print(f"Recording started: '{name}' ({mode})")
            else:
                print("Already recording. Use 'record stop' first.")

        elif arg == "stop":
            macro = recorder.stop_recording()
            if macro:
                print(f"Recording stopped: '{macro.name}' ({len(macro.commands)} commands)")
            else:
                print("Not currently recording.")

        else:
            print(f"Usage: record start [--expand] <name> [description]")
            print(f"       record stop")

    # ---- Play: macro playback ----

    def do_play(self: OpenMREPLCore, arg: str):
        """
        Play back a recorded macro.
        Usage: play <name>
               play <name> --preserve-vars
        Example: play format_sel
                 play report_q4 --preserve-vars
        """
        arg = arg.strip()
        if not arg:
            print("Usage: play <name> [--preserve-vars]")
            return

        # Parse --preserve-vars flag
        preserve_vars = False
        if "--preserve-vars" in arg:
            preserve_vars = True
            arg = arg.replace("--preserve-vars", "").strip()

        if not arg:
            print("Usage: play <name> [--preserve-vars]")
            return

        from lib_utils.macro_recorder import get_recorder
        recorder = get_recorder()
        errors = recorder.play_macro(arg, self, preserve_vars=preserve_vars)

        if errors:
            print(f"Macro playback had {len(errors)} error(s):")
            for err in errors:
                print(f"  - {err}")
        else:
            print(f"Macro '{arg}' played back successfully")

    # ---- OneCmd: override for variable handling and macro recording ----

    def onecmd(self: OpenMREPLCore, line: str):
        """Delegate to ScriptLineExecutor for shared script-line semantics."""
        from lib_scripting.line_executor import ScriptLineExecutor
        return ScriptLineExecutor(self).execute_line(line)
