"""Frozen behaviour of port_manager (hot-plug, failures) and of Recorder, both against a fake MIDI backend."""

import logging
import queue
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import cast

import mido
import pytest
from helpers import FakeBackend, note, pedal, track_names, wait_for

import midi_recorder as m


class Ports:
    """port_manager running in a thread."""

    def __init__(self) -> None:
        self.q: queue.Queue[m.Event] = queue.Queue()
        self.status = m.Status()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=m.port_manager, args=(self.q, self.stop, self.status))


@contextmanager
def ports_running() -> Iterator[Ports]:
    ports = Ports()
    ports.thread.start()
    try:
        yield ports
    finally:
        ports.stop.set()
        ports.thread.join(5.0)
        assert not ports.thread.is_alive()


def _messages(log_records: list[logging.LogRecord], level: int = logging.WARNING) -> list[str]:
    return [r.getMessage() for r in log_records if r.levelno == level]


class TestPortManager:
    def test_opens_real_inputs_and_skips_loopbacks(self, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["Piano 20:0", "Midi Through:Midi Through Port-0 14:0", "RtMidi Output 130:0", "MIDI THROUGH 2"]
        with ports_running() as p:
            assert wait_for(lambda: p.status.ports == ("Piano 20:0",))
        assert fake_backend.open_calls == ["Piano 20:0"]

    def test_status_ports_are_sorted(self, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["Zeta", "Alpha", "Mid"]
        with ports_running() as p:
            assert wait_for(lambda: len(p.status.ports) == 3)
            assert p.status.ports == ("Alpha", "Mid", "Zeta")

    def test_callback_queues_events_with_port_name_and_drops_realtime_noise(self, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["Piano"]
        with ports_running() as p:
            assert wait_for(lambda: "Piano" in fake_backend.ports)
            port = fake_backend.ports["Piano"]
            before = time.perf_counter()
            for msg in (note("note_on"), mido.Message("clock"), mido.Message("active_sensing"), pedal(127), mido.Message("songpos", pos=3)):
                port.send(msg)
            after = time.perf_counter()

            got = [p.q.get(timeout=1.0) for _ in range(3)]
            assert p.q.empty()  # clock and active_sensing were not queued

        assert [(name, msg.type) for _, name, msg in got] == [("Piano", "note_on"), ("Piano", "control_change"), ("Piano", "songpos")]
        stamps = [t for t, _, _ in got]
        assert stamps == sorted(stamps)
        assert before <= stamps[0] <= stamps[-1] <= after  # time.perf_counter() at arrival

    def test_hot_plug_opens_new_and_closes_vanished_inputs(self, fake_backend: FakeBackend, log_records: list[logging.LogRecord]) -> None:
        fake_backend.names = ["A"]
        with ports_running() as p:
            assert wait_for(lambda: p.status.ports == ("A",))

            fake_backend.names = ["A", "B"]
            assert wait_for(lambda: p.status.ports == ("A", "B"))

            fake_backend.names = ["B"]
            assert wait_for(lambda: p.status.ports == ("B",))
            assert fake_backend.ports["A"].closed
            assert not fake_backend.ports["B"].closed

        assert "listening on: A" in _messages(log_records, logging.INFO)
        assert "input gone: A" in _messages(log_records, logging.INFO)

    def test_a_replugged_input_is_opened_again(self, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["A"]
        with ports_running() as p:
            assert wait_for(lambda: p.status.ports == ("A",))
            first = fake_backend.ports["A"]
            fake_backend.names = []
            assert wait_for(lambda: p.status.ports == ())
            fake_backend.names = ["A"]
            assert wait_for(lambda: p.status.ports == ("A",))
            assert fake_backend.ports["A"] is not first
            assert first.closed

    def test_scan_failure_is_logged_once_per_outage_and_open_inputs_are_kept(self, fake_backend: FakeBackend, log_records: list[logging.LogRecord]) -> None:
        fake_backend.names = ["A"]
        with ports_running() as p:
            assert wait_for(lambda: p.status.ports == ("A",))

            fake_backend.scan_error = OSError("backend gone")
            time.sleep(0.3)  # many rescans
            assert p.status.ports == ("A",)
            assert not fake_backend.ports["A"].closed
            assert [x for x in _messages(log_records) if "port scan failed" in x] == ["port scan failed: backend gone"]

            fake_backend.scan_error = None
            time.sleep(0.1)
            fake_backend.scan_error = OSError("again")
            time.sleep(0.2)
            assert sum("port scan failed" in x for x in _messages(log_records)) == 2  # a second outage is logged again

    def test_open_failure_is_logged_once_per_port_but_retried(self, fake_backend: FakeBackend, log_records: list[logging.LogRecord]) -> None:
        fake_backend.names = ["A"]
        fake_backend.open_errors["A"] = OSError("busy")
        with ports_running() as p:
            assert wait_for(lambda: fake_backend.open_calls.count("A") >= 4)
            assert p.status.ports == ()
            assert _messages(log_records) == ["cannot open A: busy"]

            del fake_backend.open_errors["A"]
            assert wait_for(lambda: p.status.ports == ("A",))
        assert "listening on: A" in _messages(log_records, logging.INFO)

    def test_a_failing_port_that_disappears_is_forgotten(self, fake_backend: FakeBackend, log_records: list[logging.LogRecord]) -> None:
        fake_backend.names = ["A"]
        fake_backend.open_errors["A"] = OSError("busy")
        with ports_running():
            assert wait_for(lambda: len(_messages(log_records)) == 1)
            fake_backend.names = []
            time.sleep(0.15)
            fake_backend.names = ["A"]
            assert wait_for(lambda: len(_messages(log_records)) == 2)  # unplugged and back: reported again

    def test_stop_closes_every_input(self, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["A", "B"]
        with ports_running() as p:
            assert wait_for(lambda: p.status.ports == ("A", "B"))
        assert all(port.closed for port in fake_backend.ports.values())
        assert p.status.ports == ()


class TestRecorder:
    def test_end_to_end_with_a_fake_piano(self, tmp_path: Path, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["Piano 20:0"]
        recorder = m.Recorder(tmp_path / "out", idle=0.3, max_hold=5.0)
        recorder.start()
        try:
            assert wait_for(lambda: recorder.status.ports == ("Piano 20:0",))
            port = fake_backend.ports["Piano 20:0"]
            for msg in (
                pedal(127),
                note("note_on", 60),
                mido.Message("aftertouch", value=33),
                mido.Message("polytouch", note=60, value=40),
                note("note_off", 60),
                pedal(0),
            ):
                port.send(msg)
                time.sleep(0.02)
            assert wait_for(lambda: len(recorder.status.saved) == 1)
        finally:
            recorder.stop()

        (saved,) = recorder.status.saved
        assert saved.parent == tmp_path / "out"
        tracks = mido.MidiFile(saved).tracks
        assert track_names(tracks[1]) == ["Piano 20:0"]
        assert [x.type for x in tracks[1] if x.type not in ("track_name", "end_of_track")] == [
            "control_change",
            "note_on",
            "aftertouch",
            "polytouch",
            "note_off",
            "control_change",
        ]
        assert not list((tmp_path / "out").glob("*.journal"))

    def test_stop_saves_the_take_in_progress(self, tmp_path: Path, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["Piano"]
        recorder = m.Recorder(tmp_path, idle=60.0, max_hold=120.0)
        recorder.start()
        assert wait_for(lambda: "Piano" in fake_backend.ports)
        for number in (60, 62, 64):
            fake_backend.ports["Piano"].send(note("note_on", number))
        assert wait_for(lambda: recorder.status.events == 3)

        recorder.stop()

        assert len(recorder.status.saved) == 1
        assert recorder.status.state == "idle"
        assert recorder.status.ports == ()
        assert fake_backend.ports["Piano"].closed
        assert not [t for t in recorder._threads if t.is_alive()]

    def test_start_recovers_journals_left_by_a_crash(self, tmp_path: Path, fake_backend: FakeBackend, log_records: list[logging.LogRecord]) -> None:
        journal = m.Journal(tmp_path / "2026-10-05_09-00-00.journal", datetime(2026, 10, 5, 9, 0, 0), t0=0.0)
        journal.append((0.0, "Piano", note("note_on")))
        journal.close()

        recorder = m.Recorder(tmp_path)
        recorder.start()
        try:
            assert [p.name for p in recorder.status.saved] == ["2026-10-05_09-00-00.mid"]
        finally:
            recorder.stop()
        assert any("recovered" in x for x in _messages(log_records))

    def test_two_inputs_two_tracks_one_file(self, tmp_path: Path, fake_backend: FakeBackend) -> None:
        fake_backend.names = ["Piano", "Synth"]
        recorder = m.Recorder(tmp_path, idle=0.3, max_hold=5.0)
        recorder.start()
        try:
            assert wait_for(lambda: recorder.status.ports == ("Piano", "Synth"))
            fake_backend.ports["Synth"].send(note("note_on", 40))
            fake_backend.ports["Piano"].send(note("note_on", 60))
            assert wait_for(lambda: len(recorder.status.saved) == 1)
        finally:
            recorder.stop()
        tracks = mido.MidiFile(recorder.status.saved[0]).tracks
        assert [track_names(t) for t in tracks[1:]] == [["Synth"], ["Piano"]]

    def test_the_list_of_saved_files_is_capped(self, tmp_path: Path, fake_backend: FakeBackend, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(m, "RECENT_FILES", 2)
        for hour in (9, 10, 11):
            journal = m.Journal(tmp_path / f"2026-10-05_{hour:02d}-00-00.journal", datetime(2026, 10, 5, hour), t0=0.0)
            journal.append((0.0, "A", note("note_on")))
            journal.close()

        recorder = m.Recorder(tmp_path)
        recorder.start()
        recorder.stop()

        assert [p.name for p in recorder.status.saved] == ["2026-10-05_10-00-00.mid", "2026-10-05_11-00-00.mid"]  # the newest two

    def test_constructor_defaults(self, tmp_path: Path) -> None:
        recorder = m.Recorder(tmp_path)
        assert (recorder.idle, recorder.max_hold, recorder.outdir) == (m.IDLE_TIME, m.MAX_HOLD, tmp_path)
        assert recorder.status == m.Status()


def test_stop_warns_about_a_thread_that_does_not_stop(tmp_path: Path, log_records: list[logging.LogRecord]) -> None:
    class Stuck:
        name = "stuck"

        def join(self, timeout: float | None = None) -> None:
            pass  # returns at once, like a join() that ran into its timeout

        def is_alive(self) -> bool:
            return True

    recorder = m.Recorder(tmp_path)
    recorder._threads = [cast(threading.Thread, Stuck())]
    recorder.stop()
    assert _messages(log_records) == ["thread stuck did not stop in time"]
