"""Shared OpenM script import path for GUI and automated validation."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class ScriptImportResult:
    path: str
    executed: int
    errors: tuple[str, ...]

    @property
    def success(self) -> bool:
        return not self.errors


def import_openm_script(
    session: Any, path: str | Path, *, gui_window: Any | None = None
) -> ScriptImportResult:
    """Source an OpenM script through the same parser used by the REPL and GUI."""

    source_path = Path(path).resolve()
    if source_path.suffix.lower() != ".openm":
        return ScriptImportResult(str(source_path), 0, ("Expected an .openm script",))
    if not source_path.is_file():
        return ScriptImportResult(str(source_path), 0, ("Script file does not exist",))

    # Import lazily to avoid making lib_scripting package import depend on the
    # compatibility REPL facade during module initialization.
    from lib_repl import OpenMREPL

    repl = OpenMREPL(session=session)
    if gui_window is not None:
        repl.gui_window = gui_window
        gui_port = getattr(gui_window, "gui_port", None)
        if gui_port is not None:
            repl.gui_port = gui_port
    repl.do_source(str(source_path))
    raw = getattr(repl, "_last_source_result", None)
    if not isinstance(raw, dict):
        return ScriptImportResult(
            str(source_path), 0, ("Script source did not report an outcome",)
        )
    errors = tuple(str(error) for error in raw.get("errors", ()))
    return ScriptImportResult(
        str(source_path), int(raw.get("executed", 0)), errors
    )
