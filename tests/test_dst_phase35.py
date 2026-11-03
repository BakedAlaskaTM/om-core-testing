from __future__ import annotations

import contextlib
import io
import subprocess
import sys
from dataclasses import replace

import pytest

from lib_dst.executor import create_trace_runtime, execute_trace, report_runtime
from lib_dst.faults import FaultDecision, with_faults
from lib_dst.generator import generate_trace
from lib_dst.schemas import TraceOperation, TraceTranscript
from lib_dst.script_export import (
    ScriptExportError,
    export_openm_script,
    write_openm_script,
)
from lib_dst.scheduling import generate_scheduler_scenario, with_scheduler
from lib_dst.shrinker import shrink_failure
from lib_runtime.lifecycle import reset_runtime_state
from lib_scripting.script_import import import_openm_script


def _round_trip(trace: TraceTranscript, tmp_path):
    with contextlib.redirect_stdout(io.StringIO()):
        expected = execute_trace(trace)
    script_path = write_openm_script(trace, tmp_path / "trace.openm")
    runtime = create_trace_runtime(trace)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            imported = import_openm_script(runtime.session, script_path)
        actual = report_runtime(runtime, trace)
        views = runtime.session.query("view_list").get("views", [])
        return expected, actual, imported, views
    finally:
        reset_runtime_state(runtime)


def test_export_is_byte_deterministic_and_preserves_operation_order() -> None:
    trace = generate_trace(0)

    first = export_openm_script(trace)
    second = export_openm_script(TraceTranscript.from_json(trace.to_json()))

    assert first == second
    assert f"# Trace SHA-256: {trace.sha256()}" in first
    assert "# Master seed: 0" in first
    positions = [
        first.index(f"# Trace step {operation.step}: {operation.kind}")
        for operation in trace.operations
    ]
    assert positions == sorted(positions)
    assert "rule $Calc::Line.Result = " in first
    assert "view DST_Trace_" in first


@pytest.mark.parametrize("seed", (0, 1, 4, 5, 13, 16))
def test_exported_script_matches_real_trace_semantics(seed: int, tmp_path) -> None:
    trace = generate_trace(seed)

    expected, actual, imported, views = _round_trip(trace, tmp_path)

    assert imported.success
    assert actual.oracle_passed
    assert actual.oracle_expected == expected.oracle_expected
    assert actual.oracle_actual == expected.oracle_actual
    assert actual.observation.to_json() == expected.observation.to_json()
    assert any(view["name"].startswith("DST_Trace_") for view in views)


def test_cell_error_export_uses_visual_annotation_not_invalid_assertion() -> None:
    script = export_openm_script(generate_trace(5))

    assert "#DIV/0!" in script
    assert "script assertions cannot compare CellError values" in script
    assert "assert Calc::" not in script


def test_scheduler_policy_is_documented_but_not_scripted() -> None:
    trace = with_scheduler(generate_trace(2), generate_scheduler_scenario(2, "delay"))

    script = export_openm_script(trace)

    assert "Harness-only scheduler policy (not simulated)" in script
    assert "request.command" in script


def test_numeric_export_assertion_failure_is_reported_by_importer(tmp_path) -> None:
    trace = generate_trace(0)
    lines = export_openm_script(trace).splitlines()
    assertion = next(
        index for index, line in enumerate(lines) if line.startswith("assert Calc::")
    )
    lines[assertion] = (
        'assert Calc::@.value:Line.Result >= 999 "Forced oracle mismatch"'
    )
    script = "\n".join(lines) + "\n"
    path = tmp_path / "wrong.openm"
    path.write_text(script, encoding="utf-8")
    runtime = create_trace_runtime(trace)
    try:
        with contextlib.redirect_stdout(io.StringIO()):
            result = import_openm_script(runtime.session, path)
        assert not result.success
        assert any("Command reported failure" in error for error in result.errors)
    finally:
        reset_runtime_state(runtime)


def test_minimized_failure_exports_semantics_and_fault_as_comment(tmp_path) -> None:
    failing = with_faults(
        generate_trace(7),
        [FaultDecision("persistence", "save", 1, "raise")],
    )
    with contextlib.redirect_stdout(io.StringIO()):
        minimized = shrink_failure(failing).minimized

    script = export_openm_script(minimized)
    expected, actual, imported, _ = _round_trip(minimized, tmp_path)

    assert "Harness-only persistence faults (not simulated)" in script
    assert imported.success
    assert actual.observation.to_json() == expected.observation.to_json()


def test_export_rejects_invalid_or_unsupported_trace() -> None:
    trace = generate_trace(0)
    operations = list(trace.operations)
    operations[-1] = TraceOperation(
        operations[-1].step, "unsupported_mutation", "runtime", {}
    )
    broken = replace(trace, operations=tuple(operations))

    with pytest.raises(ScriptExportError, match="Unsupported operation"):
        export_openm_script(broken)


def test_export_rejects_ast_rule_disagreement() -> None:
    trace = generate_trace(0)
    operations = []
    for operation in trace.operations:
        if operation.kind == "set_rule":
            args = dict(operation.args)
            args["expression"] = "1"
            operation = replace(operation, args=args)
        operations.append(operation)

    with pytest.raises(ScriptExportError, match="Serialized AST"):
        export_openm_script(replace(trace, operations=tuple(operations)))


def test_worker_exports_exact_script(tmp_path) -> None:
    trace = generate_trace(3)
    completed = subprocess.run(
        [sys.executable, "-m", "lib_dst.worker", "--export-script-stdin"],
        input=trace.to_json(),
        text=True,
        capture_output=True,
        check=True,
    )

    assert completed.stdout == export_openm_script(trace)
    assert completed.stderr == ""

    output = tmp_path / "worker.openm"
    written = subprocess.run(
        [
            sys.executable,
            "-m",
            "lib_dst.worker",
            "--export-script-stdin",
            str(output),
        ],
        input=trace.to_json(),
        text=True,
        capture_output=True,
        check=True,
    )
    assert written.stdout == ""
    assert written.stderr == ""
    assert output.read_text(encoding="utf-8") == export_openm_script(trace)


def test_gui_import_surface_uses_shared_script_path() -> None:
    from lib_gui.actions import MainWindowActions
    from lib_gui.app import MainWindow

    assert "act_import_script" in MainWindowActions.__annotations__
    assert callable(MainWindow.import_openm_script)
    assert callable(MainWindow._on_import_script)
