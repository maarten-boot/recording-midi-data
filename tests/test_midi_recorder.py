"""Tests for the pure parts of midi_recorder (no MIDI hardware needed)."""

import logging
import queue
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from pathlib import Path

import mido
import pytest

import midi_recorder as m


def _events() -> list[m.Event]:
    return [
        (10.0, "A", mido.Message("note_on", note=60, velocity=90)),
        (10.0005, "B", mido.Message("control_change", control=64, value=127)),
        (10.5, "A", mido.Message("aftertouch", value=40)),
        (11.2, "A", mido.Message("note_off", note=60)),
    ]


def test_save_filename_and_layout(tmp_path: Path) -> None:
    path = m.save(_events(), datetime(2026, 10, 4, 21, 35, 12), tmp_path)
    assert path.name == "2026-10-04_21-35-12.mid"

    mid = mido.MidiFile(path)
    assert mid.type == 1
    assert mid.ticks_per_beat == m.TICKS_PER_BEAT
    assert len(mid.tracks) == 3  # tempo track + one per port
    assert round(mid.length, 3) == 1.2


def test_ticks_per_second_is_one_ms() -> None:
    assert m.TICKS_PER_SECOND == 1000


def test_save_delta_times(tmp_path: Path) -> None:
    path = m.save(_events(), datetime(2026, 1, 1), tmp_path)
    track_a = mido.MidiFile(path).tracks[1]
    assert [(x.type, x.time) for x in track_a] == [
        ("track_name", 0),
        ("note_on", 0),
        ("aftertouch", 500),
        ("note_off", 700),
        ("end_of_track", 0),
    ]


def _active(s: m.HoldState) -> bool:
    # a function call, so mypy does not narrow `s.active` across the updates below
    return s.active


def test_hold_state_notes_and_sustain() -> None:
    s = m.HoldState()
    assert not _active(s)

    s.update("A", mido.Message("note_on", note=60, velocity=90))
    assert _active(s)
    s.update("A", mido.Message("note_on", note=60, velocity=0))  # note_on vel 0 == off
    assert not _active(s)

    s.update("A", mido.Message("control_change", control=64, value=127))
    assert _active(s)
    s.update("A", mido.Message("control_change", control=64, value=0))
    assert not _active(s)


@pytest.fixture
def clean_logger() -> Iterator[None]:
    yield
    for handler in list(m.logger.handlers):  # do not leak open file handlers between tests
        m.logger.removeHandler(handler)
        handler.close()


