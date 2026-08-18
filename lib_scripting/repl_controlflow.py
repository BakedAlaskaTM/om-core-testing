"""
Control flow mixin for OpenM REPL - implements if/else/elseif/end statements.
"""
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib_repl.repl_core import OpenMREPLCore


class REPLControlFlowMixin:
    """Mixin for control flow operations (if/else/elseif/end)."""

    def do_if(self: "OpenMREPLCore", arg: str):
        """
        Conditional execution with if/then/else/elseif/end.
        
        Usage:
            if condition then command end
            if condition then commands else commands end
            if condition then commands elseif condition then commands else commands end
        
        Examples:
            if Revenue > 1000 then echo "High revenue" end
            if Quarter == "Q1" then set Target 100 else set Target 200 end
            if A > B then echo "A wins" elseif A == B then echo "Tie" else echo "B wins" end
        
        Operators: ==, !=, <, >, <=, >=
        Boolean: and, or, not
        
        Note: For multi-line blocks, use 'source' command with a script file,
        or use the 'run' command for full script files.
        """
        if not arg.strip():
            print(self.do_if.__doc__)
            return
            
        # For simple one-line if statements
        spm = self.script_parser_module
        if spm is None:
            print("Error: script parser not available")
            return

        # Reconstruct the full if statement from the argument
        script_text = f"if {arg}"

        try:
            # Parse the script
            lexer = spm.ScriptLexer(script_text)
            tokens = lexer.tokenize()
            parser = spm.ScriptParser(tokens)
            statements = parser.parse()

            if not statements:
                print("Error: Could not parse if statement")
                return

            # Build context from workspace
            context = self._build_script_context()

            # Execute
            def executor(cmd_name: str, args: str):
                full_cmd = f"{cmd_name} {args}".strip()
                self.onecmd(full_cmd)

            errors = spm.execute_script(statements, executor, context)
            
            for error in errors:
                print(f"[IF-ERROR] {error}")
                
        except SyntaxError as e:
            print(f"Syntax error: {e}")
        except Exception as e:
            print(f"Error executing if statement: {e}")
            
    def _build_script_context(self: "OpenMREPLCore") -> dict:
        """Build execution context for script conditions."""
        context = {}
        
        # Try to get variables from workspace
        ws = getattr(self, '_workspace', None)
        if ws:
            # Add cube values, dimensions, etc.
            for cube in ws.get_all_cubes():
                context[cube.name] = cube
                
        # Add environment variables
        import os
        for key, value in os.environ.items():
            if key.startswith('OPENM_'):
                context[key] = value
                
        return context
        
    def do_run(self: "OpenMREPLCore", arg: str):
        """
        Execute an OpenM script file with control flow support.
        
        Usage: run <filename>
        
        Example:
            run myscript.openm
            
        Script files support:
            - All REPL commands
            - if/then/else/elseif/end blocks
            - Variables and conditions
        """
        if not arg.strip():
            print("Usage: run <filename>")
            return
            
        from pathlib import Path

        spm = self.script_parser_module
        if spm is None:
            print("Error: script parser not available")
            return

        script_arg = arg.strip()
        if (
            len(script_arg) >= 2
            and script_arg[0] == script_arg[-1]
            and script_arg[0] in {'"', "'"}
        ):
            script_arg = script_arg[1:-1]
        filepath = Path(script_arg)
        if not filepath.exists():
            print(f"File not found: {filepath}")
            return

        try:
            with open(filepath) as f:
                script_text = f.read()

            statements = spm.parse_script(script_text)
            context = self._build_script_context()

            def executor(cmd_name: str, args: str):
                full_cmd = f"{cmd_name} {args}".strip()
                self.onecmd(full_cmd)

            errors = spm.execute_script(statements, executor, context)
            
            if errors:
                print(f"\n{len(errors)} error(s) during execution:")
                for error in errors:
                    print(f"  - {error}")
            else:
                print(f"Script executed successfully: {filepath}")
                
        except SyntaxError as e:
            print(f"Syntax error in script: {e}")
        except Exception as e:
            print(f"Error running script: {e}")
