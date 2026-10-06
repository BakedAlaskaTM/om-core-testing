"""Generate the human-readable OM Core function conformance dashboard.

The CSV is the normative test manifest.  This generator deliberately leaves
errors unwrapped so the GUI displays the engine's real typed result.  OpenM's
script-level ``assert`` currently cannot compare errored cells, so those rows
are marked for a typed test harness instead of being weakened with IFERROR.
"""

import csv
import json
import math
import re
from collections import defaultdict


REQUIRED_COLUMNS = {
    "Standard", "Version", "Profile", "Category", "Function",
    "SpecificationSection", "TestCaseID", "Formula", "ExpectedType",
    "Expected", "Tolerance", "Assertion", "Description",
}

EXPECTED_TYPES = {"NUMBER", "BOOLEAN", "STRING", "ERROR"}
ASSERTIONS = {"EQUALS", "ALMOST_EQUALS", "ERROR_MATCH", "PROPAGATES_ERROR"}
TEST_CASE_ID_RE = re.compile(r"^TC_[A-Z]+_[A-Z0-9_]+_\d{3}$")
REFERENCE_FIXTURE_FUNCTIONS = {
    "INDEX", "OFFSET", "MATCH", "LOOKUP", "IRR", "XIRR",
    "XLS_INDEX", "XLS_OFFSET", "XLS_MATCH", "XLS_ROWS", "XLS_COLUMNS",
    "XLS_HLOOKUP", "XLS_VLOOKUP", "XLS_IRR", "XLS_XIRR",
}


def function_member(function_name: str) -> str:
    return f"FN_{function_name}"


def _assert_literal(test: dict) -> str | None:
    expected = test["expected"]
    expected_type = test["expected_type"].lower()
    if expected_type == "number":
        return expected
    # do_assert treats quoted content as the assertion message. Bare tokens are
    # safe for simple strings; empty/whitespace-bearing strings need pytest.
    if expected_type == "boolean":
        return f"{expected.upper()}()"
    if expected_type == "string" and expected and not any(c.isspace() for c in expected):
        return expected
    return None


def _rule_literal(test: dict) -> str:
    """Render a CSV expected value as an OpenM rule literal."""
    expected_type = test["expected_type"].lower()
    expected = test["expected"]
    if expected_type == "number":
        return expected
    if expected_type == "boolean":
        return f"{expected.upper()}()"
    if expected_type == "string":
        return json.dumps(expected, ensure_ascii=False)
    raise ValueError(f"Cannot render expected {expected_type} value as a rule literal")


def _absolute_tolerance(expected: float, tolerance: str) -> float:
    """Convert a manifest tolerance to an absolute numeric delta."""
    normalized = tolerance.strip().upper()
    if normalized.endswith("ULP"):
        count_text = normalized.removesuffix("ULP").strip()
        count = float(count_text) if count_text else 1.0
        return count * math.ulp(expected)
    return float(tolerance)


def _pass_condition(test: dict, ref: str) -> str:
    """Build a Boolean rule expression for one manifest test case."""
    assertion = test["assertion"].lower()
    if assertion in {"error_match", "propagates_error"}:
        # The rule evaluator has IFERROR but no error-code inspection function,
        # so an Error assertion means that any typed cell error is expected.
        marker = json.dumps("__OPENM_EXPECTED_ERROR__")
        return f"IFERROR({ref},{marker}) == {marker}"
    if assertion == "almost_equals":
        expected = float(test["expected"])
        tolerance = _absolute_tolerance(expected, test["tolerance"])
        lower = f"{expected - tolerance:.17g}"
        upper = f"{expected + tolerance:.17g}"
        return (
            f"IFERROR(AND({ref} >= {lower},{ref} <= {upper}),FALSE())"
        )
    if assertion == "equals":
        return f"IFERROR({ref} == {_rule_literal(test)},FALSE())"
    raise ValueError(f"Unsupported assertion type: {test['assertion']}")


def _load_manifest(csv_file_path: str) -> list[dict[str, str]]:
    """Load and validate the normative CSV manifest."""
    tests: list[dict[str, str]] = []
    seen_ids: set[str] = set()
    with open(csv_file_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing))}")
        for line_number, row in enumerate(reader, start=2):
            test = {key.lower(): value.strip() for key, value in row.items()}
            test["expected_type"] = test.pop("expectedtype").upper()
            test["assertion"] = test["assertion"].upper()
            test_id = test["testcaseid"]
            if not TEST_CASE_ID_RE.fullmatch(test_id):
                raise ValueError(f"Invalid TestCaseID at line {line_number}: {test_id}")
            if test_id in seen_ids:
                raise ValueError(f"Duplicate TestCaseID at line {line_number}: {test_id}")
            if test["expected_type"] not in EXPECTED_TYPES:
                raise ValueError(
                    f"Unsupported ExpectedType at line {line_number}: {test['expected_type']}"
                )
            if test["assertion"] not in ASSERTIONS:
                raise ValueError(
                    f"Unsupported Assertion at line {line_number}: {test['assertion']}"
                )
            if test["assertion"] == "ALMOST_EQUALS":
                if test["expected_type"] != "NUMBER" or not test["tolerance"]:
                    raise ValueError(
                        f"ALMOST_EQUALS requires NUMBER and Tolerance at line {line_number}"
                    )
                _absolute_tolerance(float(test["expected"]), test["tolerance"])
            if test["assertion"] in {"ERROR_MATCH", "PROPAGATES_ERROR"}:
                if test["expected_type"] != "ERROR" or not test["expected"].startswith("#"):
                    raise ValueError(
                        f"{test['assertion']} requires an ERROR code at line {line_number}"
                    )
            tests.append(test)
            seen_ids.add(test_id)
    return tests