@pytest.mark.usefixtures("clean_logger")
def test_logging_goes_to_stderr_and_file(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    log_file = tmp_path / "x.log"
    m.setup_logging("INFO", log_file)
    m.logger.info("hello %s", "world")
    m.logger.debug("hidden")

    assert "INFO hello world" in capsys.readouterr().err
    text = log_file.read_text(encoding="utf-8")
    assert "INFO hello world" in text
    assert "hidden" not in text


@pytest.mark.usefixtures("clean_logger")
def test_logging_stderr_only_and_level(capsys: pytest.CaptureFixture[str]) -> None:
    m.setup_logging("WARNING", None)
    m.logger.info("quiet")
    m.logger.warning("loud")

    err = capsys.readouterr().err
    assert "loud" in err
    assert "quiet" not in err
    assert len(m.logger.handlers) == 1
    assert m.logger.level == logging.WARNING


def _wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _note(kind: str, note: int = 60) -> mido.Message:
    return mido.Message(kind, note=note, velocity=90 if kind == "note_on" else 0)


def test_elapsed_text() -> None:
    s = m.Status()
    now = datetime(2026, 10, 5, 12, 0, 0)
    assert s.elapsed_text(now) == ""
    s.started = datetime(2026, 10, 5, 10, 58, 37)
    assert s.elapsed_text(now) == "01:01:23"


def test_journal_roundtrip_and_recovery(tmp_path: Path) -> None:
    started = datetime(2026, 10, 5, 9, 40, 4)
    journal_path = tmp_path / "2026-10-05_09-40-04.journal"
    journal = m.Journal(journal_path, started, t0=100.0)
    journal.append((100.0, "A", _note("note_on")))
    journal.append((100.25, "A", _note("note_off")))
    journal.close()
    with journal_path.open("a", encoding="utf-8") as fh:
        fh.write('[0.5, "A", "90 3')  # a write cut off by a crash

    recovered = m.recover_journals(tmp_path)

    assert [p.name for p in recovered] == ["2026-10-05_09-40-04.mid"]
    assert not list(tmp_path.glob("*.journal"))
    track = mido.MidiFile(recovered[0]).tracks[1]
    assert [(x.type, x.time) for x in track if x.type.startswith("note")] == [("note_on", 0), ("note_off", 250)]


def test_recovery_does_not_overwrite_existing_file(tmp_path: Path) -> None:
    started = datetime(2026, 10, 5, 9, 40, 4)
    (tmp_path / "2026-10-05_09-40-04.mid").write_bytes(b"keep me")
    journal = m.Journal(tmp_path / "2026-10-05_09-40-04.journal", started, t0=0.0)
    journal.append((0.0, "A", _note("note_on")))
    journal.close()

    recovered = m.recover_journals(tmp_path)

    assert [p.name for p in recovered] == ["2026-10-05_09-40-04_recovered.mid"]
    assert (tmp_path / "2026-10-05_09-40-04.mid").read_bytes() == b"keep me"


def test_recovery_discards_empty_and_keeps_unreadable_journals(tmp_path: Path) -> None:
    empty = m.Journal(tmp_path / "a.journal", datetime(2026, 1, 1), t0=0.0)
    empty.close()
    (tmp_path / "b.journal").write_text("not json at all\n", encoding="utf-8")

    assert m.recover_journals(tmp_path) == []
    assert not (tmp_path / "a.journal").exists()
    assert (tmp_path / "b.journal").exists()  # left for a human to look at


def test_journal_in_missing_folder_does_not_raise(tmp_path: Path) -> None:
    journal = m.Journal(tmp_path / "nope" / "x.journal", datetime(2026, 1, 1), t0=0.0)
    journal.append((0.0, "A", _note("note_on")))
    journal.remove()


def test_session_loop_saves_after_idle(tmp_path: Path) -> None:
    q: queue.Queue[m.Event] = queue.Queue()
    status = m.Status()
    stop = threading.Event()
    q.put((time.perf_counter() - 1000, "A", _note("note_on")))  # an old event: the idle time is already over

    thread = threading.Thread(target=m.session_loop, args=(q, tmp_path, 0.1, 1.0, status, stop))
    thread.start()
    try:
        assert _wait_for(lambda: len(status.saved) == 1)
    finally:
        stop.set()
        thread.join(5.0)

    assert not thread.is_alive()
    assert [p.suffix for p in tmp_path.iterdir()] == [".mid"]  # the journal is gone
    assert status.state == "idle"
    assert status.events == 0


def test_session_loop_saves_take_in_progress_on_stop(tmp_path: Path) -> None:
    q: queue.Queue[m.Event] = queue.Queue()
    status = m.Status()
    stop = threading.Event()
    thread = threading.Thread(target=m.session_loop, args=(q, tmp_path, 60.0, 120.0, status, stop))
    thread.start()
    try:
        for note in (60, 62, 64):
            q.put((time.perf_counter(), "A", _note("note_on", note)))
        assert _wait_for(lambda: status.events == 3)
        assert status.state == "recording"
        assert list(tmp_path.glob("*.journal"))  # the crash-safety journal exists while recording
    finally:
        stop.set()
        thread.join(5.0)

    assert not thread.is_alive()
    assert len(status.saved) == 1
    track = mido.MidiFile(status.saved[0]).tracks[1]
    assert [x.note for x in track if isinstance(x, mido.Message) and x.type == "note_on"] == [60, 62, 64]
    assert not list(tmp_path.glob("*.journal"))


def test_list_ports_reports_backend_errors_cleanly(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    def broken() -> list[str]:
        raise SystemError("no ALSA")

    monkeypatch.setattr(mido, "get_input_names", broken)

    assert m.list_ports() == 1
    err = capsys.readouterr().err
    assert "error: cannot list MIDI input ports: no ALSA" in err
    assert "Traceback" not in err


def test_list_ports_prints_names(monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> None:
    monkeypatch.setattr(mido, "get_input_names", lambda: ["Piano 20:0", "Synth 24:0"])

    assert m.list_ports() == 0
    assert capsys.readouterr().out.splitlines() == ["Piano 20:0", "Synth 24:0"]
