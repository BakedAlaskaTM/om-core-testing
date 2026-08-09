"""
REPL Help System - Handbook, cheatsheet, topic lookup.

Commands for accessing documentation.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib_repl.repl_core import OpenMREPLCore


class REPLHelpMixin:
    """Mixin for help system operations."""

    def complete_help(self, text, line, begidx, endidx):
        """Tab-complete the argument to 'help' with all available command names."""
        # Gather REPL do_* commands and registered command IDs
        names = self.get_names()
        cmds = [n[3:] for n in names if n.startswith('do_')]
        registry = getattr(self, 'registry', None)
        if registry is not None:
            cmds.extend(registry.get_all().keys())
        # Deduplicate while preserving order
        seen = set()
        uniq = []
        for c in cmds:
            if c not in seen:
                seen.add(c)
                uniq.append(c)
        return [c for c in uniq if c.startswith(text)]

    def _handbook_docs_path(self) -> "pathlib.Path":
        import pathlib
        # Project root is two levels up from lib_scripting/
        return pathlib.Path(__file__).resolve().parent.parent / "docs" / "c-handbook"

    def _handbook_files(self):
        """Yield (relative_path, full_path) for every .md in docs/c-handbook."""
        root = self._handbook_docs_path()
        if not root.exists():
            return
        for p in sorted(root.rglob("*.md")):
            rel = p.relative_to(root)
            yield (str(rel), p)

    def do_handbook(self: OpenMREPLCore, arg: str):
        """
        OpenM Handbook viewer.
        Usage: handbook [doc|search term]
        Examples:
            handbook              # List all documents
            handbook graph        # Show/search documents matching 'graph'
            handbook c-01         # Show doc c-01-graph-primitives
        """
        docs = list(self._handbook_files())
        if not docs:
            print("Handbook directory not found.")
            return

        if not arg:
            # List all documents
            print("\n📚 OpenM Handbook:")
            print("=" * 60)
            print("  Main documents:")
            for rel, path in docs:
                if "/" not in rel:
                    print(f"    {rel}")
            print("\n  Drafts:")
            for rel, path in docs:
                if "/" in rel:
                    print(f"    {rel}")
            print("\nType 'handbook <name>' to read a specific doc")
            print("Type 'handbook <topic>' to search contents")
            return

        # Try exact match first
        arg_lower = arg.lower()
        for rel, path in docs:
            if arg_lower in rel.lower():
                print(f"\n📖 {rel}")
                print("=" * 60)
                try:
                    content = path.read_text(encoding="utf-8")
                    print(content[:3000])
                    if len(content) > 3000:
                        print("\n... (truncated, use 'handbook <exact_name>' for full text)")
                except Exception as e:
                    print(f"Error reading file: {e}")
                return

        # Content search across all docs
        matches = []
        for rel, path in docs:
            try:
                content = path.read_text(encoding="utf-8")
                if arg_lower in content.lower():
                    # Find first occurrence line
                    for i, line in enumerate(content.splitlines(), 1):
                        if arg_lower in line.lower():
                            matches.append((rel, i, line.strip()[:100]))
                            break
            except Exception:
                pass

        if matches:
            print(f"\n🔍 Search results for '{arg}':")
            print("=" * 60)
            for rel, line_no, excerpt in matches[:10]:
                print(f"\n  {rel} (line {line_no})")
                print(f"    {excerpt}...")
            if len(matches) > 10:
                print(f"\n... and {len(matches) - 10} more results")
        else:
            print(f"No results found for '{arg}'")
            print("Try: handbook graph | handbook formula | handbook c-01")

    def do_cheatsheet(self: OpenMREPLCore, arg: str):
        """Show quick reference cheatsheet."""
        print(self.help_system.get_cheatsheet())

    def do_topic(self: OpenMREPLCore, arg: str):
        """
        Find help for a specific topic.
        Usage: topic <keyword>
        Examples: topic rule, topic this, topic prev
        """
        if not arg:
            print("Usage: topic <keyword>")
            print("Common topics: rule, rule, sequential, this, prev, excel")
            return

        section = self.help_system.find_topic(arg)
        if section:
            print(f"\n📖 {section.title}")
            print("=" * 60)
            print(section.content[:1500])
        else:
            print(f"No topic found for '{arg}'")
            print("Try 'handbook <search>' for full text search")

    def do_man(self: OpenMREPLCore, arg: str):
        """Alias for handbook."""
        return self.do_handbook(arg)

    def do_docs(self: OpenMREPLCore, arg: str):
        """Alias for handbook."""
        return self.do_handbook(arg)

    def do_ref(self: OpenMREPLCore, arg: str):
        """Alias for cheatsheet."""
        return self.do_cheatsheet(arg)

    def help_handbook(self: OpenMREPLCore):
        print("\nhandbook [part|search]")
        print("  View OpenM Handbook documentation.")
        print("  Examples:")
        print("    handbook           # List all parts")
        print("    handbook 01        # Read Part 01 (Rules)")
        print("    handbook 05        # Read Part 05 (Sequential)")
        print("    handbook excel     # Search for Excel comparison")

    def help_cheatsheet(self: OpenMREPLCore):
        print("\ncheatsheet")
        print("  Show quick reference for rules and commands.")

    def help_topic(self: OpenMREPLCore):
        print("\ntopic <keyword>")
        print("  Get help on a specific topic.")
        print("  Common: rule, rule, sequential, this, prev, next, excel")

    def do_help(self: OpenMREPLCore, arg: str):
        """Override cmd.Cmd.do_help to support sigil topics and registered commands."""
        if arg == "$":
            self._help_dollar()
        elif arg == "@":
            self._help_at()
        elif arg == "%":
            self._help_percent()
        elif arg == "{" or arg == "{{":
            self._help_brace()
        elif arg == "var" or arg == "set":
            self._help_var()
        elif not arg:
            # Default help listing — show REPL commands, registered commands, then custom topics
            super().do_help(arg)
            # Append registered commands
            registry = getattr(self, 'registry', None)
            if registry is not None:
                all_reg = sorted(registry.get_all().values(), key=lambda c: c.id)
                if all_reg:
                    print()
                    print("Registered commands (type 'help <command_id>'):")
                    for cmd in all_reg:
                        name = f"  {cmd.id:<25} {cmd.name}"
                        if cmd.shortcut:
                            name += f"  ({cmd.shortcut})"
                        print(name)
            print()
            print("Special topics (type 'help <topic>'):")
            print("  var  — variable assignment")
            print("  $    — anchored / absolute target")
            print("  @    — technical channel")
            print("  %    — hidden / system name")
            print("  {    — macro placeholder")
            print("  exec — explicit command execution")
        else:
            # Check for registered command help
            registry = getattr(self, 'registry', None)
            if registry is not None:
                cmd_def = registry.get(arg)
                if cmd_def:
                    print(f"\nCommand: {cmd_def.id}")
                    print(f"  Name: {cmd_def.name}")
                    print(f"  Category: {cmd_def.category.name}")
                    print(f"  Shortcut: {cmd_def.shortcut or 'None'}")
                    print(f"  Needs Context: {cmd_def.needs_context}")
                    if cmd_def.description:
                        print(f"  Description: {cmd_def.description}")
                    if cmd_def.params:
                        print(f"  Parameters:")
                        for name, typ in cmd_def.params.items():
                            print(f"    - {name}: {typ.__name__}")
                    return
            # Try multi-word help topics (e.g. "solver run" → help_solver_run)
            help_func_name = 'help_' + arg.replace(' ', '_')
            help_func = getattr(self, help_func_name, None)
            if help_func is not None:
                help_func()
                return
            super().do_help(arg)

    def _help_dollar(self: OpenMREPLCore):
        """Print help for $target sigil."""
        print("""
