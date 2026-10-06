"""Minimal typing stubs for the parts of mido used by midi_recorder (mido ships no py.typed)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import ports as ports

class Message:
    type: str
    channel: int
    note: int
    velocity: int
    control: int
    value: int
    time: int
    def __init__(self, type: str, **args: Any) -> None: ...
    def copy(self, **overrides: Any) -> Message: ...
    def hex(self, sep: str = " ") -> str: ...
    @classmethod
    def from_hex(cls, text: str, sep: str = " ", **kwargs: Any) -> Message: ...

class MetaMessage:
    type: str
    time: int
    name: str  # track_name
    tempo: int  # set_tempo
    def __init__(self, type: str, **kwargs: Any) -> None: ...
    def copy(self, **overrides: Any) -> MetaMessage: ...

class MidiTrack(list[Message | MetaMessage]):
    name: str

class MidiFile:
    type: int
    ticks_per_beat: int
    tracks: list[MidiTrack]
    length: float
    def __init__(self, filename: str | Path | None = None, type: int = 1, ticks_per_beat: int = 480, **kwargs: Any) -> None: ...
    def save(self, filename: str | Path | None = None) -> None: ...

def get_input_names(**kwargs: Any) -> list[str]: ...
def open_input(name: str | None = None, virtual: bool = False, callback: Callable[[Message], None] | None = None, **kwargs: Any) -> ports.BaseInput: ...
