"""Frozen behaviour of the command line: options, defaults, logging set-up, list_ports and main()."""

import argparse
import logging
import logging.handlers
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import ClassVar

import mido
import pytest

import midi_recorder as m


@pytest.fixture
def clean_logger() -> Iterator[None]:
    yield
    for handler in list(m.logger.handlers):
        m.logger.removeHandler(handler)
        handler.close()
    m.logger.setLevel(logging.NOTSET)
    m.logger.propagate = True


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser()
    m.add_common_arguments(ap)
    return ap


class TestOptions:
    def test_defaults(self) -> None:
        args = _parser().parse_args([])
        assert (args.outdir, args.idle, args.max_hold) == (".", 30.0, 120.0)
        assert (args.log_level, args.log_file, args.no_log_file) == ("INFO", None, False)

    def test_all_options(self) -> None:
        args = _parser().parse_args(["-o", "rec", "--idle", "5", "--max-hold", "9.5", "--log-level", "debug", "--log-file", "x.log", "--no-log-file"])
        assert (args.outdir, args.idle, args.max_hold) == ("rec", 5.0, 9.5)
        assert (args.log_level, args.log_file, args.no_log_file) == ("DEBUG", Path("x.log"), True)

    def test_long_form_of_outdir(self) -> None:
        assert _parser().parse_args(["--outdir", "x"]).outdir == "x"

    def test_unknown_log_level_is_rejected(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit) as exc:
            _parser().parse_args(["--log-level", "loud"])
        assert exc.value.code == 2
        assert "invalid choice" in capsys.readouterr().err

    def test_help_shows_the_real_defaults(self, capsys: pytest.CaptureFixture[str]) -> None:
        with pytest.raises(SystemExit):
            _parser().parse_args(["--help"])
        out = capsys.readouterr().out
        assert "(default 30.0)" in out
        assert "(default 120.0)" in out
        assert "<outdir>/midi_recorder.log" in out


class TestLogging:
    def test_handlers_levels_and_rotation(self, tmp_path: Path, clean_logger: None) -> None:
        m.setup_logging("WARNING", tmp_path / "x.log")
        kinds = sorted(type(h).__name__ for h in m.logger.handlers)
        assert kinds == ["RotatingFileHandler", "StreamHandler"]
        rotating = next(h for h in m.logger.handlers if isinstance(h, logging.handlers.RotatingFileHandler))
        assert (rotating.maxBytes, rotating.backupCount) == (1_000_000, 3)
        assert m.logger.level == logging.WARNING
        assert m.logger.propagate is False

    def test_calling_it_twice_does_not_duplicate_output(self, tmp_path: Path, clean_logger: None, capsys: pytest.CaptureFixture[str]) -> None:
        m.setup_logging("INFO", tmp_path / "x.log")
        m.setup_logging("INFO", tmp_path / "x.log")
        assert len(m.logger.handlers) == 2
        m.logger.info("once")
        assert capsys.readouterr().err.count("once") == 1
        assert (tmp_path / "x.log").read_text(encoding="utf-8").count("once") == 1

    def test_a_missing_stderr_is_harmless(self, tmp_path: Path, clean_logger: None, monkeypatch: pytest.MonkeyPatch) -> None:
        """A Windows gui-script (pythonw) has no console: sys.stderr is None. Logging must not fail, and the file log must still work."""
        monkeypatch.setattr(sys, "stderr", None)
        m.setup_logging("INFO", tmp_path / "x.log")
        m.logger.info("still here")
        assert "still here" in (tmp_path / "x.log").read_text(encoding="utf-8")

    def test_line_format(self, tmp_path: Path, clean_logger: None) -> None:
        m.setup_logging("INFO", tmp_path / "x.log")
        m.logger.warning("careful %d", 7)
        line = (tmp_path / "x.log").read_text(encoding="utf-8").strip()
        assert line[4] == "-" and line[10] == " " and line[13] == ":"  # 2026-10-05 21:35:12
        assert line.endswith(" WARNING careful 7")


