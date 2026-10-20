from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any
import uuid


_id_provider: ContextVar[Any | None] = ContextVar("om_id_provider", default=None)


def bind_id_provider(provider: Any) -> Token:
    """Bind an ID provider to the current execution context."""
    if not hasattr(provider, "new_id"):
        raise TypeError("ID provider must define new_id(prefix)")
    return _id_provider.set(provider)


def reset_id_provider(token: Token) -> None:
    """Restore the provider binding that preceded *token*."""
    _id_provider.reset(token)


def new_id(prefix: str | None = None) -> str:
    provider = _id_provider.get()
    if provider is not None:
        return provider.new_id(prefix)
    value = uuid.uuid4().hex
    return f"{prefix}_{value}" if prefix else value
