"""Generate the human-readable OpenFormula conformance dashboard.

The CSV is the normative test manifest.  This generator deliberately leaves
errors unwrapped so the GUI displays the engine's real typed result.  OpenM's
script-level ``assert`` currently cannot compare errored cells, so those rows
are marked for a typed test harness instead of being weakened with IFERROR.
"""

import csv
import json
from collections import defaultdict


REQUIRED_COLUMNS = {
    "Standard", "Version", "Profile", "Category", "Function",
    "SpecificationSection", "TestCaseID", "Formula", "ExpectedType",
    "Expected", "Tolerance", "Assertion", "Description",
}


def function_member(function_name: str) -> str:
    return f"FN_{function_name}"


def _assert_literal(test: dict) -> str | None:
    expected = test["expected"]
    expected_type = test["expected_type"].lower()
    if expected_type in {"number", "integer", "logical"}:
        return expected
    # do_assert treats quoted content as the assertion message. Bare tokens are
    # safe for simple strings; empty/whitespace-bearing strings need pytest.
    if expected_type == "text" and expected and not any(c.isspace() for c in expected):
        return expected
    return None


def _rule_literal(test: dict) -> str:
    """Render a CSV expected value as an OpenM rule literal."""
    expected_type = test["expected_type"].lower()
    expected = test["expected"]
    if expected_type in {"number", "integer"}:
        return expected
    if expected_type == "logical":
        return f"{expected.upper()}()"
    if expected_type == "text":
        return json.dumps(expected, ensure_ascii=False)
    raise ValueError(f"Cannot render expected {expected_type} value as a rule literal")


def _pass_condition(test: dict, ref: str) -> str:
    """Build a Boolean rule expression for one manifest test case."""
    assertion = test["assertion"].lower()
    if assertion == "error":
        # The rule evaluator has IFERROR but no error-code inspection function,
        # so an Error assertion means that any typed cell error is expected.
        marker = json.dumps("__OPENM_EXPECTED_ERROR__")
        return f"IFERROR({ref},{marker}) == {marker}"
    if assertion == "tolerance":
        expected = float(test["expected"])
        tolerance = float(test["tolerance"])
        lower = f"{expected - tolerance:.17g}"
        upper = f"{expected + tolerance:.17g}"
        return (
            f"IFERROR(AND({ref} >= {lower},{ref} <= {upper}),FALSE())"
        )
    if assertion in {"exact", "property"}:
        return f"IFERROR({ref} == {_rule_literal(test)},FALSE())"
    raise ValueError(f"Unsupported assertion type: {test['assertion']}")


def generate_unified_openm(csv_file_path: str, output_openm_path: str):
    tests = []
    functions_by_category = defaultdict(set)
    tests_by_function = defaultdict(list)
    cases = set()

    with open(csv_file_path, newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        missing = REQUIRED_COLUMNS.difference(reader.fieldnames or ())
        if missing:
            raise ValueError(f"Missing CSV columns: {', '.join(sorted(missing))}")
        for row in reader:
            test = {key.lower(): value.strip() for key, value in row.items()}
            test["expected_type"] = test.pop("expectedtype")
            if test["standard"] != "OpenFormula":
                raise ValueError(f"Unsupported standard: {test['standard']}")
            tests.append(test)
            functions_by_category[test["category"]].add(test["function"])
            tests_by_function[(test["category"], test["function"])].append(test)
            cases.add(test["testcaseid"])

    lines = [
        "# ==============================================================================",
        "# OPENFORMULA 1.4 FUNCTION-LEVEL CONFORMANCE DASHBOARD",
        "# Generated from tests/test-matrix.csv; formulas use OM Core comma syntax.",
        "# Error rows retain raw errors and require typed-harness verification.",
        "# ==============================================================================\n",
        "dim Mock_Row R1 R2 R3",
        "dim Mock_Col C1 C2 C3",
        "cube MockCube Mock_Row Mock_Col",
        "view MockView = MockCube rows: Mock_Row cols: Mock_Col",
        "hval view_id=MockView row=0 col=0 value=10",
        "hval view_id=MockView row=0 col=1 value=20",
        "hval view_id=MockView row=0 col=2 value=50",
        "hval view_id=MockView row=1 col=0 value=30",
        "hval view_id=MockView row=1 col=1 value=40",
        "hval view_id=MockView row=1 col=2 value=100",
        'hval view_id=MockView row=2 col=0 value="Alpha"',
        'hval view_id=MockView row=2 col=1 value="Beta"',
        'hval view_id=MockView row=2 col=2 value="Gamma"\n',
        f"dim Case {' '.join(sorted(cases))} PassFail",
    ]

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
            f"# OpenFormula {test['version']} section {test['specificationsection']} "
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
        if test["assertion"].lower() == "tolerance":
            expected = float(test["expected"])
            tolerance = float(test["tolerance"])
            lines.append(f'assert {ref} >= {expected - tolerance:.17g} "{message} lower bound"')
            lines.append(f'assert {ref} <= {expected + tolerance:.17g} "{message} upper bound"')
        elif test["assertion"].lower() in {"exact", "property"}:
            literal = _assert_literal(test)
            if literal is not None:
                lines.append(f'assert {ref} == {literal} "{message}"')
            else:
                lines.append(f"# TYPED_ASSERT {ref} == {test['expected']} | {message}")
        else:
            lines.append(
                f"# TYPED_ASSERT {ref} is {test['expected_type']} "
                f"{test['expected']} | {message}"
            )

    lines.append('\necho "=== OPENFORMULA DASHBOARD EXECUTED ==="')
    with open(output_openm_path, "w", encoding="utf-8", newline="\n") as f:
        f.write("\n".join(lines))


if __name__ == "__main__":
    generate_unified_openm("tests/test-matrix.csv", "tests/test_suite_unified.openm")
