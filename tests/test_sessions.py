"""Frozen behaviour of session_loop: when a take starts, when it ends, what it leaves on disk."""

import json
import logging
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, tzinfo
from pathlib import Path

import mido
import pytest
from helpers import note, pedal, track_names, wait_for

import midi_recorder as m


class Loop:
    """session_loop running in a thread, with the queue, status and stop flag a test needs."""

    def __init__(self, outdir: Path, idle: float, max_hold: float) -> None:
        self.q: queue.Queue[m.Event] = queue.Queue()
        self.status = m.Status()
        self.stop = threading.Event()
        self.outdir = outdir
        self.thread = threading.Thread(target=m.session_loop, args=(self.q, outdir, idle, max_hold, self.status, self.stop))

    def put(self, msg: mido.Message, port: str = "A", age: float = 0.0) -> None:
        self.q.put((time.perf_counter() - age, port, msg))

    def mids(self) -> list[Path]:
        return sorted(self.outdir.glob("*.mid"))


@contextmanager
def running(outdir: Path, idle: float, max_hold: float) -> Iterator[Loop]:
    loop = Loop(outdir, idle, max_hold)
    loop.thread.start()
    try:
        yield loop
    finally:
        loop.stop.set()
        loop.thread.join(5.0)
        assert not loop.thread.is_alive()


def _snapshot(status: m.Status) -> tuple[str, int, datetime | None]:
    # a call, so mypy does not narrow the fields from asserts made earlier in the test
    return status.state, status.events, status.started


def _notes(path: Path) -> list[int]:
    return [x.note for t in mido.MidiFile(path).tracks for x in t if isinstance(x, mido.Message) and x.type == "note_on"]


def test_a_take_ends_after_the_idle_time(tmp_path: Path) -> None:
    with running(tmp_path, idle=0.2, max_hold=60.0) as loop:
        loop.put(note("note_on", 60))
        loop.put(note("note_off", 60))
        assert wait_for(lambda: len(loop.status.saved) == 1)
    assert [x.type for x in mido.MidiFile(loop.mids()[0]).tracks[1] if x.type.startswith("note")] == ["note_on", "note_off"]
    assert loop.status.state == "idle"


def test_status_shows_the_take_in_progress(tmp_path: Path) -> None:
    with running(tmp_path, idle=60.0, max_hold=120.0) as loop:
        before = datetime.now()
        loop.put(note("note_on", 60))
        loop.put(note("note_on", 62))
        assert wait_for(lambda: loop.status.events == 2)
        assert loop.status.state == "recording"
        assert loop.status.started is not None
        assert before <= loop.status.started <= datetime.now()
    assert _snapshot(loop.status) == ("idle", 0, None)


@pytest.mark.parametrize("start", [note("note_on", 60), pedal(127)], ids=["held note", "sustain pedal down"])
def test_a_take_stays_open_while_a_note_or_the_pedal_is_held(tmp_path: Path, start: mido.Message) -> None:
    release = note("note_off", 60) if start.type == "note_on" else pedal(0)
    with running(tmp_path, idle=0.1, max_hold=60.0) as loop:
        loop.put(start)
        time.sleep(0.6)  # six times the idle time
        assert loop.status.state == "recording"
        assert loop.mids() == []

        loop.put(release)
        assert wait_for(lambda: len(loop.status.saved) == 1)  # now the normal idle time applies
    assert len(mido.MidiFile(loop.mids()[0]).tracks[1]) == 4  # name + 2 events + end_of_track


def test_max_hold_ends_a_take_with_a_stuck_note(tmp_path: Path) -> None:
    with running(tmp_path, idle=0.05, max_hold=0.4) as loop:
        started = time.perf_counter()
        loop.put(note("note_on", 60))
        assert wait_for(lambda: len(loop.status.saved) == 1, timeout=5.0)
        assert time.perf_counter() - started >= 0.35


