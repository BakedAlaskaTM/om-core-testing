from __future__ import annotations

from pathlib import Path

from lib_tui.app import _ScriptTranscript, _script_file_arg


def test_script_file_arg_matches_source_and_run_commands():
    assert _script_file_arg("source tests/example.openm") == "tests/example.openm"
    assert _script_file_arg('source "tests/my example.openm"') == "tests/my example.openm"
    assert _script_file_arg("run tests/example.openm") == "tests/example.openm"
    assert _script_file_arg('run "tests/my example.openm"') == "tests/my example.openm"
    assert _script_file_arg("echo source tests/example.openm") is None
    assert _script_file_arg("source") is None
    assert _script_file_arg("run") is None


def test_script_transcript_writes_timestamped_log(tmp_path: Path):
    transcript = _ScriptTranscript("tests/My script.openm", tmp_path)
    transcript.write_line("Sourcing My script.openm")
    transcript.write_line("Executed 4 commands")
    path = transcript.close()

    assert path.parent == tmp_path
    assert path.name.startswith("My_script_")
    assert path.suffix == ".log"
    content = path.read_text(encoding="utf-8")
    assert "TUI OpenM script: tests/My script.openm" in content
    assert "Sourcing My script.openm" in content
    assert "Executed 4 commands" in content
    assert "Started:" in content
    assert "Finished:" in content
