"""
REPL File Operations - Load, save, source, import.

Commands for file I/O and batch processing.
"""

from __future__ import annotations

import glob
import shlex
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib_repl.repl_core import OpenMREPLCore


class REPLFileMixin:
    """Mixin for file I/O operations."""

    def do_load(self: OpenMREPLCore, arg: str):
        """
        Load data, macros, or models from file.
        Usage: load <type> <filepath> [options]

        Types:
          macro <filepath> [--play]  - Load macro commands
          model <filepath>           - Load workspace/model
          data <filepath>            - Import data (Excel/CSV)

        Examples:
          load macro my_macro.json
          load macro ~/macros/format.json --play
          load model ~/models/finance.openm
          load data ~/data/sales.xlsx
        """
        if not arg:
            print(self.do_load.__doc__)
            return

        parts = arg.split(maxsplit=1)
        if len(parts) < 2:
            print("Error: Usage: load <type> <filepath>")
            return

        load_type, rest = parts[0], parts[1]

        if load_type == "macro":
            self._load_macro(rest)
        elif load_type == "model":
            self._load_model(rest)
        elif load_type == "data":
            self._load_data(rest)
        else:
            print(f"Unknown type: {load_type}")
            print(f"Supported: macro, model, data")

    def complete_load(self: OpenMREPLCore, text: str, line: str, begidx: int, endidx: int):
        """Tab completion for load command."""
        import os
        from pathlib import Path

        parts = line[:endidx].split()

        if len(parts) <= 1 or (len(parts) == 2 and not line.endswith(' ')):
            types = ['macro', 'model', 'data']
            if text:
                return [t for t in types if t.startswith(text)]
            return types

        if len(parts) >= 2:
            if line.endswith(' '):
                partial = ''
            else:
                partial = parts[-1] if len(parts) > 1 else ''

            if partial.startswith('~'):
                partial = os.path.expanduser(partial)

            if not partial:
                pattern = '*'
            elif partial == '.' or partial == './':
                pattern = './*'
            elif partial.endswith('/'):
                pattern = partial + '*'
            else:
                if os.path.isdir(partial):
                    pattern = partial + '/*'
                else:
                    pattern = partial + '*'

            try:
                matches = glob.glob(pattern)
                matches = [m for m in matches if m not in ('.', './')]
                results = []
                for m in matches:
                    if m.startswith('./'):
                        m = m[2:]
                    if os.path.isdir(m) and not m.endswith('/'):
                        results.append(m + '/')
                    else:
                        results.append(m)
                return results
            except Exception:
                return []

        return []

    def _load_macro(self: OpenMREPLCore, arg: str):
        """Load macro from file."""
        parts = arg.split()
        filepath = parts[0]
        play_immediately = '--play' in parts

        from lib_utils.macro_recorder import Macro, MacroRecorder

        path = Path(filepath).expanduser()
        if not path.exists():
            print(f"File not found: {path}")
            return

        try:
            import json
            with open(path) as f:
                data = json.load(f)

            macro = Macro.from_dict(data)
            recorder = MacroRecorder()
            recorder._save_macro(macro)

            print(f"Loaded macro '{macro.name}' ({len(macro.commands)} commands)")

            if play_immediately:
                from lib_utils.macro_recorder import get_recorder
                errors = get_recorder().play_macro(macro.name, self)
                if errors:
                    for err in errors:
                        print(f"  {err}")
                else:
                    print(f"Macro '{macro.name}' executed")

        except Exception as e:
            print(f"Error loading macro: {e}")

    def _load_model(self: OpenMREPLCore, arg: str):
        """Load workspace/model from file.

        Phase 5: Uses canonical load_workspace command ID.
        REPL method: _load_model()
        Bus command: "load_workspace"
        Events: command.load_workspace.before / command.load_workspace.succeeded / command.load_workspace.failed
        """
        path = Path(arg.split()[0]).expanduser()

        if hasattr(self, 'gui_port') and self.gui_port:
            if not self.gui_port.confirm_discard_unsaved_changes():
                print("Load cancelled - unsaved changes")
                return
            success = self.gui_port.open_file(str(path))
            if success:
                print(f"Loaded model from {path}")
                self.workspace = self.gui_port.get_workspace()
            else:
                print(f"Failed to load model from {path}")
        else:
            if not path.exists():
                print(f"File not found: {path}")
                return
            try:
                result = self.session.execute("load_workspace", path=str(path))
                if result.success:
                    print(f"Loaded model from {path} (bus-driven)")
                else:
                    print(f"Error loading model: {result.error}")
            except Exception as e:
                print(f"Error loading model: {e}")

    def _load_data(self: OpenMREPLCore, arg: str):
        """Import data from file."""
        path = Path(arg.split()[0]).expanduser()

        if not path.exists():
            print(f"File not found: {path}")
            return

        ext = path.suffix.lower()
        try:
            if ext in ('.xlsx', '.xls'):
                try:
                    result = self.session.execute("run_excel_import", path=str(path))
                    if result.success:
                        data = result.data
                        print(f"Imported {data.get('values_loaded', 0)} values from {path.name}")
                        if data.get('warnings'):
                            print(f"Warnings: {'; '.join(data['warnings'])}")
                    else:
                        print(f"Import failed: {result.error}")
                except Exception as e:
                    print(f"Import failed: {e}")
            elif ext == '.csv':
                import pandas as pd
                df = pd.read_csv(path)
                print(f"Loaded CSV: {len(df)} rows, {len(df.columns)} columns")
                print(f"  Columns: {', '.join(df.columns[:5])}")
            else:
                print(f"Unsupported format: {ext}")
        except Exception as e:
            print(f"Error loading data: {e}")

    def do_save(self: OpenMREPLCore, arg: str):
        """
        Save data, macros, or models to file.
        Usage: save <filepath>            - Save workspace to file (auto-detect)
               save model <filepath>       - Save workspace/model
               save macro <name> [filepath] - Save macro by name to file

        Examples:
          save model_20260503.json
          save model ~/models/finance.openm
          save macro format_blue ~/format_blue.json
        """
        if not arg:
            print(self.do_save.__doc__)
            return

        parts = arg.split(maxsplit=1)

        if parts[0] in ("macro", "model"):
            if len(parts) < 2:
                print(f"Error: Usage: save {parts[0]} <filepath>")
                return
            save_type, rest = parts[0], parts[1]

            if save_type == "macro":
                self._save_macro(rest)
            elif save_type == "model":
                self._save_model(rest)
        else:
            self._save_model(arg)

    def _save_model(self: OpenMREPLCore, arg: str):
        """Save workspace/model to file.

        Phase 5: Uses canonical save_workspace command ID.
        REPL method: _save_model()
        Bus command: "save_workspace"
        Events: command.save_workspace.before / command.save_workspace.succeeded / command.save_workspace.failed
        """
        path = Path(arg.split()[0]).expanduser()

        if not path.is_absolute():
            path = Path.cwd() / path
        path = path.resolve()

        try:
            result = self.session.execute("save_workspace", path=str(path))
            if result.success:
                print(f"Saved model to: {path}")
            else:
                print(f"Error saving model: {result.error}")
        except Exception as e:
            print(f"Error saving model: {e}")
            import traceback
            traceback.print_exc()

    def _save_macro(self: OpenMREPLCore, arg: str):
        """Save macro to file."""
        parts = arg.split()
        if not parts:
            print("Error: Usage: save macro <name> [filepath]")
            return

        macro_name = parts[0]
        custom_path = parts[1] if len(parts) > 1 else None

        from lib_utils.macro_recorder import get_recorder
        recorder = get_recorder()

        macro = recorder.load_macro(macro_name)
        if not macro:
            print(f"Macro '{macro_name}' not found")
            return

        try:
            if custom_path:
                path = Path(custom_path).expanduser()
            else:
                from lib_utils.paths import OM_EXPORTS_DIR
                path = OM_EXPORTS_DIR / f"{macro_name}.json"
                path.parent.mkdir(parents=True, exist_ok=True)

            with open(path, "w") as f:
                import json
                json.dump(macro.to_dict(), f, indent=2)

            print(f"Saved macro '{macro_name}' to {path}")
        except Exception as e:
            print(f"Error saving macro: {e}")


    def do_source(self: OpenMREPLCore, arg: str):
        """
        Execute commands from a file (without saving to history).
        Usage: source <filename>
        Example: source test_scripts/01_basic_variables.openm
        
        Commands executed via source are NOT saved to command history.
        This allows you to load and run scripts without polluting your REPL history.
        """
        try:
            import readline
        except ImportError:
            readline = None
        
        if not arg:
            print("Error: No filename specified. Usage: source <filename>")
            return

        # Track source call stack so nested source paths resolve relative to the
        # script that contains them, and circular references are rejected.
        if not hasattr(self, "_source_stack"):
            self._source_stack: list[str] = []

        filepath = Path(arg).expanduser()
        if not filepath.is_absolute():
            if self._source_stack:
                base_dir = Path(self._source_stack[-1]).parent
            else:
                base_dir = Path.cwd()
            filepath = base_dir / filepath

        filepath = filepath.resolve()
        if not filepath.exists():
            print(f"Error: File not found: {filepath}")
            return

        source_path = str(filepath)
        if source_path in self._source_stack:
            print(f"Error: Circular source detected: {filepath.name}")
            return

        # Disable history writes during source (same approach as macro playback)
        # In remote mode, skip_history is a local concern only; readline is not shared.
        _orig_add_history = None
        if readline is not None:
            _orig_add_history = readline.add_history
            readline.add_history = lambda *a, **kw: None  # no-op
        ctx = None
        try:
            ctx = self.session.context
            ctx.skip_history = True
        except (AttributeError, RuntimeError):
            pass  # Remote session or no context

        self._source_stack.append(source_path)

        # Suspend UI refresh for the duration of the source command so that
        # thousands of bus events (one per line) don't each trigger a separate
        # browser rebuild + view refresh.  A single coalesced refresh fires
        # when we resume.
        gui_win = getattr(self, "gui_window", None)
        if gui_win is not None and hasattr(gui_win, "suspend_ui_refresh"):
            gui_win.suspend_ui_refresh()

        try:
            with open(filepath, 'r') as f:
                lines = f.readlines()

            print(f"Sourcing {filepath} ({len(lines)} lines)...")
            executed = 0
            errors = []
            rule_batch: list[dict] = []
            hardvalue_batches: dict[str, list[dict]] = {}
            dim_item_batch: list[dict] = []
            hval_batch: list[tuple[str, list[str], object]] = []
            _hval_resolve_cache: dict[str, dict] = {}

            def _flush_rule_batch() -> None:
                if not rule_batch:
                    return
                try:
                    result = self.session.execute("apply_rule_batch", rules=rule_batch)
                    if result.success:
                        print(f"Applied {len(rule_batch)} rule(s)")
                    else:
                        raise Exception(result.error or "Rule batch failed")
                except Exception as e:
                    errors.append((line_num, str(e), f"apply_rule_batch ({len(rule_batch)} rules)"))
                    raise
                finally:
                    rule_batch.clear()

            _HVAL_CHUNK_SIZE = 500

            def _flush_hardvalue_batches() -> None:
                if not hardvalue_batches:
                    return
                for cid, entries in hardvalue_batches.items():
                    if not entries:
                        continue
                    for i in range(0, len(entries), _HVAL_CHUNK_SIZE):
                        chunk = entries[i:i + _HVAL_CHUNK_SIZE]
                        try:
                            result = self.session.execute(
                                "set_cell_hardvalues_batch_by_addr",
                                cube_id=cid, entries=chunk,
                            )
                            if not result.success:
                                raise Exception(result.error or "Batch hardvalue failed")
                        except Exception as e:
                            errors.append((line_num, str(e), f"set_cell_hardvalues_batch_by_addr ({len(chunk)} cells)"))
                            raise
                hardvalue_batches.clear()

            def _flush_dim_item_batch() -> None:
                if not dim_item_batch:
                    return
                try:
                    result = self.session.execute(
                        "create_dimension_items_batch",
                        entries=dim_item_batch,
                    )
                    if not result.success:
                        raise Exception(result.error or "Batch dim item creation failed")
                except Exception as e:
                    errors.append((line_num, str(e), f"create_dimension_items_batch ({len(dim_item_batch)} items)"))
                    raise
                finally:
                    dim_item_batch.clear()

            def _try_batch_dim_item(line_str: str) -> bool:
                """If line is `exec create_dimension_item ...` or `exec add_dimension_item ...`, queue it."""
                parts = line_str.split(None, 2)
                if len(parts) < 2:
                    return False
                if parts[0].lower() != "exec":
                    return False
                cmd_name = parts[1].lower()
                if cmd_name not in ("create_dimension_item", "add_dimension_item"):
                    return False
                try:
                    args = shlex.split(line_str[len("exec"):].strip())
                except ValueError:
                    return False
                if not args or args[0].lower() not in ("create_dimension_item", "add_dimension_item"):
                    return False
                params = {}
                for token in args[1:]:
                    if "=" in token:
                        k, v = token.split("=", 1)
                        params[k] = v
                dim_id = params.get("dim_id")
                name = params.get("name")
                if dim_id is None or name is None:
                    return False
                position = params.get("position", "append")
                dim_item_batch.append({"dim_id": dim_id, "name": name, "position": position})
                return True

            def _flush_hval_batch() -> None:
                if not hval_batch:
                    return
                for cube_name, dim_specs, value in hval_batch:
                    try:
                        if cube_name not in _hval_resolve_cache:
                            cube_id, _ = self._resolve_cube_id(cube_name)
                            if not cube_id:
                                errors.append((line_num, f"Cube not found: {cube_name}", f"hval {cube_name}::..."))
                                continue
                            cube_detail = self.session.query("cube_detail", cube_id=cube_id)
                            if not cube_detail:
                                errors.append((line_num, f"cube_detail query failed for {cube_name}", f"hval {cube_name}::..."))
                                continue
                            dim_data = self.session.query("dimension_list")
                            if not dim_data:
                                errors.append((line_num, "dimension_list query failed", f"hval {cube_name}::..."))
                                continue
                            cube_dim_ids = cube_detail.get("dimension_ids", [])
                            dim_name_to_id: dict[str, str] = {}
                            item_name_to_id: dict[str, dict[str, str]] = {}
                            for d in dim_data.get("dimensions", []):
                                d_id = d.get("id", "")
                                d_name = d.get("name", d_id)
                                dim_name_to_id[d_name] = d_id
                                item_name_to_id[d_id] = {}
                                for item in d.get("item_list", []):
                                    item_name_to_id[d_id][item.get("name", "")] = item.get("id", "")
                            _hval_resolve_cache[cube_name] = {
                                "cube_id": cube_id,
                                "cube_dim_ids": cube_dim_ids,
                                "dim_name_to_id": dim_name_to_id,
                                "item_name_to_id": item_name_to_id,
                            }
                        rc = _hval_resolve_cache[cube_name]
                        cube_id = rc["cube_id"]
                        cube_dim_ids = rc["cube_dim_ids"]
                        dim_name_to_id = rc["dim_name_to_id"]
                        item_name_to_id = rc["item_name_to_id"]
                        resolved: dict[str, str] = {}
                        for spec in dim_specs:
                            spec = spec.strip()
                            if "." not in spec:
                                continue
                            dim_name, item_name = spec.split(".", 1)
                            dim_id = dim_name_to_id.get(dim_name)
                            if dim_id is None:
                                for dn, did in dim_name_to_id.items():
                                    if dn.lower() == dim_name.lower():
                                        dim_id = did
                                        break
                            if dim_id is None:
                                continue
                            item_id = item_name_to_id.get(dim_id, {}).get(item_name)
                            if item_id is None:
                                for iname, iid in item_name_to_id.get(dim_id, {}).items():
                                    if iname.lower() == item_name.lower():
                                        item_id = iid
                                        break
                            if item_id is not None:
                                resolved[dim_id] = item_id
                        from lib_openm.technical_ids import CHANNEL_TO_AT_ID
                        addr_list: list[str] = []
                        for dim_id in cube_dim_ids:
                            if dim_id == "@":
                                addr_list.append(CHANNEL_TO_AT_ID["value"])
                            elif dim_id in resolved:
                                addr_list.append(resolved[dim_id])
                            else:
                                items = item_name_to_id.get(dim_id, {})
                                if items:
                                    addr_list.append(next(iter(items.values())))
                        hardvalue_batches.setdefault(cube_id, []).append({"addr": addr_list, "value": value})
                    except Exception as e:
                        errors.append((line_num, str(e), f"hval {cube_name}::..."))
                hval_batch.clear()

            def _try_batch_hval(line_str: str) -> bool:
                """If line is `hval Cube::Dim.Item:Dim.Item = value` or `hval $Cube::... value=X`, queue it."""
                parts = line_str.split(None, 2)
                if not parts or parts[0].lower() != "hval":
                    return False
                rest = line_str[4:].strip()
                if "::" not in rest:
                    return False
                # Parse value: support both "= value" and "value=value" syntax
                value = None
                addr_part = rest
                # Try "value=" syntax first
                import shlex as _shlex
                try:
                    _tokens = _shlex.split(rest)
                except ValueError:
                    _tokens = rest.split()
                if len(_tokens) >= 2 and _tokens[-1].startswith("value="):
                    value_str = _tokens[-1][len("value="):]
                    addr_part = " ".join(_tokens[:-1])
                elif "=" in rest:
                    addr_part, value_str = rest.rsplit("=", 1)
                    addr_part = addr_part.strip()
                    value_str = value_str.strip()
                else:
                    return False
                if "::" not in addr_part:
                    return False
                cube_name, dims_part = addr_part.split("::", 1)
                cube_name = cube_name.strip().lstrip("$")
                dim_specs = [s.strip() for s in dims_part.split(":") if s.strip()]
                if not dim_specs:
                    return False
                try:
                    value = self._parse_value(value_str)
                except Exception:
                    value = value_str
                hval_batch.append((cube_name, dim_specs, value))
                return True

            def _try_batch_hardvalue(line_str: str) -> bool:
                """If line is `exec set_cell_hardvalue_by_addr ...`, queue it and return True."""
                parts = line_str.split(None, 2)
                if len(parts) < 2:
                    return False
                if parts[0].lower() != "exec":
                    return False
                if parts[1].lower() != "set_cell_hardvalue_by_addr":
                    return False
                try:
                    args = shlex.split(line_str[len("exec"):].strip())
                except ValueError:
                    return False
                if not args or args[0].lower() != "set_cell_hardvalue_by_addr":
                    return False
                params = {}
                for token in args[1:]:
                    if "=" in token:
                        k, v = token.split("=", 1)
                        params[k] = self._parse_value(v)
                cid = params.get("cube_id")
                addr = params.get("addr")
                value = params.get("value")
                if cid is None or addr is None:
                    return False
                if isinstance(addr, str):
                    addr = addr.split()
                hardvalue_batches.setdefault(cid, []).append({"addr": list(addr), "value": value})
                return True

            for line_num, line in enumerate(lines, 1):
                stripped = line.strip()
                if not stripped or stripped.startswith('#'):
                    continue

                try:
                    parts = stripped.split()
                    is_batchable_rule = (
                        parts
                        and parts[0].lower() == "rule"
                        and "=" in stripped
                        and (len(parts) == 1 or parts[1].lower() not in ("delete", "delete-anchored", "set-anchored"))
                    )
                    if is_batchable_rule:
                        _flush_hval_batch()
                        _flush_hardvalue_batches()
                        _flush_dim_item_batch()
                        rule_dict = self.do_rule(stripped[5:].strip(), batch_mode=True)
                        if rule_dict is not None:
                            rule_batch.append(rule_dict)
                            executed += 1
                            continue
                        # Parse failed; do_rule already reported it. Skip executing it again.
                        errors.append((line_num, "Invalid rule command", stripped))
                        continue
                    _flush_rule_batch()
                    if _try_batch_dim_item(stripped):
                        executed += 1
                        continue
                    _flush_dim_item_batch()
                    if _try_batch_hval(stripped):
                        executed += 1
                        continue
                    _flush_hval_batch()
                    if _try_batch_hardvalue(stripped):
                        executed += 1
                        continue
                    _flush_hardvalue_batches()
                    _flush_dim_item_batch()
                    self.onecmd(stripped)
                    executed += 1
                except Exception as e:
                    errors.append((line_num, str(e), stripped))

            try:
                _flush_rule_batch()
            except Exception:
                pass
            try:
                _flush_hval_batch()
            except Exception:
                pass
            try:
                _flush_hardvalue_batches()
            except Exception:
                pass
            try:
                _flush_dim_item_batch()
            except Exception:
                pass

            if errors:
                print(f"Executed {executed} commands, {len(errors)} errors:")
                for line_num, err, line in errors:
                    print(f"  Line {line_num}: {err}")
                    print(f"    {line}")
            else:
                print(f"Executed {executed} commands from {filepath.name}")

        except Exception as e:
            print(f"Error reading file: {e}")
        finally:
            self._source_stack.pop()
            # Resume UI refresh — fires a single coalesced refresh if any
            # events were queued during suspension.
            if gui_win is not None and hasattr(gui_win, "resume_ui_refresh"):
                gui_win.resume_ui_refresh()
            # Restore history writes
            try:
                if ctx is not None:
                    ctx.skip_history = False
            except (AttributeError, RuntimeError):
                pass
            if readline is not None and _orig_add_history is not None:
                readline.add_history = _orig_add_history

    def complete_source(self: OpenMREPLCore, text: str, line: str, begidx: int, endidx: int):
        """Tab completion for source command - file paths."""
        from pathlib import Path

        typed = text

        if typed.startswith('~'):
            typed = str(Path.home()) + typed[1:]

        if '/' in typed:
            if typed.endswith('/'):
                dir_path = typed.rstrip('/')
                if not dir_path:
                    dir_path = '.'
                file_pattern = ''
                prefix = typed
            else:
                dir_path = str(Path(typed).parent) if typed else '.'
                file_pattern = str(Path(typed).name)
                prefix = str(Path(typed).parent) + '/' if str(Path(typed).parent) != '.' else ''
        else:
            dir_path = '.'
            file_pattern = typed
            prefix = ''

        try:
            results = []
            base_path = Path(dir_path)

            if not file_pattern:
                if base_path.exists():
                    for item in base_path.iterdir():
                        if item.name.startswith('.'):
                            continue
                        results.append(prefix + item.name)
                        if len(results) >= 1000:
                            break
            else:
                for match in base_path.glob(file_pattern + '*'):
                    if match.name.startswith('.'):
                        continue
                    results.append(prefix + match.name)
                    if len(results) >= 1000:
                        break

            results.sort(key=lambda x: x.lower())
            return results
        except Exception:
            return []