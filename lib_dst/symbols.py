"""Typed logical symbols shared by trace generation and replay."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any


class SymbolKind(str, Enum):
    DIMENSION = "dimension"
    ITEM = "item"
    CUBE = "cube"


@dataclass(frozen=True)
class Symbol:
    handle: str
    kind: SymbolKind
    name: str
    owner: str | None = None


class SymbolTable:
    def __init__(self) -> None:
        self._symbols: dict[str, Symbol] = {}
        self._runtime_ids: dict[str, str] = {}

    def declare(self, symbol: Symbol) -> Symbol:
        if symbol.handle in self._symbols:
            raise ValueError(f"Duplicate symbol handle: {symbol.handle}")
        if symbol.owner is not None and symbol.owner not in self._symbols:
            raise ValueError(f"Unknown owner handle: {symbol.owner}")
        self._symbols[symbol.handle] = symbol
        return symbol

    def require(self, handle: str, kind: SymbolKind | None = None) -> Symbol:
        try:
            symbol = self._symbols[handle]
        except KeyError as exc:
            raise KeyError(f"Unknown symbol handle: {handle}") from exc
        if kind is not None and symbol.kind is not kind:
            raise TypeError(f"{handle} is {symbol.kind.value}, expected {kind.value}")
        return symbol

    def bind(self, handle: str, runtime_id: str, kind: SymbolKind) -> None:
        self.require(handle, kind)
        self._runtime_ids[handle] = runtime_id

    def runtime_id(self, handle: str, kind: SymbolKind) -> str:
        self.require(handle, kind)
        try:
            return self._runtime_ids[handle]
        except KeyError as exc:
            raise KeyError(f"Symbol is not bound: {handle}") from exc

    def to_dict(self) -> dict[str, Any]:
        return {
            handle: {
                "kind": symbol.kind.value,
                "name": symbol.name,
                "owner": symbol.owner,
            }
            for handle, symbol in sorted(self._symbols.items())
        }
