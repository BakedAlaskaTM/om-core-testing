from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Iterator
import os
from lib_utils.config import engine as engine_config


@dataclass(frozen=True)
class Node:
    """Represents a calculated region or a single cell in the dependency graph.

    This is an interface placeholder; we will evolve it to support:
    - cube slice nodes
    - function nodes
    - input nodes
    """

    key: str

# Master debug flag from engine.conf (overridable by environment variable)
_DEBUG_ENABLED = bool(int(os.environ.get("OPENM_DEBUG", "0"))) or engine_config("debug", "debug_enabled", False)

_DEBUG_CORE = _DEBUG_ENABLED and (bool(int(os.environ.get("OPENM_DEBUG_CORE", "0"))) or engine_config("debug", "debug_core", False))


class DependencyGraph:
    """Minimal dependency graph interface with dirty-node tracking."""

    def __init__(self) -> None:
        self._nodes: dict[str, Node] = {}
        self._edges: dict[Node, set[Node]] = {}
        self._reverse_edges: dict[Node, set[Node]] = {}
        self._dirty: set[Node] = set()

    # -- node helpers -------------------------------------------------
    def ensure_node(self, key: str) -> Node:
        node = self._nodes.get(key)
        if node is None:
            node = Node(key)
            self._nodes[key] = node
        self._edges.setdefault(node, set())
        self._reverse_edges.setdefault(node, set())
        return node

    def remove_node(self, key: str) -> None:
        node = self._nodes.pop(key, None)
        if node is None:
            return
        for precedent in list(self._reverse_edges.get(node, set())):
            self._edges.get(precedent, set()).discard(node)
        for dependent in list(self._edges.get(node, set())):
            self._reverse_edges.get(dependent, set()).discard(node)
        self._edges.pop(node, None)
        self._reverse_edges.pop(node, None)
        self._dirty.discard(node)

    def remove_nodes_with_prefix(self, prefix: str) -> None:
        """Remove all nodes whose keys start with ``prefix``."""
        for key in list(self._nodes.keys()):
            if key.startswith(prefix):
                self.remove_node(key)

    def nodes(self) -> Iterator[Node]:
        return iter(sorted(self._nodes.values(), key=lambda node: node.key))

    # -- edge helpers -------------------------------------------------
    def add_edge(self, src_key: str, dst_key: str) -> None:
        src = self.ensure_node(src_key)
        dst = self.ensure_node(dst_key)
        self._edges.setdefault(src, set()).add(dst)
        self._reverse_edges.setdefault(dst, set()).add(src)

    def remove_edge(self, src_key: str, dst_key: str) -> None:
        src = self._nodes.get(src_key)
        dst = self._nodes.get(dst_key)
        if src is None or dst is None:
            return
        self._edges.get(src, set()).discard(dst)
        self._reverse_edges.get(dst, set()).discard(src)

    def replace_precedents(self, dst_key: str, precedent_keys: Iterable[str]) -> None:
        dst = self.ensure_node(dst_key)
        new_precedents = {self.ensure_node(k) for k in precedent_keys}
        old_precedents = set(self._reverse_edges.get(dst, set()))
        _DEBUG_CORE and print(f"[DEBUG REPLACE_PREC] dst={dst_key[:50]}... new={len(new_precedents)} old={len(old_precedents)}")
        # Preserve edges from function/slice nodes - these are added by _end_tracking_node
        # for dirty propagation and should not be removed when replacing precedents
        preserved_precedents = {p for p in old_precedents if p.key.startswith(("func::", "slice::"))}
        if preserved_precedents:
            _DEBUG_CORE and print(f"[DEBUG REPLACE_PREC] Preserving {len(preserved_precedents)} func/slice edges")
            new_precedents = new_precedents | preserved_precedents
        for precedent in old_precedents - new_precedents:
            self._edges.get(precedent, set()).discard(dst)
            _DEBUG_CORE and print(f"[DEBUG REPLACE_PREC] Removed edge: {precedent.key[:50]}... -> {dst_key[:50]}...")
        for precedent in sorted(new_precedents, key=lambda node: node.key):
            self._edges.setdefault(precedent, set()).add(dst)
            _DEBUG_CORE and print(f"[DEBUG REPLACE_PREC] Added edge: {precedent.key[:50]}... -> {dst_key[:50]}...")
        self._reverse_edges[dst] = new_precedents

    def precedents_of(self, key: str) -> list[str]:
        node = self._nodes.get(key)
        if node is None:
            return []
        return sorted(n.key for n in self._reverse_edges.get(node, set()))

    def has_precedents(self, key: str) -> bool:
        """Check if a node has any precedents (dependencies) recorded."""
        node = self._nodes.get(key)
        if node is None:
            return False
        return bool(self._reverse_edges.get(node, set()))

    def dependents_of(self, key: str) -> list[str]:
        node = self._nodes.get(key)
        if node is None:
            return []
        deps = self._edges.get(node, set())
        return sorted(n.key for n in deps)

    # -- dirty tracking -----------------------------------------------
    def mark_dirty(self, key: str) -> None:
        node = self.ensure_node(key)
        self._dirty.add(node)

    def clear_dirty(self, key: str) -> None:
        node = self._nodes.get(key)
        if node is not None:
            self._dirty.discard(node)

    def pop_dirty(self) -> str | None:
        if not self._dirty:
            return None
        node = min(self._dirty, key=lambda candidate: candidate.key)
        self._dirty.remove(node)
        return node.key

    def dirty_keys(self) -> list[str]:
        return sorted(n.key for n in self._dirty)

    def is_dirty(self, key: str) -> bool:
        node = self._nodes.get(key)
        if node is None:
            return False
        return node in self._dirty

    def clear(self) -> None:
        """Clear all nodes, edges, and dirty tracking."""
        self._nodes.clear()
        self._edges.clear()
        self._reverse_edges.clear()
        self._dirty.clear()

    # -- analysis helpers --------------------------------------------
    def topo_sort(self) -> list[Node]:
        perm: set[Node] = set()
        temp: set[Node] = set()
        out: list[Node] = []

        def visit(n: Node) -> None:
            if n in perm:
                return
            if n in temp:
                raise ValueError("Cycle detected")
            temp.add(n)
            for m in sorted(self._edges.get(n, set()), key=lambda node: node.key):
                visit(m)
            temp.remove(n)
            perm.add(n)
            out.append(n)

        for n in sorted(self._nodes.values(), key=lambda node: node.key):
            visit(n)

        return out
