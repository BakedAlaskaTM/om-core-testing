"""Coordinator for repeatable Phase 1 worker-process runs."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from typing import Any

from lib_dst.schemas import TraceTranscript


def run_seed_range(
    start_seed: int, count: int, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = python_hash_seed
    environment["TZ"] = "UTC"
    completed = subprocess.run(
        [
            sys.executable,
            "-m",
            "lib_dst.worker",
            "--start-seed",
            str(start_seed),
            "--count",
            str(count),
        ],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])


def compare_seed_range(
    start_seed: int, count: int, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    first = run_seed_range(start_seed, count, python_hash_seed=python_hash_seed)
    second = run_seed_range(start_seed, count, python_hash_seed=python_hash_seed)
    if first != second:
        raise AssertionError(f"Worker replay mismatch: {first!r} != {second!r}")
    return first


def run_failure_in_worker(
    trace: TraceTranscript, *, python_hash_seed: str = "1"
) -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTHONHASHSEED"] = python_hash_seed
    environment["TZ"] = "UTC"
    completed = subprocess.run(
        [sys.executable, "-m", "lib_dst.worker", "--trace-stdin"],
        check=True,
        capture_output=True,
        text=True,
        input=trace.to_json(),
        env=environment,
    )
    return json.loads(completed.stdout.strip().splitlines()[-1])
