from __future__ import annotations

import csv
from pathlib import Path

from lib_command.core.executor import get_executor
from lib_openm.api import Engine
from lib_openm.model import demo_workspace
from lib_repl import OpenMREPL
from tests.generate_tests import generate_unified_openm
from tests.helpers import _MockSession, get_cell_by_dim


def test_generates_one_passfail_rule_per_function(tmp_path: Path):
    output = tmp_path / "generated.openm"
    generate_unified_openm("tests/test-matrix.csv", str(output))
    generated = output.read_text(encoding="utf-8")

    with Path("tests/test-matrix.csv").open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    functions = {(row["Category"], row["Function"]) for row in rows}

    passfail_rules = [
        line for line in generated.splitlines()
        if line.startswith("rule ") and ":Case.PassFail = " in line
    ]
    case_ids = sorted(row["TestCaseID"] for row in rows)
    assert f"dim Case {' '.join(case_ids)} PassFail" in generated
    assert "cube MockCube Row Column" in generated
    assert "rule MockCube::Row.Periodic:Column.C1 = -100" in generated
    assert len(passfail_rules) == len(functions)

    for category, function in functions:
        prefix = (
            f"rule TestCube_{category}::Function_{category}."
            f"FN_{function}:Case.PassFail = AND("
        )
        matching = [line for line in passfail_rules if line.startswith(prefix)]
        assert len(matching) == 1
        for row in (r for r in rows if r["Category"] == category and r["Function"] == function):
            assert f":Case.{row['TestCaseID']}" in matching[0]


def test_passfail_rules_handle_equals_almost_equals_text_and_error(tmp_path: Path):
    manifest = tmp_path / "matrix.csv"
    manifest.write_text(
        "Standard,Version,Profile,Category,Function,SpecificationSection,TestCaseID,"
        "Formula,ExpectedType,Expected,Tolerance,Assertion,Description\n"
        'IEEE 754,2019,Core,Demo,FN,§1,TC_DEMO_FN_001,1,NUMBER,1,0,EQUALS,exact\n'
        'ISO/IEC 10646,2020,Full,Demo,FN,§2,TC_DEMO_FN_002,"""a""",STRING,a,0,EQUALS,text\n'
        'ISO/IEC 10967-2,2001,Core,Demo,FN,§3,TC_DEMO_FN_003,1,NUMBER,1,0.1,ALMOST_EQUALS,tolerance\n'
        'IEEE 754,2019,Strict,Demo,FN,§9,TC_DEMO_FN_004,1/0,ERROR,#DIV/0!,0,ERROR_MATCH,error\n',
        encoding="utf-8",
    )
    output = tmp_path / "generated.openm"
    generate_unified_openm(str(manifest), str(output))
    generated = output.read_text(encoding="utf-8")
    assert "cube MockCube Row Column" not in generated
    passfail = next(
        line for line in generated.splitlines()
        if ":Case.PassFail = " in line
    )

    assert "IFERROR(TestCube_Demo::Function_Demo.FN_FN:Case.TC_DEMO_FN_001 == 1,FALSE())" in passfail
    assert 'IFERROR(TestCube_Demo::Function_Demo.FN_FN:Case.TC_DEMO_FN_002 == "a",FALSE())' in passfail
    assert "Case.TC_DEMO_FN_003 >= 0.90000000000000002" in passfail
    assert "Case.TC_DEMO_FN_003 <= 1.1000000000000001" in passfail
    assert 'IFERROR(TestCube_Demo::Function_Demo.FN_FN:Case.TC_DEMO_FN_004,"__OPENM_EXPECTED_ERROR__")' in passfail

    repl = OpenMREPL(session=_MockSession(executor=get_executor()))
    workspace = demo_workspace()
    engine = Engine(workspace)
    repl.session.context.engine = engine
    repl.session.context.workspace = workspace
    for line in output.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        result = repl.onecmd(line)
        assert result is not True
        if line == "calc":
            break

    cube = engine.find_cube_by_name("TestCube_Demo")
    address = {}
    item_by_dimension = {"Function_Demo": "FN_FN", "Case": "PassFail"}
    for dimension_id in cube.dimension_ids:
        dimension = engine.require_dimension_by_id(dimension_id)
        if dimension.name == "@":
            continue
        item_name = item_by_dimension[dimension.name]
        address[dimension_id] = next(item.id for item in dimension.items if item.name == item_name)
    # Logical cell values are stored numerically by the cube engine.
    assert get_cell_by_dim(engine, cube.id, address) == 1.0


def test_converts_ulp_tolerance_to_numeric_bounds(tmp_path: Path):
    manifest = tmp_path / "matrix.csv"
    manifest.write_text(
        "Standard,Version,Profile,Category,Function,SpecificationSection,TestCaseID,"
        "Formula,ExpectedType,Expected,Tolerance,Assertion,Description\n"
        "IEEE 754,2019,Strict,Math,PI,§9,TC_MATH_PI_001,PI(),NUMBER,"
        "3.141592653589793,1.5 ULP,ALMOST_EQUALS,ulp\n",
        encoding="utf-8",
    )
    output = tmp_path / "generated.openm"
    generate_unified_openm(str(manifest), str(output))
    generated = output.read_text(encoding="utf-8")

    assert "1.5 ULP" not in generated
    assert "Case.TC_MATH_PI_001 >= " in generated
    assert "Case.TC_MATH_PI_001 <= " in generated
