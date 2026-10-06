"""Frozen behaviour of the status window. Needs tkinter; the window tests also need a display and are skipped without one."""

import contextlib
import logging
import re
import subprocess
import sys
from collections.abc import Iterator
from datetime import datetime, timedelta
from pathlib import Path

import pytest

pytest.importorskip("tkinter", reason="tkinter is not installed")

import tkinter as tk

import midi_recorder as m
import midi_recorder_gui as g


@pytest.fixture
def root() -> Iterator[tk.Tk]:
    try:
        window = tk.Tk()
    except tk.TclError as exc:
        pytest.skip(f"no display: {exc}")
    window.withdraw()
    yield window
    with contextlib.suppress(tk.TclError):  # the test may already have closed it
        window.destroy()


@pytest.fixture
def app(root: tk.Tk, tmp_path: Path) -> g.App:
    return g.App(root, m.Recorder(tmp_path, idle=30.0))


def _selection(listbox: tk.Listbox) -> tuple[int, ...]:
    return tuple(listbox.curselection())  # type: ignore[no-untyped-call]


def _texts(listbox: tk.Listbox) -> tuple[str, ...]:
    return tuple(listbox.get(0, "end"))


class TestWindow:
    def test_no_input_connected(self, app: g.App) -> None:
        assert app.state_var.get() == "No MIDI input found"
        assert app.state_label.cget("fg") == g.COLOR_WARNING
        assert "picked up automatically" in app.detail_var.get()
        assert _texts(app.ports_list) == ()

    def test_ready_when_an_input_is_open(self, app: g.App) -> None:
        app.recorder.status.ports = ("Piano 20:0", "Synth 24:0")
        app.refresh()
        assert app.state_var.get() == "Ready \u2013 waiting for MIDI"
        assert app.state_label.cget("fg") == g.COLOR_READY
        assert app.detail_var.get() == "Recording stops after 30 s of silence"
        assert _texts(app.ports_list) == ("Piano 20:0", "Synth 24:0")

    def test_recording_shows_events_and_elapsed_time(self, app: g.App) -> None:
        status = app.recorder.status
        status.ports = ("Piano",)
        status.state = "recording"
        status.events = 12
        status.started = datetime.now() - timedelta(seconds=75)
        app.refresh()
        assert app.state_var.get() == "\u25cf RECORDING"
        assert app.state_label.cget("fg") == g.COLOR_RECORDING
        assert re.fullmatch(r"12 events {3}00:01:1[5-7]", app.detail_var.get())

    def test_recording_without_inputs_still_shows_recording(self, app: g.App) -> None:
        app.recorder.status.state = "recording"  # e.g. the keyboard was unplugged mid-take
        app.refresh()
        assert app.state_var.get() == "\u25cf RECORDING"

    def test_recent_files_are_listed_newest_first_by_name(self, app: g.App, tmp_path: Path) -> None:
        app.recorder.status.saved = (tmp_path / "2026-10-05_09-00-00.mid", tmp_path / "2026-10-05_10-00-00.mid")
        app.refresh()
        assert _texts(app.files_list) == ("2026-10-05_10-00-00.mid", "2026-10-05_09-00-00.mid")

    def test_lists_are_only_rewritten_when_their_content_changes(self, app: g.App, tmp_path: Path) -> None:
        status = app.recorder.status
        status.saved = (tmp_path / "a.mid",)
        app.refresh()
        app.files_list.selection_set(0)

        app.refresh()
        assert _selection(app.files_list) == (0,)  # unchanged content: the user's selection survives

        status.saved = (*status.saved, tmp_path / "b.mid")
        app.refresh()
        assert _texts(app.files_list) == ("b.mid", "a.mid")
        assert _selection(app.files_list) == ()  # changed content: the list was rebuilt

    def test_the_output_folder_is_shown(self, app: g.App, tmp_path: Path) -> None:
        labels = [w.cget("text") for w in app.root.winfo_children()[0].winfo_children() if w.winfo_class() == "TLabel"]
        assert str(tmp_path.resolve()) in labels

    def test_close_destroys_the_window(self, app: g.App, root: tk.Tk) -> None:
        app.close()
        with pytest.raises(tk.TclError):
            root.winfo_exists()


class TestMain:
    def test_no_display_gives_a_clean_error_and_starts_nothing(
        self, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
    ) -> None:
        def no_display() -> tk.Tk:
            raise tk.TclError("no display name and no $DISPLAY environment variable")

        started: list[object] = []
        monkeypatch.setattr(sys, "argv", ["midi-recorder-gui", "-o", str(tmp_path)])
        monkeypatch.setattr(tk, "Tk", no_display)
        monkeypatch.setattr(m.Recorder, "start", lambda self: started.append(self))

        assert g.main() == 1
        assert "error: cannot open a window: no display" in capsys.readouterr().err
        assert started == []

    def test_window_lifecycle_starts_and_stops_the_recorder(self, root: tk.Tk, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
        calls: list[str] = []

        class Quick(tk.Tk):
            def mainloop(self, n: int = 0) -> None:  # open and immediately close, like a user who closes the window
                calls.append("mainloop")
                self.destroy()

        monkeypatch.setattr(sys, "argv", ["midi-recorder-gui", "-o", str(tmp_path / "rec"), "--idle", "7", "--no-log-file"])
        monkeypatch.setattr(tk, "Tk", Quick)
        monkeypatch.setattr(m.Recorder, "start", lambda self: calls.append("start"))
        monkeypatch.setattr(m.Recorder, "stop", lambda self: calls.append("stop"))

        assert g.main() == 0
        assert calls == ["start", "mainloop", "stop"]
        assert (tmp_path / "rec").is_dir()


class TestOpenFolder:
    """No display needed: the platform branches are chosen by sys.platform."""

    def _run(self, monkeypatch: pytest.MonkeyPatch, platform: str) -> list[list[str]]:
        calls: list[list[str]] = []
        monkeypatch.setattr(sys, "platform", platform)
        monkeypatch.setattr(subprocess, "run", lambda cmd, **kw: calls.append(list(cmd)))
        g.open_folder(Path("/some/dir"))
        return calls

    def test_linux_uses_xdg_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._run(monkeypatch, "linux") == [["xdg-open", str(Path("/some/dir"))]]

    def test_macos_uses_open(self, monkeypatch: pytest.MonkeyPatch) -> None:
        assert self._run(monkeypatch, "darwin") == [["open", str(Path("/some/dir"))]]

    def test_windows_uses_startfile(self, monkeypatch: pytest.MonkeyPatch) -> None:
        opened: list[Path] = []
        monkeypatch.setattr("os.startfile", opened.append, raising=False)
        assert self._run(monkeypatch, "win32") == []
        assert opened == [Path("/some/dir")]

    def test_a_missing_file_manager_is_logged_not_raised(self, monkeypatch: pytest.MonkeyPatch, log_records: list[logging.LogRecord]) -> None:
        def missing(cmd: list[str], **kw: object) -> None:
            raise FileNotFoundError("xdg-open")

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(subprocess, "run", missing)
        g.open_folder(Path("/some/dir"))
        assert any("cannot open folder" in r.getMessage() for r in log_records)