class TestPrepare:
    def _args(self, *argv: str) -> argparse.Namespace:
        return _parser().parse_args(list(argv))

    def test_creates_the_folder_and_logs_into_it_by_default(self, tmp_path: Path, clean_logger: None) -> None:
        out = m.prepare(self._args("-o", str(tmp_path / "a" / "b")))
        assert out == tmp_path / "a" / "b"
        m.logger.info("hello")
        assert "hello" in (out / "midi_recorder.log").read_text(encoding="utf-8")

    def test_no_log_file(self, tmp_path: Path, clean_logger: None) -> None:
        m.prepare(self._args("-o", str(tmp_path), "--no-log-file"))
        m.logger.info("hello")
        assert not (tmp_path / "midi_recorder.log").exists()
        assert [type(h).__name__ for h in m.logger.handlers] == ["StreamHandler"]

    def test_explicit_log_file(self, tmp_path: Path, clean_logger: None) -> None:
        m.prepare(self._args("-o", str(tmp_path), "--log-file", str(tmp_path / "other.log")))
        m.logger.info("hello")
        assert (tmp_path / "other.log").exists()
        assert not (tmp_path / "midi_recorder.log").exists()

    def test_log_level_is_applied(self, tmp_path: Path, clean_logger: None) -> None:
        m.prepare(self._args("-o", str(tmp_path), "--log-level", "error"))
        assert m.logger.level == logging.ERROR


class TestListPorts:
    def test_backend_failure_gives_a_short_error_not_a_traceback(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        def broken() -> list[str]:
            raise SystemError("no ALSA")

        monkeypatch.setattr(mido, "get_input_names", broken)
        assert m.list_ports() == 1
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "error: cannot list MIDI input ports: no ALSA" in captured.err
        assert "hint:" in captured.err
        assert "Traceback" not in captured.err

    def test_prints_one_name_per_line(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(mido, "get_input_names", lambda: ["Piano 20:0", "Synth 24:0"])
        assert m.list_ports() == 0
        assert capsys.readouterr().out.splitlines() == ["Piano 20:0", "Synth 24:0"]

    def test_no_ports_is_not_an_error(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(mido, "get_input_names", lambda: [])
        assert m.list_ports() == 0
        captured = capsys.readouterr()
        assert captured.out == ""
        assert "no MIDI input ports found" in captured.err

    def test_ports_are_listed_unfiltered(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(mido, "get_input_names", lambda: ["Midi Through 14:0", "Piano"])
        m.list_ports()
        assert capsys.readouterr().out.splitlines() == ["Midi Through 14:0", "Piano"]  # --list shows what exists, the recorder skips loopbacks


class FakeRecorder:
    instances: ClassVar[list["FakeRecorder"]] = []

    def __init__(self, outdir: Path, idle: float, max_hold: float) -> None:
        self.args = (outdir, idle, max_hold)
        self.calls: list[str] = []
        FakeRecorder.instances.append(self)

    def start(self) -> None:
        self.calls.append("start")

    def stop(self) -> None:
        self.calls.append("stop")


class TestMain:
    def test_list_option_lists_and_exits(self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
        monkeypatch.setattr(sys, "argv", ["midi-recorder", "--list"])
        monkeypatch.setattr(mido, "get_input_names", lambda: ["Piano"])
        assert m.main() == 0
        assert capsys.readouterr().out == "Piano\n"

    def test_runs_until_ctrl_c_then_stops_the_recorder(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_logger: None) -> None:
        FakeRecorder.instances.clear()
        monkeypatch.setattr(sys, "argv", ["midi-recorder", "-o", str(tmp_path / "rec"), "--idle", "12", "--max-hold", "34"])
        monkeypatch.setattr(m, "Recorder", FakeRecorder)

        def interrupt(seconds: float) -> None:
            raise KeyboardInterrupt

        monkeypatch.setattr(time, "sleep", interrupt)

        assert m.main() == 0

        (recorder,) = FakeRecorder.instances
        assert recorder.args == (tmp_path / "rec", 12.0, 34.0)
        assert recorder.calls == ["start", "stop"]
        assert "midi recorder active" in (tmp_path / "rec" / "midi_recorder.log").read_text(encoding="utf-8")

    def test_recorder_is_stopped_even_if_something_else_goes_wrong(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clean_logger: None) -> None:
        FakeRecorder.instances.clear()
        monkeypatch.setattr(sys, "argv", ["midi-recorder", "-o", str(tmp_path), "--no-log-file"])
        monkeypatch.setattr(m, "Recorder", FakeRecorder)

        def explode(seconds: float) -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(time, "sleep", explode)

        with pytest.raises(RuntimeError, match="boom"):
            m.main()
        assert FakeRecorder.instances[0].calls == ["start", "stop"]
