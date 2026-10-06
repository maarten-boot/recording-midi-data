"""Frozen behaviour of the pure parts: held-note tracking, the status object and the .mid file layout."""

from datetime import datetime, timedelta
from pathlib import Path

import mido
import pytest
from helpers import note, pedal, track_names

import midi_recorder as m


def _active(s: m.HoldState) -> bool:
    # a call, so mypy does not narrow `s.active` between the updates a test makes
    return s.active


class TestHoldState:
    def test_note_on_and_off(self) -> None:
        s = m.HoldState()
        assert not _active(s)
        s.update("A", note("note_on"))
        assert _active(s)
        s.update("A", note("note_off"))
        assert not _active(s)

    def test_note_on_with_velocity_zero_is_a_release(self) -> None:
        s = m.HoldState()
        s.update("A", note("note_on"))
        s.update("A", mido.Message("note_on", note=60, velocity=0))
        assert not _active(s)

    def test_notes_are_tracked_per_port_channel_and_pitch(self) -> None:
        s = m.HoldState()
        s.update("A", note("note_on", 60, channel=0))
        s.update("B", note("note_on", 60, channel=0))
        s.update("A", note("note_on", 60, channel=1))
        s.update("A", note("note_off", 60, channel=0))
        assert _active(s)  # B/0 and A/1 are still held
        s.update("B", note("note_off", 60, channel=0))
        s.update("A", note("note_off", 60, channel=1))
        assert not _active(s)

    def test_release_of_an_unknown_note_is_harmless(self) -> None:
        s = m.HoldState()
        s.update("A", note("note_off", 99))
        assert not _active(s)

    @pytest.mark.parametrize(("value", "down"), [(0, False), (63, False), (64, True), (127, True)])
    def test_sustain_pedal_threshold_is_64(self, value: int, down: bool) -> None:
        s = m.HoldState()
        s.update("A", pedal(value))
        assert _active(s) is down

    def test_sustain_pedal_is_tracked_per_port_and_channel(self) -> None:
        s = m.HoldState()
        s.update("A", pedal(127, channel=0))
        s.update("A", pedal(0, channel=1))  # another channel's pedal up does not release channel 0
        assert _active(s)
        s.update("A", pedal(0, channel=0))
        assert not _active(s)

    def test_other_controllers_and_messages_do_not_hold(self) -> None:
        s = m.HoldState()
        for msg in (pedal(127, control=67), pedal(127, control=66), mido.Message("aftertouch", value=50), mido.Message("pitchwheel", pitch=100)):
            s.update("A", msg)
        assert not _active(s)


class TestStatus:
    def test_defaults(self) -> None:
        s = m.Status()
        assert (s.state, s.events, s.started, s.ports, s.saved) == ("idle", 0, None, (), ())

    def test_elapsed_text_formats_and_clamps(self) -> None:
        now = datetime(2026, 10, 5, 12, 0, 0)
        s = m.Status()
        s.started = now - timedelta(hours=100, minutes=2, seconds=3)
        assert s.elapsed_text(now) == "100:02:03"
        s.started = now + timedelta(seconds=30)  # a clock that stepped back never shows a negative time
        assert s.elapsed_text(now) == "00:00:00"


def test_file_stem_and_constants() -> None:
    assert m.file_stem(datetime(2026, 1, 2, 3, 4, 5)) == "2026-01-02_03-04-05"
    assert (m.TICKS_PER_BEAT, m.DEFAULT_TEMPO, m.TICKS_PER_SECOND) == (1000, 1_000_000, 1000.0)
    assert (m.IDLE_TIME, m.MAX_HOLD, m.RESCAN_SECONDS) == (30.0, 120.0, 2.0)
    assert {"clock", "active_sensing"} == m.SKIP_TYPES


class TestSave:
    def test_one_track_per_port_in_order_of_first_event(self, tmp_path: Path) -> None:
        events: list[m.Event] = [
            (1.0, "Second", note("note_on", 60)),
            (1.1, "First", note("note_on", 62)),
            (1.2, "Second", note("note_off", 60)),
            (1.3, "First", note("note_off", 62)),
        ]
        mid = mido.MidiFile(m.save(events, datetime(2026, 1, 1), tmp_path))

        assert mid.type == 1
        assert mid.ticks_per_beat == 1000
        assert len(mid.tracks) == 3
        assert [track_names(t) for t in mid.tracks[1:]] == [["Second"], ["First"]]  # ordered by first appearance, not alphabetically

    def test_tempo_track_and_end_of_track_markers(self, tmp_path: Path) -> None:
        mid = mido.MidiFile(m.save([(0.0, "A", note("note_on"))], datetime(2026, 1, 1), tmp_path))
        tempo, track = mid.tracks
        assert [x.type for x in tempo] == ["set_tempo", "end_of_track"]
        assert tempo[0].tempo == 1_000_000  # type: ignore[union-attr]
        assert track[-1].type == "end_of_track"
        assert track[0].type == "track_name"

    def test_ticks_are_milliseconds_relative_to_the_first_event(self, tmp_path: Path) -> None:
        events: list[m.Event] = [(500.0, "A", note("note_on")), (500.25, "A", note("note_off"))]
        track = mido.MidiFile(m.save(events, datetime(2026, 1, 1), tmp_path)).tracks[1]
        assert [(x.type, x.time) for x in track if x.type.startswith("note")] == [("note_on", 0), ("note_off", 250)]

    def test_rounding_never_accumulates(self, tmp_path: Path) -> None:
        """1000 events 0.7 ms apart: the deltas must add up to the rounded absolute end time."""
        events: list[m.Event] = [(i * 0.0007, "A", mido.Message("aftertouch", value=i % 128)) for i in range(1000)]
        track = mido.MidiFile(m.save(events, datetime(2026, 1, 1), tmp_path)).tracks[1]
        assert sum(x.time for x in track) == round(999 * 0.0007 * 1000)

    def test_messages_survive_unchanged_and_the_input_is_not_modified(self, tmp_path: Path) -> None:
        original = [
            note("note_on", 61, channel=3, velocity=77),
            pedal(127),
            mido.Message("aftertouch", value=33, channel=2),
            mido.Message("polytouch", note=60, value=40),
            mido.Message("pitchwheel", pitch=-1234),
            mido.Message("sysex", data=[1, 2, 3]),
        ]
        events: list[m.Event] = [(10.0 + i, "A", msg) for i, msg in enumerate(original)]
        track = mido.MidiFile(m.save(events, datetime(2026, 1, 1), tmp_path)).tracks[1]

        saved = [x for x in track if isinstance(x, mido.Message)]
        assert [x.copy(time=0) for x in saved] == original
        assert all(msg.time == 0 for msg in original)  # save() copies, it does not edit the caller's messages

    def test_filename_comes_from_the_start_time_and_suffix(self, tmp_path: Path) -> None:
        started = datetime(2026, 10, 5, 21, 35, 12)
        ev: list[m.Event] = [(0.0, "A", note("note_on"))]
        assert m.save(ev, started, tmp_path).name == "2026-10-05_21-35-12.mid"
        assert m.save(ev, started, tmp_path, suffix="_recovered").name == "2026-10-05_21-35-12_recovered.mid"

    def test_playing_length_equals_recorded_time(self, tmp_path: Path) -> None:
        events: list[m.Event] = [(0.0, "A", note("note_on")), (6.15, "A", note("note_off"))]
        assert round(mido.MidiFile(m.save(events, datetime(2026, 1, 1), tmp_path)).length, 3) == 6.15
