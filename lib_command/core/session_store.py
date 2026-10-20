"""SessionStore — in-memory registry of active sessions (singleton)."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional

from .session_view_state import SessionViewState


@dataclass
class SessionRecord:
    session_id: str
    client_type: str          # gui | repl | cli | headless
    workspace_id: Optional[str]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_accessed: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    context: Any = None       # ExecutionContext


class SessionStore:
    """In-memory session metadata and per-session view state."""

    _instance: Optional["SessionStore"] = None

    def __new__(cls) -> "SessionStore":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._sessions: dict[str, SessionRecord] = {}
            cls._instance._view_states: dict[str, SessionViewState] = {}
        return cls._instance

    def create(
        self,
        client_type: str,
        workspace_id: Optional[str],
        context: Any,
    ) -> str:
        providers = getattr(context, "providers", None)
        ids = getattr(providers, "ids", None)
        clock = getattr(providers, "clock", None)
        sid = str(ids.uuid4()) if ids is not None else str(uuid.uuid4())
        now = clock.now_utc() if clock is not None else datetime.now(timezone.utc)
        self._sessions[sid] = SessionRecord(
            session_id=sid,
            client_type=client_type,
            workspace_id=workspace_id,
            created_at=now,
            last_accessed=now,
            context=context,
        )
        # Initialise per-session view state from the workspace-level saved default.
        # engine.saved_default_view_id is the schema-v14 file-level active view,
        # not runtime session state.  It is used here only as an initial default
        # until the GUI/CLI explicitly sets the session active view via
        # set_active_view.  Falls back to deprecated active_view_id alias.
        view_state = SessionViewState(session_id=sid)
        if context is not None:
            workspace = getattr(context, "workspace", None)
            if workspace is not None:
                default_view_id = getattr(workspace, "saved_default_view_id", None)
                if default_view_id:
                    view_state.active_view_id = default_view_id
        self._view_states[sid] = view_state
        return sid

    def get(self, session_id: str) -> Optional[SessionRecord]:
        record = self._sessions.get(session_id)
        if record:
            providers = getattr(record.context, "providers", None)
            clock = getattr(providers, "clock", None)
            record.last_accessed = (
                clock.now_utc() if clock is not None else datetime.now(timezone.utc)
            )
        return record

    def get_view_state(self, session_id: str) -> Optional[SessionViewState]:
        """Return the SessionViewState for a session, or None if not found."""
        return self._view_states.get(session_id)

    def set_view_state(self, session_id: str, state: SessionViewState) -> None:
        """Replace the SessionViewState for a session."""
        self._view_states[session_id] = state

    def set_active_view(self, session_id: str, view_id: str | None) -> None:
        """Set the active view for a session."""
        vs = self._view_states.get(session_id)
        if vs is None:
            vs = SessionViewState(session_id=session_id)
            self._view_states[session_id] = vs
        vs.active_view_id = view_id

    def set_selection(
        self,
        session_id: str,
        *,
        cursor_row: int = 0,
        cursor_col: int = 0,
        anchor_row: int = 0,
        anchor_col: int = 0,
        selection_mode: str = "cell",
        selected_indices: list[tuple[int, int] | int] | None = None,
    ) -> None:
        """Set cursor, anchor, and selection indices for a session."""
        vs = self._view_states.get(session_id)
        if vs is None:
            vs = SessionViewState(session_id=session_id)
            self._view_states[session_id] = vs
        vs.cursor_row = cursor_row
        vs.cursor_col = cursor_col
        vs.anchor_row = anchor_row
        vs.anchor_col = anchor_col
        vs.selection_mode = selection_mode
        vs.selected_indices = list(selected_indices) if selected_indices is not None else []

    def set_active_cell(self, session_id: str, active_cell: tuple[int, int] | None) -> None:
        """Set the active cell for a session."""
        vs = self._view_states.get(session_id)
        if vs is None:
            vs = SessionViewState(session_id=session_id)
            self._view_states[session_id] = vs
        vs.active_cell = active_cell

    def set_anchor_cell(self, session_id: str, anchor_cell: tuple[int, int] | None) -> None:
        """Set the anchor cell for a session."""
        vs = self._view_states.get(session_id)
        if vs is None:
            vs = SessionViewState(session_id=session_id)
            self._view_states[session_id] = vs
        vs.anchor_cell = anchor_cell

    def set_scroll_pos(self, session_id: str, scroll_pos: tuple[int, int] | None) -> None:
        """Set the scroll position for a session."""
        vs = self._view_states.get(session_id)
        if vs is None:
            vs = SessionViewState(session_id=session_id)
            self._view_states[session_id] = vs
        vs.scroll_pos = scroll_pos

    def set_selected_indices(self, session_id: str, selected_indices: list[tuple[int, int] | int] | None) -> None:
        """Set the selected indices for a session."""
        vs = self._view_states.get(session_id)
        if vs is None:
            vs = SessionViewState(session_id=session_id)
            self._view_states[session_id] = vs
        vs.selected_indices = list(selected_indices) if selected_indices is not None else []

    def set_selection_mode(self, session_id: str, selection_mode: str) -> None:
        """Set the selection mode for a session."""
        vs = self._view_states.get(session_id)
        if vs is None:
            vs = SessionViewState(session_id=session_id)
            self._view_states[session_id] = vs
        vs.selection_mode = selection_mode

    def close(self, session_id: str) -> None:
        self._sessions.pop(session_id, None)
        self._view_states.pop(session_id, None)

    def list_active(self) -> list[SessionRecord]:
        return list(self._sessions.values())

    def clear(self) -> None:
        """Clear all sessions. Intended for tests and process reset only."""
        self._sessions.clear()
        self._view_states.clear()


def get_session_store() -> SessionStore:
    return SessionStore()


def _reset_session_store() -> None:
    """Discard all process-local sessions and view state."""
    instance = SessionStore._instance
    if instance is not None:
        instance._sessions.clear()
        instance._view_states.clear()
    SessionStore._instance = None
