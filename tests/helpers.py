"""Shared test helpers: a fake MIDI backend (no hardware needed), message builders and a polling wait."""

import time
from collections.abc import Callable

import mido


def note(kind: str, number: int = 60, channel: int = 0, velocity: int = 90) -> mido.Message:
    """note_on (velocity > 0) or note_off."""
    if kind == "note_on":
        return mido.Message("note_on", note=number, channel=channel, velocity=velocity)
    return mido.Message("note_off", note=number, channel=channel, velocity=0)


def pedal(value: int, control: int = 64, channel: int = 0) -> mido.Message:
    return mido.Message("control_change", control=control, value=value, channel=channel)


def track_names(track: mido.MidiTrack) -> list[str]:
    """The track_name meta messages of a track (normally exactly one)."""
    return [x.name for x in track if isinstance(x, mido.MetaMessage) and x.type == "track_name"]


def wait_for(predicate: Callable[[], bool], timeout: float = 5.0) -> bool:
    """Poll `predicate` until it is true or `timeout` seconds have passed."""
    deadline = time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakePort:
    """Stands in for a mido input port: remembers its callback so tests can 'play' messages."""

    def __init__(self, name: str, callback: Callable[[mido.Message], None] | None) -> None:
        self.name = name
        self.callback = callback
        self.closed = False

    def send(self, msg: mido.Message) -> None:
        assert self.callback is not None
        self.callback(msg)

    def close(self) -> None:
        self.closed = True


class FakeBackend:
    """Replaces mido.get_input_names / mido.open_input. Tests change `names` to plug and unplug ports."""

    def __init__(self) -> None:
        self.names: list[str] = []
        self.scan_error: Exception | None = None
        self.open_errors: dict[str, Exception] = {}
        self.open_calls: list[str] = []
        self.ports: dict[str, FakePort] = {}  # the most recently opened port per name

    def get_input_names(self, **kwargs: object) -> list[str]:
        if self.scan_error is not None:
            raise self.scan_error
        return list(self.names)

    def open_input(self, name: str | None = None, callback: Callable[[mido.Message], None] | None = None, **kwargs: object) -> FakePort:
        assert name is not None
        self.open_calls.append(name)
        if name in self.open_errors:
            raise self.open_errors[name]
        port = FakePort(name, callback)
        self.ports[name] = port
        return port
