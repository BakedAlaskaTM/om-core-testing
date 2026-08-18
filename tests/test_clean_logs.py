from __future__ import annotations

import csv
from pathlib import Path

from tests.clean_logs import process_logs


MATRIX_HEADER = (
    "Standard,Version,Profile,Category,Function,SpecificationSection,TestCaseID,"
    "Formula,ExpectedType,Expected,Tolerance,Assertion,Description\n"
)


def test_cleans_logs_and_enriches_failures(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "run.log").write_text(
        "setup noise\n"
        "✓ assert Cube::Dim.FN_OK:Case.Happy_1 == 1\n"
        "ASSERTION FAILED: expected result\n"
        "more noise\n",
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.csv"
    matrix.write_text(
        MATRIX_HEADER
        + "OpenFormula,1.4,Small,Math,BROKEN,1,Happy_1,BROKEN(),Number,1,,Exact,expected result\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "cleaned"

    assert process_logs(logs_dir, matrix, output_dir) == (1, 1)
    assert (output_dir / "run.clean.log").read_text(encoding="utf-8") == (
        "PASS | assert Cube::Dim.FN_OK:Case.Happy_1 == 1\n"
        "FAIL | BROKEN | expected result\n"
    )
    with (output_dir / "failed_assertions.csv").open(
        newline="", encoding="utf-8-sig"
    ) as report:
        rows = list(csv.DictReader(report))
    assert rows[0]["Function"] == "BROKEN"
    assert rows[0]["TestCaseID"] == "Happy_1"


def test_maps_both_tolerance_assertions(tmp_path: Path):
    logs_dir = tmp_path / "logs"
    logs_dir.mkdir()
    (logs_dir / "run.log").write_text(
        "ASSERTION FAILED: close result lower bound\n"
        "ASSERTION FAILED: close result upper bound\n",
        encoding="utf-8",
    )
    matrix = tmp_path / "matrix.csv"
    matrix.write_text(
        MATRIX_HEADER
        + "OpenFormula,1.4,Small,Math,CLOSE,1,Happy_1,CLOSE(),Number,1,0.1,Tolerance,close result\n",
        encoding="utf-8",
    )

    assert process_logs(logs_dir, matrix, tmp_path / "out") == (1, 2)
