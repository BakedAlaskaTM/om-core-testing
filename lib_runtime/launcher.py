"""Launcher — manages the local server subprocess lifecycle.

For local endpoints (unix:///path, tcp://localhost:port), Launcher starts
and stops the server process. For remote endpoints (tcp://host:port where
host is not localhost), start() and stop() are no-ops — the server is
already running elsewhere.

Launcher reads server-specific config (server_bin, state_bytes, etc.) from
om-engine.conf [remote] section and passes them as CLI args when spawning.
"""

from __future__ import annotations

import logging
import os
import socket
import subprocess
import time
from pathlib import Path
from urllib.parse import urlparse

_log = logging.getLogger(__name__)

DEFAULT_SOCKET_PATH = "/tmp/om-engine.sock"


def _resolve_server_bin() -> Path:
    """Locate the server binary from config or environment."""
    from lib_utils.config import engine

    server_bin = engine("remote", "server_bin", "")
    if server_bin:
        return Path(server_bin)
    return Path(os.environ.get("OM_ENGINE_SERVER_BIN", "om-engine-server"))


class Launcher:
    """Manages the lifecycle of a local remote-server subprocess.

    For remote endpoints, start()/stop() are no-ops.
    """

    def __init__(
        self,
        endpoint: str,
        *,
        server_bin: Path | str | None = None,
        state_bytes: int | None = None,
        max_undo_entries: int | None = None,
        scratch_bytes: int | None = None,
        server_args: list[str] | None = None,
        mode: str | None = None,
    ) -> None:
        self._endpoint = endpoint
        self._parsed = urlparse(endpoint)
        self._is_local = self._is_local_endpoint()
        self._server_bin = Path(server_bin) if server_bin else _resolve_server_bin()
        self._state_bytes = state_bytes
        self._max_undo_entries = max_undo_entries
        self._scratch_bytes = scratch_bytes
        self._server_args = list(server_args or [])
        if mode is not None:
            self._server_args.append(f"--mode={mode}")
        self._proc: subprocess.Popen | None = None

    def _is_local_endpoint(self) -> bool:
        scheme = self._parsed.scheme
        if scheme == "unix":
            return True
        if scheme == "tcp":
            host = self._parsed.hostname or "localhost"
            return host in ("localhost", "127.0.0.1", "::1")
        return False

    @property
    def is_local(self) -> bool:
        return self._is_local

    def start(self) -> None:
        """Start the server subprocess if local. No-op for remote endpoints."""
        if not self._is_local:
            _log.debug("Remote endpoint %s — Launcher.start() is no-op", self._endpoint)
            return

        if self._proc is not None:
            return

        if not self._server_bin.exists():
            raise FileNotFoundError(f"server binary not found: {self._server_bin}")

        # Build CLI args
        args: list[str] = [str(self._server_bin)]

        if self._parsed.scheme == "unix":
            socket_path = self._parsed.path
            args.append(socket_path)
            # Remove stale socket
            try:
                os.unlink(socket_path)
            except FileNotFoundError:
                pass
        elif self._parsed.scheme == "tcp":
            host = self._parsed.hostname or "localhost"
            port = self._parsed.port or 7654
            args.append(f"tcp://{host}:{port}")

        if self._state_bytes is not None:
            args.append(f"--state-bytes={self._state_bytes}")
        if self._max_undo_entries is not None:
            args.append(f"--max-undo-entries={self._max_undo_entries}")
        if self._scratch_bytes is not None:
            args.append(f"--scratch-bytes={self._scratch_bytes}")

        args.extend(self._server_args)

        # Allow extra CLI args via environment variable for testing.
        extra = os.environ.get("OMENGINE_EXTRA_ARGS")
        if extra:
            args.extend(extra.split())

        _log.info("Starting server: %s", " ".join(args))
        self._proc = subprocess.Popen(
            args,
            stderr=None,  # inherit parent stderr so server logs appear in terminal
        )

        # Wait for the server to be ready
        for _ in range(50):
            if self._is_ready():
                _log.info("Server ready at %s", self._endpoint)
                return
            time.sleep(0.1)

        raise RuntimeError(f"server did not start at {self._endpoint}")

    def _is_ready(self) -> bool:
        if self._parsed.scheme == "unix":
            path = self._parsed.path
            return os.path.exists(path)
        elif self._parsed.scheme == "tcp":
            host = self._parsed.hostname or "localhost"
            port = self._parsed.port or 7654
            try:
                with socket.create_connection((host, port), timeout=0.5):
                    return True
            except (OSError, ConnectionRefusedError):
                return False
        return False

    def stop(self) -> None:
        """Stop the server subprocess if we started it. No-op for remote."""
        if self._proc is not None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            self._proc = None
            _log.info("Server stopped")

        # Clean up Unix socket
        if self._parsed.scheme == "unix":
            try:
                os.unlink(self._parsed.path)
            except FileNotFoundError:
                pass

    def __enter__(self) -> "Launcher":
        self.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self.stop()
