"""
REPL Model Operations - Cube, dimension, view, rule creation.

Commands for building the model structure.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from lib_repl.repl_core import OpenMREPLCore


def _split_dim_items(items: list[str]) -> list[str]:
    """Split dim item tokens by whitespace/commas and normalize integer floats.

    Treats every item label as a string. Integer-valued floats such as
    '2026.0' are normalized to '2026' so template-expanded lists keep
    clean labels.
    """
    labels: list[str] = []
    for token in items:
        for part in token.replace(",", " ").split():
            part = part.strip()
            if not part:
                continue
            try:
                num = float(part)
                if num.is_integer():
                    part = str(int(num))
            except ValueError:
                pass
            labels.append(part)
    return labels


def _parse_role_syntax(spec: str) -> tuple[str, dict[str, list[str]]]:
    """Parse 'Cube rows: A B cols: C page: D E' into (cube_name, layout).

    Raises ValueError on malformed input or repeated role clauses.
    """
    parts = spec.split()
    if not parts:
        raise ValueError("Empty view spec after cube name")
    cube_name = parts[0]
    layout: dict[str, list[str]] = {"rows": [], "cols": [], "page": []}
    current_role: str | None = None
    role_counts: dict[str, int] = {"rows": 0, "cols": 0, "page": 0}
    for token in parts[1:]:
        if token in ("rows:", "cols:", "page:"):
            role = token.rstrip(":")
            role_counts[role] += 1
            if role_counts[role] > 1:
                raise ValueError(f"Role '{role}' appears more than once")
            current_role = role
        elif current_role is not None:
            layout[current_role].append(token)
        else:
            raise ValueError(f"Unexpected token before role marker: {token}")
    return cube_name, layout


class REPLModelMixin:
    """Mixin for model creation and rule operations."""

    def do_use(self: OpenMREPLCore, arg: str):
        """
        Set the default cube context for subsequent commands.
        Usage: use <cube_name>
        Example: use Sales
        
        Phase A: Refactored to use bus event instead of direct engine access.
        REPL method: do_use()
        Bus command: "set" with target="session:active_cube"
        Events: command.set.before / command.set.succeeded / command.set.failed
        """
        cube_name = arg.strip()
        if not cube_name:
            print("Usage: use <cube_name>")
            return
        
        cube_id, resolved_name = self._resolve_cube_id(cube_name)
        if cube_id is None and resolved_name is None:
            print("Error: Could not list cubes (timeout or disconnect)")
            return
        if not cube_id:
            data = self.session.query("cube_list")
            available = [c.get("name", "") for c in data.get("cubes", [])] if data else []
            print(f"Error: Cube '{cube_name}' not found")
            if available:
                print(f"Available cubes: {', '.join(available)}")
            return

        # Set session context directly (session state, not engine state)
        ctx = getattr(self.session, "context", None)
        if ctx is None and hasattr(self.session, "require_context"):
            ctx = self.session.require_context()
        if ctx is not None:
            if not hasattr(ctx, "variables"):
                ctx.variables = {}
            ctx.variables["_current_cube"] = cube_id
        print(f"Current cube: {resolved_name or cube_name}")

    def do_cube(self: OpenMREPLCore, arg: str):
        """
        Cube sub-command dispatcher.

        Usage:
          cube <name> [dim1 dim2 ...]               # Create (backward-compatible)
          cube create <name> [dim1 dim2 ...]
          cube delete <name>
          cube attach <cube_name> <dim_name>
          cube detach <cube_name> <dim_name>

        Expands to canonical command IDs and calls session.execute().
        """
        if not arg:
            print("Usage: cube create <name> [dim1 dim2 ...]")
            print("       cube delete <name>")
            print("       cube attach <cube_name> <dim_name>")
            print("       cube detach <cube_name> <dim_name>")
            return

        parts = arg.split()
        sub = parts[0].lower()
        rest = parts[1:]

        # Backward-compatible: bare cube name means create
        if sub not in ("create", "delete", "attach", "detach"):
            name = parts[0]
            dim_ids = parts[1:]
            try:
                result = self.session.execute("create_cube", name=name, dimension_ids=dim_ids)
                if result.success:
                    cube_id = result.data.get('id', name)
                    print(f"Created cube: {cube_id} ({len(dim_ids)} dimensions)")
                else:
                    print(f"Error creating cube: {result.error}")
            except Exception as e:
                print(f"Error creating cube: {e}")
            return

        if sub == "create":
            if not rest:
                print("Usage: cube create <name> [dim1 dim2 ...]")
                return
            name = rest[0]
            dim_ids = rest[1:]
            try:
                result = self.session.execute("create_cube", name=name, dimension_ids=dim_ids)
                if result.success:
                    cube_id = result.data.get('id', name)
                    print(f"Created cube: {cube_id} ({len(dim_ids)} dimensions)")
                else:
                    print(f"Error creating cube: {result.error}")
            except Exception as e:
                print(f"Error creating cube: {e}")

        elif sub == "delete":
            if not rest:
                print("Usage: cube delete <name>")
                return
            cube_name = rest[0]
            cube_id, resolved_name = self._resolve_cube_id(cube_name)
            if cube_id is None and resolved_name is None:
                print("Error: Could not list cubes (timeout or disconnect)")
                return
            if not cube_id:
                print(f"Error: Cube '{cube_name}' not found")
                return
            try:
                result = self.session.execute("delete_cube", cube_id=cube_id)
                if result.success:
                    print(f"Deleted cube: {resolved_name or cube_name}")
                else:
                    print(f"Error deleting cube: {result.error}")
            except Exception as e:
                print(f"Error deleting cube: {e}")

        elif sub in ("attach", "detach"):
            if len(rest) < 2:
                print(f"Usage: cube {sub} <cube_name> <dim_name>")
                return
            cube_name, dim_name = rest[0], rest[1]
            cube_id, resolved_name = self._resolve_cube_id(cube_name)
            if cube_id is None and resolved_name is None:
                print("Error: Could not list cubes (timeout or disconnect)")
                return
            if not cube_id:
                print(f"Error: Cube '{cube_name}' not found")
                return
            dim_id, resolved_dim_name = self._resolve_dimension_id(dim_name)
            if dim_id is None and resolved_dim_name is None:
                print("Error: Could not list dimensions (timeout or disconnect)")
                return
            if not dim_id:
                print(f"Error: Dimension '{dim_name}' not found")
                return
            cmd = "attach_dimension_to_cube" if sub == "attach" else "detach_dimension_from_cube"
            try:
                result = self.session.execute(cmd, cube_id=cube_id, dim_id=dim_id)
                if result.success:
                    action = "Attached" if sub == "attach" else "Detached"
                    print(f"{action} dimension {dim_name} {sub} cube {cube_name}")
                else:
                    print(f"Error: {result.error}")
            except Exception as e:
                print(f"Error: {e}")

        else:
            print(f"Unknown cube sub-command: {sub}")
            print("Usage: cube create|delete|attach|detach ...")

    def help_cube(self: OpenMREPLCore):
        print("\ncube create <name> [dim1 dim2 ...]")
        print("cube delete <name>")
        print("cube attach <cube_name> <dim_name>")
        print("cube detach <cube_name> <dim_name>")
        print("  Create, delete, attach, or detach cubes. Expands to canonical command IDs.")

    def do_delete_cube(self: OpenMREPLCore, arg: str):
        """
        Delete a cube and all views that reference it.
        Usage: delete_cube <cube_name_or_id>
        Example: delete_cube Sales

        Routes through the command spine: session.execute("delete_cube", ...).
        """
        if not arg:
            print("Usage: delete_cube <cube_name_or_id>")
            return
        cube_name = arg.strip()
        cube_id, resolved_name = self._resolve_cube_id(cube_name)
        if cube_id is None and resolved_name is None:
            print("Error: Could not list cubes (timeout or disconnect)")
            return
        if not cube_id:
            print(f"Error: Cube '{cube_name}' not found")
            return
        try:
            result = self.session.execute("delete_cube", cube_id=cube_id)
            if result.success:
                print(f"Deleted cube: {resolved_name or cube_name}")
            else:
                print(f"Error deleting cube: {result.error}")
        except Exception as e:
            print(f"Error deleting cube: {e}")

    def do_dim(self: OpenMREPLCore, arg: str):
        """
        Create a dimension or add items to an existing dimension.

        Usage:
          dim <id> [item1 item2 ...]              # unordered by default
          dim <id> --set [item1 item2 ...]        # explicit unordered
          dim <id> --seq [item1 item2 ...]         # ordered sequence

        Examples:
          dim Region North South East West
          dim Year --seq Y1 Y2 Y3 Y4 Y5
          dim Quarter --seq Q1 Q2 Q3 Q4

        Phase A: Refactored to use bus event instead of direct engine access.
        REPL method: do_dim()
        Bus command: "create" with type="dimension"
        Events: command.create.before / command.create.succeeded / command.create.failed
        """
        if not arg:
            print("Usage: dim <id> [--set|--seq] item1 item2 ...")
            return
        # Normalize colon to space, then split
        arg_clean = arg.replace(':', ' ').split()
        dim_id = arg_clean[0]
        dim_type = "set"
        items: list[str] = []
        rest = arg_clean[1:]
        if rest:
            if rest[0] == "--seq":
                dim_type = "seq"
                items = _split_dim_items(rest[1:])
            elif rest[0] == "--set":
                dim_type = "set"
                items = _split_dim_items(rest[1:])
            else:
                items = _split_dim_items(rest)

        try:
            result = self.session.execute("create_dimension", name=dim_id, dim_type=dim_type)
            if result.success:
                dim_result_id = result.data.get("id", dim_id)
                created_items = 0
                if items:
                    entries = [{"dim_id": dim_result_id, "name": item, "position": "append"} for item in items]
                    batch_result = self.session.execute(
                        "create_dimension_items_batch", entries=entries
                    )
                    if batch_result.success:
                        created_items = len(entries)
                print(f"Created dimension: {dim_id} ({created_items} items)")
            else:
                print(f"Error creating dimension: {result.error}")
        except Exception as e:
            print(f"Error: {e}")

    def do_dimension(self: OpenMREPLCore, arg: str):
        """
        Dimension sub-command dispatcher.

        Usage:
          dimension create <name> [--set|--seq] [item1 item2 ...]
          dimension rename <old_name> <new_name>
          dimension delete <name>

        Expands to canonical command IDs and calls session.execute().
        """
        if not arg:
            print("Usage: dimension create <name> [--set|--seq] [item1 ...]")
            print("       dimension rename <old_name> <new_name>")
            print("       dimension delete <name>")
            return

        parts = arg.split()
        sub = parts[0].lower()
        rest = parts[1:]

        if sub == "create":
            if not rest:
                print("Usage: dimension create <name> [--set|--seq] [item1 ...]")
                return
            dim_name = rest[0]
            dim_type = "set"
            items = rest[1:]
            if items:
                if items[0] == "--seq":
                    dim_type = "seq"
                    items = _split_dim_items(items[1:])
                elif items[0] == "--set":
                    items = _split_dim_items(items[1:])
                else:
                    items = _split_dim_items(items)
            try:
                result = self.session.execute("create_dimension", name=dim_name, dim_type=dim_type)
                if result.success:
                    dim_result_id = result.data.get("id", dim_name)
                    created_items = 0
                    if items:
                        entries = [{"dim_id": dim_result_id, "name": item, "position": "append"} for item in items]
                        batch_result = self.session.execute(
                            "create_dimension_items_batch", entries=entries
                        )
                        if batch_result.success:
                            created_items = len(entries)
                    print(f"Created dimension: {dim_name} ({created_items} items)")
                else:
                    print(f"Error creating dimension: {result.error}")
            except Exception as e:
                print(f"Error: {e}")

        elif sub == "rename":
            if len(rest) < 2:
                print("Usage: dimension rename <old_name> <new_name>")
                return
            old_name, new_name = rest[0], rest[1]
            dim_id, resolved_dim_name = self._resolve_dimension_id(old_name)
            if dim_id is None and resolved_dim_name is None:
                print("Error: Could not list dimensions (timeout or disconnect)")
                return
            if not dim_id:
                print(f"Error: Dimension '{old_name}' not found")
                return
            try:
                result = self.session.execute("rename_dimension", dim_id=dim_id, new_name=new_name)
                if result.success:
                    print(f"Renamed dimension: {old_name} -> {new_name}")
                else:
                    print(f"Error renaming dimension: {result.error}")
            except Exception as e:
                print(f"Error renaming dimension: {e}")

        elif sub == "delete":
            if not rest:
                print("Usage: dimension delete <name>")
                return
            dim_name = rest[0]
            dim_id, resolved_dim_name = self._resolve_dimension_id(dim_name)
            if dim_id is None and resolved_dim_name is None:
                print("Error: Could not list dimensions (timeout or disconnect)")
                return
            if not dim_id:
                print(f"Error: Dimension '{dim_name}' not found")
                return
            try:
                result = self.session.execute("delete_dimension", dim_id=dim_id)
                if result.success:
                    print(f"Deleted dimension: {dim_name}")
                else:
                    print(f"Error deleting dimension: {result.error}")
            except Exception as e:
                print(f"Error deleting dimension: {e}")

        else:
            print(f"Unknown dimension sub-command: {sub}")
            print("Usage: dimension create|rename|delete ...")

    def help_dimension(self: OpenMREPLCore):
        print("\ndimension create <name> [--set|--seq] [item1 ...]")
        print("dimension rename <old_name> <new_name>")
        print("dimension delete <name>")
        print("  Create, rename, or delete dimensions. Expands to canonical command IDs.")

    def do_view(self: OpenMREPLCore, arg: str):
        """
        View sub-command dispatcher and backward-compatible activator/creator.

        A view is a named layout over a cube. It defines which cube is shown
        and how cube dimensions are placed on layout axes.

        Supported axes:
          rows   vertical/table row axis
          cols   horizontal/table column axis
          page   higher-level page axis

        Usage:
          view <name>                                      Activate existing view
          view <name> = <cube>                             Create default view
          view <name> = <cube>::<row_dim>[:<col_dim>]      Create simple 2D view
          view <name> = <cube> rows: <dims...> cols: <dims...> page: <dims...>
          view create <name> = <spec>                      Create a view
          view rename <old_name> <new_name>                Rename a view
          view delete <name>                               Delete a view

        Examples:
          view V = Sales::Region:Product
          view PnL = Sales rows: Account cols: Month page: Scenario Version
          view create V = Sales::Region:Product
          view rename V V2
          view delete V

        Notes:
          - A cube defines the dimensional data space.
          - A view defines a layout over that cube.
          - The only supported layout axes are rows, cols, and page.
          - Sub-commands expand to canonical command IDs and call session.execute().
        """

        def _resolve_dim(dim_name: str, dims: list[dict]) -> str | None:
            for d in dims:
                if d.get("name") == dim_name or d.get("id") == dim_name:
                    return d.get("id")
            return None

        # _parse_role_syntax is defined at module level in this file

        # Sub-command dispatch: create, rename, delete
        parts = arg.split() if arg else []
        sub = parts[0].lower() if parts else None
        if sub in ("create", "rename", "delete"):
            rest = parts[1:]
            if sub == "create":
                rest_str = " ".join(rest)
                if '=' not in rest_str:
                    print("Usage: view create <name> = <spec>")
                    return
                name_part, spec_part = rest_str.split('=', 1)
                return self._do_view_create(name_part.strip(), spec_part.strip())
            if sub == "rename":
                if len(rest) < 2:
                    print("Usage: view rename <old_name> <new_name>")
                    return
                old_name, new_name = rest[0], rest[1]
                view_id, resolved_view_name = self._resolve_view_id(old_name)
                if view_id is None and resolved_view_name is None:
                    print("Error: Could not list views (timeout or disconnect)")
                    return
                if not view_id:
                    print(f"Error: View '{old_name}' not found")
                    return
                try:
                    result = self.session.execute("rename_view", view_id=view_id, new_name=new_name)
                    if result.success:
                        print(f"Renamed view: {old_name} -> {new_name}")
                    else:
                        print(f"Error renaming view: {result.error}")
                except Exception as e:
                    print(f"Error renaming view: {e}")
                return
            if sub == "delete":
                if not rest:
                    print("Usage: view delete <name>")
                    return
                return self.do_delete_view(rest[0])

        if not arg:
            data = self.session.query("current_view")
            view_id = data.get("view_id") if data else None
            if not view_id:
                print("No active view")
                return ""
            detail = self.session.query("view_detail", view_id=view_id)
            name = detail.get("name", view_id) if detail else view_id
            print(name)
            return name

        # No '=' means activate an existing view by name
        if '=' not in arg:
            name = arg.strip()
            data = self.session.query("view_list")
            views = data.get("views", []) if data else []
            view_id = None
            for v in views:
                if v.get("name") == name or v.get("id") == name:
                    view_id = v.get("id")
                    break
            if not view_id:
                print(f"Error: View not found: {name}")
                available = [v.get("name", "") for v in views]
                if available:
                    print(f"Available views: {', '.join(available)}")
                return
            self.session.execute("set_active_view", view_id=view_id)
            print(f"Active view: {name}")
            return

        name_part, spec_part = arg.split('=', 1)
        name = name_part.strip()
        spec = spec_part.strip()
        return self._do_view_create(name, spec)

    def help_view(self: OpenMREPLCore):
        print("\nview <name>")
        print("view <name> = <cube>::<row>[:<col>]")
        print("view <name> = <cube> rows: ... cols: ... page: ...")
        print("view create <name> = <spec>")
        print("view rename <old_name> <new_name>")
        print("view delete <name>")
        print("  Activate, create, rename, or delete views. Expands to canonical command IDs.")

    def _do_view_create(self: OpenMREPLCore, name: str, spec: str):
        """Create a view from a name and spec. Used by do_view and do_view create."""

        def _resolve_dim(dim_name: str, dims: list[dict]) -> str | None:
            for d in dims:
                if d.get("name") == dim_name or d.get("id") == dim_name:
                    return d.get("id")
            return None

        try:
            if '::' in spec:
                # Shorthand path: Cube::RowDim[:ColDim]
                cube_part, dims_part = spec.split('::', 1)
                cube_name = cube_part.strip()

                if ':' in dims_part:
                    row_dim_name, col_dim_name = dims_part.split(':', 1)
                    row_dim_name = row_dim_name.strip()
                    col_dim_name = col_dim_name.strip()
                else:
                    row_dim_name = dims_part.strip()
                    col_dim_name = None

                cube_data = self.session.query("cube_list")
                if cube_data is None:
                    print("Error: Could not list cubes (timeout or disconnect)")
                    return
                cubes = cube_data.get("cubes", [])
                cube_id = None
                for c in cubes:
                    if c.get("name") == cube_name or c.get("id") == cube_name:
                        cube_id = c.get("id")
                        break
                if not cube_id:
                    print(f"Error: Cube not found: {cube_name}")
                    return

                dim_data = self.session.query("dimension_list")
                if dim_data is None:
                    print("Error: Could not list dimensions (timeout or disconnect)")
                    return
                dims = dim_data.get("dimensions", [])

                row_dim_id = _resolve_dim(row_dim_name, dims)
                if not row_dim_id:
                    print(f"Error: Dimension not found: {row_dim_name}")
                    return
                col_dim_id = _resolve_dim(col_dim_name, dims) if col_dim_name else None

                # Create view via session command (works in both local and remote mode)
                result = self.session.execute(
                    "create_view",
                    name=name,
                    cube_id=cube_id,
                    row_dims=[row_dim_id],
                    col_dims=[col_dim_id] if col_dim_id else []
                )
            else:
                # Role-syntax path: Cube rows: A B cols: C page: D
                cube_name, layout = _parse_role_syntax(spec)

                cube_data = self.session.query("cube_list")
                if cube_data is None:
                    print("Error: Could not list cubes (timeout or disconnect)")
                    return
                cubes = cube_data.get("cubes", [])
                cube_id = None
                for c in cubes:
                    if c.get("name") == cube_name or c.get("id") == cube_name:
                        cube_id = c.get("id")
                        break
                if not cube_id:
                    print(f"Error: Cube not found: {cube_name}")
                    return

                dim_data = self.session.query("dimension_list")
                dims = dim_data.get("dimensions", []) if dim_data else []

                resolved_layout: dict[str, list[str]] = {"rows": [], "cols": [], "page": []}
                for role in ("rows", "cols", "page"):
                    for dim_name in layout.get(role, []):
                        dim_id = _resolve_dim(dim_name, dims)
                        if not dim_id:
                            print(f"Error: Dimension not found: {dim_name}")
                            return
                        resolved_layout[role].append(dim_id)

                result = self.session.execute(
                    "create_view",
                    name=name,
                    cube_id=cube_id,
                    layout=resolved_layout,
                )

            if result.success:
                view_id = result.data.get('id', f"view_{name.lower()}")
                self.session.execute("set_active_view", view_id=view_id)
                print(f"Created view: {view_id}")
            else:
                print(f"Error creating view: {result.error}")

        except Exception as e:
            print(f"Error creating view: {e}")
            import traceback
            traceback.print_exc()

    def do_rule(self: OpenMREPLCore, arg: str, batch_mode: bool = False):
        """
        Rule sub-command dispatcher.

        Usage:
          rule <target> = <expression>              # Set rule (backward-compatible)
          rule set <target> = <expression>            # Set rule
          rule delete <rule_id>                       # Delete rule
          rule set-anchored <view_id> <cell_ref> = <expression>
          rule delete-anchored <view_id> <cell_ref>

        Example: rule Sales::@.value:*.* = RAND() * 100
                 rule Sales::Years.2023:Products.A = 100
        
        With active cube (set by 'use'): rule dim.item = expr
        Example: use Sales
                 rule Revenue = Cost * 1.15   -> resolves to Sales::Revenue = Cost * 1.15

        Phase A Step A.2: Refactored to use bus event instead of direct engine calls.
        REPL method: do_rule()
        Bus command: "set_rule"
        Events: command.set_rule.before / command.set_rule.succeeded / command.set_rule.failed
        Sub-commands expand to canonical command IDs and call session.execute().

        When batch_mode is True, set-rule commands are not executed; instead a
        rule dict is returned so the caller (e.g. do_source) can batch them.
        """
        parts = arg.split() if arg else []
        sub = parts[0].lower() if parts else None

        if sub == "delete":
            rest = parts[1:]
            if not rest:
                print("Usage: rule delete <rule_id>")
                return
            return self.do_delete_rule(rest[0])

        if sub == "delete-anchored":
            rest = parts[1:]
            if len(rest) < 2:
                print("Usage: rule delete-anchored <view_id> <cell_ref>")
                return
            view_id, cell_ref = rest[0], rest[1]
            try:
                result = self.session.execute("delete_rule_anchored", view_id=view_id, cell_ref={"kind": "label", "value": cell_ref})
                if result.success:
                    print(f"Deleted anchored rule at {cell_ref}")
                else:
                    print(f"Error: {result.error}")
            except Exception as e:
                print(f"Error: {e}")
            return

        if sub == "set-anchored":
            rest_str = " ".join(parts[1:])
            if '=' not in rest_str:
                print("Usage: rule set-anchored <view_id> <cell_ref> = <expression>")
                return
            head, expr = rest_str.split('=', 1)
            head_parts = head.strip().split()
            if len(head_parts) < 2:
                print("Usage: rule set-anchored <view_id> <cell_ref> = <expression>")
                return
            view_id, cell_ref = head_parts[0], head_parts[1]
            try:
                result = self.session.execute(
                    "set_rule_anchored",
                    view_id=view_id,
                    cell_ref={"kind": "label", "value": cell_ref},
                    expression=expr.strip(),
                )
                if result.success:
                    print(f"Set anchored rule at {cell_ref} = {expr.strip()}")
                else:
                    print(f"Error: {result.error}")
            except Exception as e:
                print(f"Error: {e}")
            return

        # Default: rule set <target> = <expr> (or backward-compatible rule <target> = <expr>)
        if sub == "set":
            arg = " ".join(parts[1:])

        if not arg or '=' not in arg:
            print("Usage: rule <target> = <expression>")
            print("Example: rule Cube::@.value:*.* = RAND() * 100")
            return

        target, expr = arg.split('=', 1)
        target = target.strip()
        expr = expr.strip()

        try:
            cube_id = None
            cube_name = None

            if '::' in target:
                # Explicit cube prefix: "Sales::Revenue = Cost"
                parts = target.split('::')
                cube_name = parts[0].strip()
                spec = parts[1].strip() if len(parts) > 1 else ""

                cube_id, resolved_name = self._resolve_cube_id(cube_name)
                if cube_id is None and resolved_name is None:
                    print("Error: Could not list cubes (timeout or disconnect)")
                    return
                if not cube_id:
                    print(f"Error: Cube not found: {cube_name}")
                    return
            else:
                # No explicit cube - check context for active cube
                cube_id = self.variables.get('_current_cube')
                if not cube_id:
                    data = self.session.query("cube_list")
                    available = [c.get("name", "") for c in data.get("cubes", [])] if data else []
                    print("Error: No cube set. Use 'use <cube_name>' first.")
                    if available:
                        print(f"Available cubes: {', '.join(available)}")
                    return

                # Verify active cube still exists via query spine
                detail = self.session.query("cube_detail", cube_id=cube_id)
                if not detail:
                    del self.variables['_current_cube']
                    print("Error: Active cube was deleted. Use 'use <cube_name>' to set a new one.")
                    return

                # Prepend cube to target: "Revenue" -> "Sales::Revenue"
                target = f"{cube_id}::{target}"
                spec = target.split('::')[1].strip() if '::' in target else ""

            # Parse specs into targets list
            targets = []
            position_specs = spec.split(':') if spec else []

            # Parse specs
            dim_specs = []
            for pos_spec in position_specs:
                pos_spec = pos_spec.strip()
                if not pos_spec:
                    continue
                if pos_spec.startswith('@.'):
                    dim_specs.append(pos_spec)
                elif '.' in pos_spec:
                    # Split pure-wildcard patterns like "*.*" / "*.*.*";
                    # preserve "Dim.*" so the dot stays as a separator.
                    stripped = pos_spec.replace(' ', '')
                    if all(c in '*.' for c in stripped):
                        parts = pos_spec.split('.')
                        for part in parts:
                            dim_specs.append(part.strip())
                    else:
                        dim_specs.append(pos_spec)
                else:
                    dim_specs.append(pos_spec)

            # Check if all specs are wildcards (e.g., "*.*" or "*:*")
            all_wildcards = all(s.strip() == '*' for s in dim_specs)
            if all_wildcards and dim_specs:
                # Whole-cube wildcard: use special marker that engine expects
                targets = [("*", "*")]
            else:
                # Query cube detail for dimension IDs and dimension list for names
                cube_detail = self.session.query("cube_detail", cube_id=cube_id)
                cube_dimension_ids = cube_detail.get("dimension_ids", []) if cube_detail else []

                dim_data = self.session.query("dimension_list")
                dim_lookup = {}
                if dim_data:
                    for d in dim_data.get("dimensions", []):
                        dim_lookup[d.get("id")] = d.get("name", "")

                dim_idx = 0
                for dim_spec in dim_specs:
                    dim_spec = dim_spec.strip()
                    if not dim_spec:
                        continue

                    if '[' in dim_spec or ']' in dim_spec:
                        print(f"Error: Sequential keywords like [FIRST], [LAST], [PREV], [NEXT], [THIS]")
                        print(f"       are only supported in expressions (right-hand side), not in rule targets.")
                        return

                    if dim_spec == '*':
                        dim_idx += 1
                        continue
                    elif dim_spec.startswith('@.'):
                        channel = dim_spec[2:]
                        targets.append(("@", channel))
                        dim_idx += 1
                    elif dim_spec == '@':
                        targets.append(("@", "value"))
                        dim_idx += 1
                    elif '.' in dim_spec:
                        dim_name, item_name = dim_spec.split('.', 1)
                        targets.append((dim_name, item_name))
                        dim_idx += 1
                    else:
                        if dim_idx < len(cube_dimension_ids):
                            dim_id = cube_dimension_ids[dim_idx]
                            dim_name = dim_lookup.get(dim_id)
                            if dim_name:
                                targets.append((dim_name, dim_spec))
                        dim_idx += 1

            # Detect $ anchor prefix
            is_anchored = False
            if target.startswith("$"):
                is_anchored = True
                target = target[1:].strip()
                # Re-parse target after stripping $
                if '::' in target:
                    parts = target.split('::')
                    spec = parts[1].strip() if len(parts) > 1 else ""
                else:
                    spec = target

            if batch_mode:
                return {
                    "cube_id": cube_id,
                    "targets": targets,
                    "expression": expr,
                    "is_anchored": is_anchored,
                }

            # Execute the rule command via the executor (bus-driven)
            result = self.session.execute(
                "set_rule", cube_id=cube_id, targets=targets, expression=expr, is_anchored=is_anchored
            )
            if result.success:
                anchor_note = " (anchored)" if is_anchored else ""
                print(f"Added rule{anchor_note}: {target} = {expr}")
            else:
                print(f"Error: {result.error}")

        except Exception as e:
            print(f"Error setting rule: {e}")
            import traceback
            traceback.print_exc()
            if batch_mode:
                return None

    def help_rule(self: OpenMREPLCore):
        print("\nrule <target> = <expression>")
        print("rule set <target> = <expression>")
        print("rule delete <rule_id>")
        print("rule set-anchored <view_id> <cell_ref> = <expression>")
        print("rule delete-anchored <view_id> <cell_ref>")
        print("  Set, delete, or manage anchored rules. Expands to canonical command IDs.")

    def do_delete_rule(self: OpenMREPLCore, arg: str):
        """
        Delete a rule by ID.
        Usage: delete_rule <rule_id>
        Example: delete_rule rule_abc123
        """
        rule_id = arg.strip()
        if not rule_id:
            print("Usage: delete_rule <rule_id>")
            return
        result = self.session.execute("delete_rule", rule_id=rule_id)
        if result.status.name == "ERROR":
            print(f"Error: {result.error}")
        else:
            print(f"OK ({result.duration_ms:.1f}ms)")

    def help_delete_rule(self: OpenMREPLCore):
        print("\ndelete_rule <rule_id>")
        print("  Delete a rule by its stable ID.")
        print("  Example: delete_rule rule_abc123")

    def do_set_rule_order(self: OpenMREPLCore, arg: str):
        """
        Set rule execution order.
        Usage: set_rule_order <rule_id1> <rule_id2> ...
        """
        parts = arg.split()
        if not parts:
            print("Usage: set_rule_order <rule_id1> <rule_id2> ...")
            return
        result = self.session.execute("set_rule_order", rule_ids=parts)
        if result.status.name == "ERROR":
            print(f"Error: {result.error}")
        else:
            print(f"OK ({result.duration_ms:.1f}ms)")

    def help_set_rule_order(self: OpenMREPLCore):
        print("\nset_rule_order <rule_id1> <rule_id2> ...")
        print("  Set the execution order of rules.")
        print("  Example: set_rule_order rule_a rule_b rule_c")

    def _resolve_cube_id(self: OpenMREPLCore, cube_ref: str) -> tuple:
        """
        Resolve a cube reference (name or ID) to (cube_id, cube_name) tuple.

        Routes through QueryService instead of direct engine/workspace reads.
        Returns (cube_id, cube_name) if found.
        Returns (None, cube_ref) if the cube is genuinely not found.
        Returns (None, None) if the query failed (timeout/disconnect).
        """
        data = self.session.query("cube_list")
        if data is None:
            return None, None

        cubes = data.get("cubes", [])

        # Try direct ID match first
        for c in cubes:
            if c.get("id") == cube_ref:
                return cube_ref, c.get("name", cube_ref)

        # Try name match
        for c in cubes:
            if c.get("name") == cube_ref:
                return c.get("id"), cube_ref

        return None, cube_ref

    def _resolve_view_id(self: OpenMREPLCore, view_ref: str) -> tuple:
        """
        Resolve a view reference (name or ID) to (view_id, view_name) tuple.

        Routes through QueryService instead of direct engine/workspace reads.
        Returns (view_id, view_name) if found.
        Returns (None, view_ref) if the view is genuinely not found.
        Returns (None, None) if the query failed (timeout/disconnect).
        """
        data = self.session.query("view_list")
        if data is None:
            return None, None

        views = data.get("views", [])

        # Try direct ID match first
        for v in views:
            if v.get("id") == view_ref:
                return view_ref, v.get("name", view_ref)

        # Try name match
        for v in views:
            if v.get("name") == view_ref:
                return v.get("id"), view_ref

        return None, view_ref

    def _resolve_dimension_id(self: OpenMREPLCore, dim_ref: str) -> tuple:
        """
        Resolve a dimension reference (name or ID) to (dim_id, dim_name) tuple.

        Routes through QueryService instead of direct engine/workspace reads.
        Returns (dim_id, dim_name) if found.
        Returns (None, dim_ref) if the dimension is genuinely not found.
        Returns (None, None) if the query failed (timeout/disconnect).
        """
        data = self.session.query("dimension_list")
        if data is None:
            return None, None

        dimensions = data.get("dimensions", [])

        # Try direct ID match first
        for d in dimensions:
            if d.get("id") == dim_ref:
                return dim_ref, d.get("name", dim_ref)

        # Try name match
        for d in dimensions:
            if d.get("name") == dim_ref:
                return d.get("id"), dim_ref

        return None, dim_ref

    def do_delete_view(self: OpenMREPLCore, arg: str):
        """
        Delete a view by name or ID.
        Usage: delete_view <view_name_or_id>
        Example: delete_view SalesView

        Routes through the command spine: session.execute("delete_view", ...).
        """
        if not arg:
            print("Usage: delete_view <view_name_or_id>")
            return
        view_name = arg.strip()
        view_id, resolved_name = self._resolve_view_id(view_name)
        if view_id is None and resolved_name is None:
            print("Error: Could not list views (timeout or disconnect)")
            return
        if not view_id:
            print(f"Error: View '{view_name}' not found")
            return
        try:
            result = self.session.execute("delete_view", view_id=view_id)
            if result.success:
                print(f"Deleted view: {resolved_name or view_name}")
            else:
                print(f"Error deleting view: {result.error}")
        except Exception as e:
            print(f"Error deleting view: {e}")

    def help_delete_view(self: OpenMREPLCore):
        print("\ndelete_view <view_name_or_id>")
        print("  Delete a view by its name or stable ID.")
        print("  Example: delete_view SalesView")

    def do_calc(self: OpenMREPLCore, arg: str):
        """
        Recalculate all rules in the workspace.
        Usage: calc [scope]
        Example: calc
                 calc all
        
        Phase A Step A.4: Refactored to use bus event instead of direct engine calls.
        """
        scope = arg.strip() or "all"
        result = self.session.execute("run_recalculation", scope=scope)
        if result.success:
            print("Recalculation complete")
        else:
            print(f"Error during recalculation: {result.error}")

    def do_recalc(self: OpenMREPLCore, arg: str):
        """Alias for calc. Usage: recalc [scope]"""
        return self.do_calc(arg)

    def help_recalc(self: OpenMREPLCore):
        print("\nrecalc [scope=all]")
        print("  Recalculate all rules in the workspace.")
        print("  Usage: recalc")
        print("         calc          # REPL shorthand alias")

    def do_rules(self: OpenMREPLCore, arg: str):
        """
        List rules for a cube.
        Usage: rules <cube_name>
        """
        if not arg:
            print("Usage: rules <cube_name>")
            return

        cube_name = arg.strip()
        result = self.session.execute("list_rules", cube_id=cube_name)
        if not result.success:
            print(f"Error: {result.error}")
            return
        data = result.data
        rules = data.get("rules", []) if data else []
        if not rules:
            print(f"No rules for cube: {cube_name}")
            return
        print(f"Rules for {cube_name} ({len(rules)} rules):")
        for r in rules:
            targets = r.get("targets", [])
            if targets:
                target = ", ".join(f"{d}.{i}" for d, i in targets)
            else:
                target = "*"
            print(f"  {target} = {r.get('expression', '')}")