def test_a_new_event_after_a_take_starts_the_next_take(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Clock(datetime):  # every take must start in a different second, or the file names would clash
        counter = 0

        @classmethod
        def now(cls, tz: tzinfo | None = None) -> "Clock":
            cls.counter += 1
            return cls(2026, 1, 1, 0, 0, cls.counter)

    monkeypatch.setattr(m, "datetime", Clock)
    with running(tmp_path, idle=0.05, max_hold=60.0) as loop:
        loop.put(note("note_on", 60), age=1000.0)  # already older than the idle time: a one-event take
        assert wait_for(lambda: len(loop.status.saved) == 1)
        loop.put(note("note_on", 62), age=1000.0)
        assert wait_for(lambda: len(loop.status.saved) == 2)

    assert [p.name for p in loop.mids()] == ["2026-01-01_00-00-01.mid", "2026-01-01_00-00-02.mid"]
    assert [_notes(p) for p in loop.mids()] == [[60], [62]]


def test_events_from_two_ports_become_two_tracks(tmp_path: Path) -> None:
    with running(tmp_path, idle=0.2, max_hold=60.0) as loop:
        loop.put(note("note_on", 60), port="Piano")
        loop.put(note("note_on", 40), port="Bass")
        loop.put(note("note_off", 60), port="Piano")
        loop.put(note("note_off", 40), port="Bass")
        assert wait_for(lambda: len(loop.status.saved) == 1)
    tracks = mido.MidiFile(loop.mids()[0]).tracks
    assert [track_names(t) for t in tracks[1:]] == [["Piano"], ["Bass"]]


def test_stop_saves_the_take_in_progress_including_queued_events(tmp_path: Path) -> None:
    with running(tmp_path, idle=60.0, max_hold=120.0) as loop:
        loop.put(note("note_on", 60))
        assert wait_for(lambda: loop.status.state == "recording")
        for number in (62, 64, 65):  # queued before the stop flag is raised: none of them may be lost
            loop.put(note("note_on", number))
        loop.stop.set()
    assert len(loop.mids()) == 1
    assert _notes(loop.mids()[0]) == [60, 62, 64, 65]


def test_stop_while_idle_writes_nothing(tmp_path: Path) -> None:
    with running(tmp_path, idle=0.1, max_hold=1.0):
        time.sleep(0.3)
    assert list(tmp_path.iterdir()) == []


def test_the_journal_mirrors_the_take_while_it_is_recorded(tmp_path: Path) -> None:
    with running(tmp_path, idle=60.0, max_hold=120.0) as loop:
        loop.put(note("note_on", 60), port="Piano")
        loop.put(pedal(127), port="Piano")
        assert wait_for(lambda: loop.status.events == 2)

        (journal,) = tmp_path.glob("*.journal")
        assert journal.stem == m.file_stem(loop.status.started or datetime.min)
        header, first, second = journal.read_text(encoding="utf-8").splitlines()
        assert set(json.loads(header)) == {"version", "started"}
        assert json.loads(first)[1:] == ["Piano", "90 3C 5A"]
        assert json.loads(second)[1:] == ["Piano", "B0 40 7F"]
    assert list(tmp_path.glob("*.journal")) == []  # removed after the clean save


def test_a_failed_save_leaves_the_journal_and_the_error_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, log_records: list[logging.LogRecord]
) -> None:
    """FROZEN CURRENT BEHAVIOUR, see SPECIFICATIONS.md KL-1: a save error ends the session thread (the process keeps running but stops recording).

    The journal is what makes this survivable. If the thread is changed to log the error and carry on, update this test.
    """

    def broken(*args: object, **kwargs: object) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(m, "save", broken)
    q: queue.Queue[m.Event] = queue.Queue()
    q.put((time.perf_counter() - 1000, "A", note("note_on")))

    with pytest.raises(OSError, match="disk full"):
        m.session_loop(q, tmp_path, 0.1, 1.0)  # runs in this thread: the old event ends the take at once

    assert len(list(tmp_path.glob("*.journal"))) == 1  # the raw events are still there for the next start


def test_the_output_folder_is_created(tmp_path: Path) -> None:
    target = tmp_path / "a" / "b"
    with running(target, idle=0.1, max_hold=1.0):
        assert wait_for(target.is_dir)
