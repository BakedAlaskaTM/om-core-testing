"""Normalized production branch coverage using Python's monitoring API."""

from __future__ import annotations

import sys
import linecache
from dataclasses import dataclass
from pathlib import Path
from types import CodeType
from typing import Any


_PRODUCTION_ROOTS = {
    "lib_command",
    "lib_openm",
    "lib_runtime",
    "lib_storeadapters",
}


@dataclass(frozen=True)
class CoverageSnapshot:
    arcs: tuple[str, ...]

    def to_dict(self) -> dict[str, list[str]]:
        return {"arcs": list(self.arcs)}


class BranchCoverage:
    """Collect stable branch arcs for the production modules exercised by DST."""

    def __init__(self, workspace_root: str | Path | None = None) -> None:
        if not hasattr(sys, "monitoring"):
            raise RuntimeError("Branch coverage requires Python sys.monitoring")
        self.workspace_root = Path(workspace_root or Path(__file__).parents[1]).resolve()
        self._arcs: set[str] = set()
        self._module_cache: dict[CodeType, str | None] = {}
        self._line_cache: dict[tuple[CodeType, int], int] = {}
        self._tool_id: int | None = None
        self._events: tuple[int, ...] = ()

    def __enter__(self) -> "BranchCoverage":
        monitoring = sys.monitoring
        for tool_id in range(6):
            if monitoring.get_tool(tool_id) is None:
                self._tool_id = tool_id
                break
        if self._tool_id is None:
            raise RuntimeError("No sys.monitoring tool ID is available")

        monitoring.use_tool_id(self._tool_id, "om-dst-branch-coverage")
        events = monitoring.events
        if hasattr(events, "BRANCH_LEFT") and hasattr(events, "BRANCH_RIGHT"):
            self._events = (events.BRANCH_LEFT, events.BRANCH_RIGHT)
            monitoring.register_callback(
                self._tool_id, events.BRANCH_LEFT, self._left_callback
            )
            monitoring.register_callback(
                self._tool_id, events.BRANCH_RIGHT, self._right_callback
            )
            monitoring.set_events(
                self._tool_id, events.BRANCH_LEFT | events.BRANCH_RIGHT
            )
        elif hasattr(events, "BRANCH"):
            self._events = (events.BRANCH,)
            monitoring.register_callback(
                self._tool_id, events.BRANCH, self._branch_callback
            )
            monitoring.set_events(self._tool_id, events.BRANCH)
        else:
            self._release()
            raise RuntimeError("sys.monitoring does not expose branch events")
        return self

    def __exit__(self, exc_type: Any, exc: Any, traceback: Any) -> None:
        self._release()

    @property
    def snapshot(self) -> CoverageSnapshot:
        return CoverageSnapshot(tuple(sorted(self._arcs)))

    def _release(self) -> None:
        if self._tool_id is None:
            return
        monitoring = sys.monitoring
        monitoring.set_events(self._tool_id, 0)
        for event in self._events:
            monitoring.register_callback(self._tool_id, event, None)
        monitoring.free_tool_id(self._tool_id)
        self._tool_id = None
        self._events = ()

    def _left_callback(self, code: CodeType, source: int, destination: int) -> None:
        self._record(code, source, destination, "L")

    def _right_callback(self, code: CodeType, source: int, destination: int) -> None:
        self._record(code, source, destination, "R")

    def _branch_callback(self, code: CodeType, source: int, destination: int) -> None:
        self._record(code, source, destination, "B")

    def _record(
        self, code: CodeType, source_offset: int, destination_offset: int, side: str
    ) -> None:
        module = self._module(code)
        if module is None:
            return
        source_line = self._line(code, source_offset)
        destination_line = self._line(code, destination_offset)
        if self._is_diagnostic_branch(code, source_line):
            return
        self._arcs.add(
            f"{module}:{code.co_qualname}:{source_line}->{destination_line}:{side}"
        )

    def _module(self, code: CodeType) -> str | None:
        if code in self._module_cache:
            return self._module_cache[code]
        try:
            path = Path(code.co_filename).resolve()
            relative = path.relative_to(self.workspace_root)
        except (OSError, ValueError):
            self._module_cache[code] = None
            return None
        if not relative.parts or relative.parts[0] not in _PRODUCTION_ROOTS:
            self._module_cache[code] = None
            return None
        if path.suffix != ".py" or "__pycache__" in relative.parts:
            self._module_cache[code] = None
            return None
        module = relative.as_posix()
        self._module_cache[code] = module
        return module

    def _line(self, code: CodeType, offset: int) -> int:
        key = (code, offset)
        if key in self._line_cache:
            return self._line_cache[key]
        line = code.co_firstlineno
        for start, end, candidate in code.co_lines():
            if start <= offset < end and candidate is not None:
                line = candidate
                break
        self._line_cache[key] = line
        return line

    @staticmethod
    def _is_diagnostic_branch(code: CodeType, source_line: int) -> bool:
        if code.co_qualname.endswith("._record_rule_eval_profile"):
            return True
        source = linecache.getline(code.co_filename, source_line)
        return "SLOW_LOG_THRESHOLD" in source or "_DEBUG_ENGINE" in source