$ — Anchored / Absolute Target
==============================

  $target
      Anchored rule target.
      do_rule strips $ and sets is_anchored=True.

  Example:
      $Cube::Products.Widgets:Years.2024

  $ is ONLY for rule anchoring. It is NOT a variable prefix.
  Use {{name}} for variable expansion everywhere.
""")

    def _help_at(self: OpenMREPLCore):
        """Print help for @.channel sigil."""
        print("""
@ — Technical Channel
=====================

  @.channel
      Meta or technical channel on a cell.
      Used for formatting, annotations, and computed properties.

  Examples:
      @.font_color
      @.bold
      @.italic
""")

    def _help_percent(self: OpenMREPLCore):
        """Print help for %name sigil."""
        print("""
% — Hidden / System / Internal Name
====================================

  %name
      Prefix for system-level identifiers.
      Used for system tables, dimensions, cubes, and views
      that are not part of the user-visible model.

  Examples:
      %REC    — system record table
      %TYP    — system type registry
      %CFG    — system configuration view
""")

    def _help_brace(self: OpenMREPLCore):
        """Print help for {{name}} macro placeholder."""
        print("""
{ — Macro Placeholder
====================

  {{name}}
      Variable / command placeholder.
      Expanded by looking up the name in context variables.
      Works everywhere: macro files, exec, and the REPL prompt.

  At the REPL prompt:
      var color = "#FF0000"
      echo {{color}}
      # → "#FF0000"

  In macro files:
      rule {{selection}}:@.font_color = "{{widget_value}}"

  In exec:
      exec {{cmd}}
      # if variables['cmd'] = 'rule $Cube::A.B:C.D = "red"'
      # → exec rule $Cube::A.B:C.D = "red"

  Examples:
      {{selection}}    → $Cube::Products.Widgets:Years.2024
      {{widget_value}} → "#FF0000"
      {{cmd}}          → rule $Cube::... = "#FF0000"
""")

    def _help_var(self: OpenMREPLCore):
        """Print help for variable assignment."""
        print("""
var (alias: set) — Variable Assignment
=======================================

  var name = value
  set name = value
      Set a variable. Both forms are identical.

  var -g name = value
  set -g name = value
      Set a global variable.

  Examples:
      var color = "#FF0000"
      set color = "#FF0000"
      var count = 42
      var items = [1, 2, 3]
      var -g config = "debug"

  Access with {{name}}:
      echo {{color}}
      # → "#FF0000"
""")

