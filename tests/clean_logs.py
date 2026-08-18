"""Extract assertion results from unified-test REPL logs.

The cleaned logs contain only assertion results.  A combined CSV report enriches
each failure with its function and test metadata from ``test-matrix.csv``.
"""

from __future__ import annotations

import argparse
import csv
from collections import defaultdict
from pathlib import Path


FAILURE_PREFIX = "ASSERTION FAILED:"
REPORT_FIELDS = (
    "Log",
    "Function",
    "Category",
    "TestCaseID",
    "Formula",
    "Assertion",
    "Expected",
    "Tolerance",
    "Description",
    "FailureMessage",
)


def load_assertion_lookup(matrix_path: Path) -> dict[str, list[dict[str, str]]]:
    """Index matrix rows by the messages printed for failed assertions."""
    lookup: dict[str, list[dict[str, str]]] = defaultdict(list)
    with matrix_path.open(newline="", encoding="utf-8-sig") as matrix_file:
        for row in csv.DictReader(matrix_file):
            description = row["Description"].strip()
            messages = [description]
            if row["Assertion"].strip().lower() == "tolerance":
                messages.extend(
                    (f"{description} lower bound", f"{description} upper bound")
                )
            for message in messages:
                lookup[message].append(row)
    return dict(lookup)


def _is_passed_assertion(line: str) -> bool:
    # Some Windows exports contain a correctly decoded check mark, while others
    # contain its common UTF-8/Windows-1252 mojibake representation.
    stripped = line.strip()
    return stripped.startswith(("✓ assert ", "âœ“ assert "))


def clean_log(
    log_path: Path,
    output_path: Path,
    assertion_lookup: dict[str, list[dict[str, str]]],
) -> list[dict[str, str]]:
    """Write assertion-only output and return enriched failed assertions."""
    cleaned_lines: list[str] = []
    failures: list[dict[str, str]] = []

    for raw_line in log_path.read_text(encoding="utf-8-sig").splitlines():
        line = raw_line.strip()
        if _is_passed_assertion(line):
            assertion = line.split(" assert ", 1)[1]
            cleaned_lines.append(f"PASS | assert {assertion}")
            continue
        if not line.startswith(FAILURE_PREFIX):
            continue

        message = line.removeprefix(FAILURE_PREFIX).strip()
        matches = assertion_lookup.get(message, [])
        if not matches:
            cleaned_lines.append(f"FAIL | UNKNOWN | {message}")
            failures.append(
                {**{field: "" for field in REPORT_FIELDS},
                 "Log": log_path.name, "FailureMessage": message}
            )
            continue

        functions = ", ".join(dict.fromkeys(row["Function"] for row in matches))
        cleaned_lines.append(f"FAIL | {functions} | {message}")
        for row in matches:
            failures.append(
                {
                    "Log": log_path.name,
                    **{field: row.get(field, "") for field in REPORT_FIELDS[1:-1]},
                    "FailureMessage": message,
                }
            )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text("\n".join(cleaned_lines) + "\n", encoding="utf-8")
    return failures


def process_logs(logs_dir: Path, matrix_path: Path, output_dir: Path) -> tuple[int, int]:
    """Clean every source log and write the combined failure report."""
    lookup = load_assertion_lookup(matrix_path)
    log_paths = sorted(
        path for path in logs_dir.glob("*.log") if not path.name.endswith(".clean.log")
    )
    all_failures: list[dict[str, str]] = []
    for log_path in log_paths:
        cleaned_path = output_dir / f"{log_path.stem}.clean.log"
        all_failures.extend(clean_log(log_path, cleaned_path, lookup))

    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = output_dir / "failed_assertions.csv"
    with report_path.open("w", newline="", encoding="utf-8-sig") as report_file:
        writer = csv.DictWriter(report_file, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        writer.writerows(all_failures)
    return len(log_paths), len(all_failures)


def parse_args() -> argparse.Namespace:
    tests_dir = Path(__file__).resolve().parent
    parser = argparse.ArgumentParser(
        description="Clean unified-test REPL logs and report failed assertions."
    )
    parser.add_argument("--logs-dir", type=Path, default=tests_dir / "logs")
    parser.add_argument("--matrix", type=Path, default=tests_dir / "test-matrix.csv")
    parser.add_argument(
        "--output-dir", type=Path, default=tests_dir / "logs" / "cleaned"
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    log_count, failure_count = process_logs(
        args.logs_dir, args.matrix, args.output_dir
    )
    print(
        f"Cleaned {log_count} log(s); wrote {failure_count} failure row(s) "
        f"to {args.output_dir / 'failed_assertions.csv'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
