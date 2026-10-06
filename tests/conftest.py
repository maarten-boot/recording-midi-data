"""Fixtures shared by all tests."""

import logging
from collections.abc import Iterator

import mido
import pytest
from helpers import FakeBackend

import midi_recorder as m


@pytest.fixture
def log_records() -> Iterator[list[logging.LogRecord]]:
    """Collect everything the midi_recorder logger emits (at any level) in a list."""
    records: list[logging.LogRecord] = []

    class Collect(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = Collect()
    old_level = m.logger.level
    m.logger.addHandler(handler)
    m.logger.setLevel(logging.DEBUG)
    yield records
    m.logger.removeHandler(handler)
    m.logger.setLevel(old_level)


@pytest.fixture
def fake_backend(monkeypatch: pytest.MonkeyPatch) -> FakeBackend:
    """A fake MIDI backend, with fast port rescans."""
    backend = FakeBackend()
    monkeypatch.setattr(mido, "get_input_names", backend.get_input_names)
    monkeypatch.setattr(mido, "open_input", backend.open_input)
    monkeypatch.setattr(m, "RESCAN_SECONDS", 0.02)
    return backend
