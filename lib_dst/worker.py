"""Isolated Phase 1 batch worker."""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import io
import json
import sys
from dataclasses import dataclass

from lib_dst.executor import execute_trace
from lib_dst.failures import capture_failure
from lib_dst.generator import generate_trace
from lib_dst.schemas import TraceTranscript


@dataclass(frozen=True)
class BatchResult:
    start_seed: int
    count: int
    digest: str
    oracle_failures: int

    def to_dict(self) -> dict[str, int | str]:
        return {
            "start_seed": self.start_seed,
            "count": self.count,
            "digest": self.digest,
            "oracle_failures": self.oracle_failures,
        }


def run_batch(start_seed: int, count: int) -> BatchResult:
    if count < 1:
        raise ValueError("count must be positive")
    digest = hashlib.sha256()
    oracle_failures = 0
    for seed in range(start_seed, start_seed + count):
        generated = generate_trace(seed)
        replay_trace = TraceTranscript.from_json(generated.to_json())
        if replay_trace.to_json() != generated.to_json():
            raise AssertionError(f"Trace round trip changed seed {seed}")
        with contextlib.redirect_stdout(io.StringIO()):
            report = execute_trace(replay_trace)
        digest.update(generated.to_json().encode("utf-8"))
        digest.update(b"\0")
        digest.update(report.to_json().encode("utf-8"))
        digest.update(b"\n")
        oracle_failures += not report.oracle_passed
    return BatchResult(start_seed, count, digest.hexdigest(), oracle_failures)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--start-seed", type=int, default=0)
    parser.add_argument("--count", type=int, default=1)
    parser.add_argument("--trace-stdin", action="store_true")
    args = parser.parse_args()
    if args.trace_stdin:
        trace = TraceTranscript.from_json(sys.stdin.read())
        with contextlib.redirect_stdout(io.StringIO()):
            outcome = capture_failure(trace)
        print(outcome.to_json())
        return
    result = run_batch(args.start_seed, args.count)
    print(json.dumps(result.to_dict(), sort_keys=True, separators=(",", ":")))
    if result.oracle_failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
