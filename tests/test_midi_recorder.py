"""Tests for the pure parts of midi_recorder (no MIDI hardware needed)."""

import logging
from collections.abc import Iterator
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