def generate_unified_openm(csv_file_path: str, output_openm_path: str):
    functions_by_category = defaultdict(set)
    tests_by_function = defaultdict(list)
    cases = set()

    tests = _load_manifest(csv_file_path)
    for test in tests:
        functions_by_category[test["category"]].add(test["function"])
        tests_by_function[(test["category"], test["function"])].append(test)
        cases.add(test["testcaseid"])

    lines = [
        "# ==============================================================================",
        "# OM CORE FUNCTION-LEVEL CONFORMANCE DASHBOARD",
        "# Generated from tests/test-matrix.csv; formulas use OM Core comma syntax.",
        "# Error rows retain raw errors and require typed-harness verification.",
        "# ==============================================================================\n",
    ]

    if any(test["function"] in REFERENCE_FIXTURE_FUNCTIONS for test in tests):
        lines.extend([
            "# Shared deterministic fixture for lookup and rate-solving functions.",
            "dim Row Header Data1 Data2 Periodic Dated Dates",
            "dim Column C1 C2 C3",
            "cube MockCube Row Column",
            "rule MockCube::Row.Header:Column.C1 = 1",
            "rule MockCube::Row.Header:Column.C2 = 2",
            "rule MockCube::Row.Header:Column.C3 = 3",
            "rule MockCube::Row.Data1:Column.C1 = 10",
            "rule MockCube::Row.Data1:Column.C2 = 20",
            "rule MockCube::Row.Data1:Column.C3 = 50",
            "rule MockCube::Row.Data2:Column.C1 = 30",
            "rule MockCube::Row.Data2:Column.C2 = 40",
            "rule MockCube::Row.Data2:Column.C3 = 100",
            "rule MockCube::Row.Periodic:Column.C1 = -100",
            "rule MockCube::Row.Periodic:Column.C2 = 60",
            "rule MockCube::Row.Periodic:Column.C3 = 60",
            "rule MockCube::Row.Dated:Column.C1 = -100",
            "rule MockCube::Row.Dated:Column.C2 = 0",
            "rule MockCube::Row.Dated:Column.C3 = 110",
            "rule MockCube::Row.Dates:Column.C1 = DATE(2024,1,1)",
            "rule MockCube::Row.Dates:Column.C2 = DATE(2024,7,2)",
            "rule MockCube::Row.Dates:Column.C3 = DATE(2025,1,1)\n",
        ])

    lines.append(f"dim Case {' '.join(sorted(cases))} PassFail")

    for category in sorted(functions_by_category):
        function_dim = f"Function_{category}"
        cube = f"TestCube_{category}"
        members = " ".join(function_member(f) for f in sorted(functions_by_category[category]))
        lines.extend([
            f"dim {function_dim} {members}",
            f"cube {cube} {function_dim} Case",
            f"view TestRunner_{category} = {cube} rows: {function_dim} cols: Case",
        ])

    lines.extend(["", "# RULES UNDER TEST"])
    for test in tests:
        category = test["category"]
        address = (
            f"TestCube_{category}::Function_{category}."
            f"{function_member(test['function'])}:Case.{test['testcaseid']}"
        )
        lines.append(
            f"# {test['standard']} {test['version']} section {test['specificationsection']} "
            f"[{test['profile']}] {test['description']}"
        )
        lines.append(f"rule {address} = {test['formula']}")

    lines.extend(["", "# ONE PASS/FAIL VALUE PER TESTED FUNCTION"])
    for (category, function), function_tests in sorted(tests_by_function.items()):
        conditions = []
        for test in function_tests:
            ref = (
                f"TestCube_{category}::Function_{category}."
                f"{function_member(function)}:Case.{test['testcaseid']}"
            )
            conditions.append(_pass_condition(test, ref))
        address = (
            f"TestCube_{category}::Function_{category}."
            f"{function_member(function)}:Case.PassFail"
        )
        lines.append(f"rule {address} = AND({','.join(conditions)})")

    lines.extend(["", "calc", "", "# AUTOMATABLE ASSERTIONS"])
    for test in tests:
        category = test["category"]
        ref = (
            f"TestCube_{category}::@.value:Function_{category}."
            f"{function_member(test['function'])}:Case.{test['testcaseid']}"
        )
        message = test["description"].replace('"', "'")
        if test["assertion"].lower() == "almost_equals":
            expected = float(test["expected"])
            tolerance = _absolute_tolerance(expected, test["tolerance"])
            lines.append(f'assert {ref} >= {expected - tolerance:.17g} "{message} lower bound"')
            lines.append(f'assert {ref} <= {expected + tolerance:.17g} "{message} upper bound"')
        elif test["assertion"].lower() == "equals":
            literal = _assert_literal(test)
            if literal is not None:
                lines.append(f'assert {ref} == {literal} "{message}"')
            else:
                lines.append(f"# TYPED_ASSERT {ref} == {test['expected']} | {message}")
        else:
            lines.append(
                f"# TYPED_ASSERT {ref} is {test['expected_type']} "
                f"{test['expected']} via {test['assertion']} | {message}"
            )

    lines.append('\necho "=== OM CORE CONFORMANCE DASHBOARD EXECUTED ==="')
    with open(output_openm_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    generate_unified_openm("tests/test-matrix.csv", "tests/test_suite_unified.openm")
