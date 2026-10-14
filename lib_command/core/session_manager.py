"""SessionManager — creates, resumes, validates, expires, and closes sessions (singleton)."""

from __future__ import annotations

from typing import Any, Optional

from .session_store import SessionStore, SessionRecord, get_session_store


class SessionManager:
    """Phase 2: creates, retrieves, lists, and closes sessions. Resume/validation/expiration policies are deferred."""

    _instance: Optional["SessionManager"] = None

    def __new__(cls) -> "SessionManager":
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance.store = get_session_store()
        return cls._instance

    def open_session(
        self,
        client_type: str,
        workspace_id: Optional[str],
        context: Any,
    ) -> str:
        """Create a new session and return its ID."""
        return self.store.create(client_type, workspace_id, context)

    def get_context(self, session_id: str) -> Optional[Any]:
        """Get the ExecutionContext for a session, or None if not found."""
        record = self.store.get(session_id)
        return record.context if record else None

    def get_record(self, session_id: str) -> Optional[SessionRecord]:
        """Get the full SessionRecord for a session."""
        return self.store.get(session_id)

    def close_session(self, session_id: str) -> None:
        """Close a session and remove it from the store."""
        self.store.close(session_id)

    def list_active(self) -> list[SessionRecord]:
        """List active sessions."""
        return self.store.list_active()


def get_session_manager() -> SessionManager:
    return SessionManager()


def _reset_session_manager() -> None:
    """Discard the process-wide session manager."""
    SessionManager._instance = None
