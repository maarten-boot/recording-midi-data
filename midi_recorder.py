#!/usr/bin/env python3
"""midi_recorder: record every incoming MIDI event from all input ports.

* A session starts at the first event and ends when playing has stopped
  (no events for --idle seconds, and no notes held / sustain pedal up).
* Files are named after the start of the session: YYYY-MM-DD_HH-MM-SS.mid
* Every event is kept (notes, pedals, aftertouch, pitch bend, ...), except
  realtime noise (clock, active_sensing).
* One track per input port. 1 tick = 1 ms, so files play back in real time.
* Ports that are plugged in later are picked up automatically.

Logging: INFO to stderr and to <outdir>/midi_recorder.log (rotated); see --log-level, --log-file, --no-log-file.

Requires: pip install mido python-rtmidi
"""

import argparse
import contextlib
import logging
import logging.handlers
import queue
import sys
import threading
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import NoReturn

import mido

SKIP_TYPES = {"clock", "active_sensing"}
SKIP_PORT_PARTS = ("midi through", "rtmidi")  # loopback / our own ports
RESCAN_SECONDS = 2.0
TICKS_PER_BEAT = 1000  # with tempo 1_000_000 us/beat -> 1 tick = 1 ms
IDLE_TIME = 30.0
MAX_HOLD = 120.0
DEFAULT_TEMPO = 1_000_000
TICKS_PER_SECOND = TICKS_PER_BEAT * 1_000_000 / DEFAULT_TEMPO  # 1000.0 with the values above

LOGGER_NAME = "midi_recorder"
LOG_FILE_NAME = "midi_recorder.log"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3

logger = logging.getLogger(LOGGER_NAME)

# (time.monotonic() at arrival, input port name, message)
Event = tuple[float, str, mido.Message]


def setup_logging(level: str = "INFO", log_file: Path | None = None) -> None:
    """Log to stderr and (when log_file is given) to a size-rotated file."""
    for handler in list(logger.handlers):  # idempotent: drop handlers from an earlier call
        logger.removeHandler(handler)
        handler.close()

    logger.setLevel(level)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", datefmt="%Y-%m-%d %H:%M:%S")

    handlers: list[logging.Handler] = [logging.StreamHandler()]  # stderr
    if log_file is not None:
        handlers.append(logging.handlers.RotatingFileHandler(log_file, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUPS, encoding="utf-8"))

    for handler in handlers:
        handler.setFormatter(formatter)
        logger.addHandler(handler)


class HoldState:
    """Tracks held notes and sustain pedal per (port, channel)."""

    def __init__(self) -> None:
        self.notes: set[tuple[str, int, int]] = set()
        self.sustain: set[tuple[str, int]] = set()

    def update(self, port: str, msg: mido.Message) -> None:
        t = msg.type
        if t == "note_on" and msg.velocity > 0:
            self.notes.add((port, msg.channel, msg.note))
        elif t in ("note_off", "note_on"):
            self.notes.discard((port, msg.channel, msg.note))
        elif t == "control_change" and msg.control == 64:
            key = (port, msg.channel)
            if msg.value >= 64:
                self.sustain.add(key)
            else:
                self.sustain.discard(key)

    @property
    def active(self) -> bool:
        return bool(self.notes or self.sustain)


def save(events: list[Event], started: datetime, outdir: Path | str) -> Path:
    """events: list of (monotonic_time, port_name, msg), in arrival order."""
    t0 = events[0][0]
    mid = mido.MidiFile(type=1, ticks_per_beat=TICKS_PER_BEAT)

    tempo_track = mido.MidiTrack()
    tempo_track.append(mido.MetaMessage("set_tempo", tempo=DEFAULT_TEMPO))
    tempo_track.append(mido.MetaMessage("end_of_track", time=0))

    mid.tracks.append(tempo_track)
    ports = list(dict.fromkeys(port for _, port, _ in events))

    for port in ports:
        track = mido.MidiTrack()
        track.append(mido.MetaMessage("track_name", name=port, time=0))
        last_tick = 0

        for t, p, msg in events:
            if p != port:
                continue

            tick = round((t - t0) * TICKS_PER_SECOND)
            track.append(msg.copy(time=tick - last_tick))  # delta from absolute
            last_tick = tick

        track.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(track)

    path = Path(outdir) / f"{started:%Y-%m-%d_%H-%M-%S}.mid"
    mid.save(path)
    return path


def session_loop(q: queue.Queue[Event], outdir: Path | str, idle: float, max_hold: float) -> NoReturn:
    """Block for the first event, collect until playing stops, save, repeat."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    while True:
        first = q.get()
        started = datetime.now()
        events = [first]
        state = HoldState()
        state.update(first[1], first[2])
        logger.info("recording started")

        try:
            while True:
                limit = max_hold if state.active else idle
                remaining = limit - (time.monotonic() - events[-1][0])

                if remaining <= 0:
                    break
                try:
                    item = q.get(timeout=remaining)
                except queue.Empty:
                    break

                events.append(item)
                state.update(item[1], item[2])

        finally:  # also runs on Ctrl-C so the current take is not lost
            path = save(events, started, outdir)
            logger.info("saved %s (%d events)", path, len(events))


def port_manager(q: queue.Queue[Event], stop: threading.Event) -> None:
    """Open every input port, and keep rescanning for plugged/unplugged ones."""
    open_ports: dict[str, mido.ports.BaseInput] = {}

    def make_callback(name: str) -> Callable[[mido.Message], None]:
        def on_msg(msg: mido.Message) -> None:  # keep tiny: runs in the MIDI driver thread
            if msg.type not in SKIP_TYPES:
                q.put((time.monotonic(), name, msg))

        return on_msg

    scan_ok = True  # log scan failures only on the transition, not every RESCAN_SECONDS
    failed: set[str] = set()  # ports that could not be opened (retried, but logged once)

    while not stop.is_set():
        try:
            names = {n for n in mido.get_input_names() if not any(part in n.lower() for part in SKIP_PORT_PARTS)}
            scan_ok = True

        except Exception as exc:  # backend hiccup, try again next round
            if scan_ok:
                logger.warning("port scan failed: %s", exc)
            scan_ok = False
            names = set(open_ports)

        for name in names - set(open_ports):
            try:
                open_ports[name] = mido.open_input(name, callback=make_callback(name))
                failed.discard(name)
                logger.info("listening on: %s", name)

            except Exception as exc:
                if name not in failed:
                    failed.add(name)
                    logger.warning("cannot open %s: %s", name, exc)

        failed &= names  # forget ports that vanished

        for name in set(open_ports) - names:
            with contextlib.suppress(Exception):
                open_ports.pop(name).close()

            logger.info("input gone: %s", name)

        stop.wait(RESCAN_SECONDS)

    for port in open_ports.values():
        port.close()


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("-o", "--outdir", default=".", help="output directory")
    ap.add_argument(
        "--idle",
        type=float,
        default=IDLE_TIME,
        help="seconds of silence that end a recording (default %(default)s)",
    )
    ap.add_argument(
        "--max-hold",
        type=float,
        default=MAX_HOLD,
        help="max silence while notes/pedal are held (default %(default)s)",
    )
    ap.add_argument("--list", action="store_true", help="list input ports and exit")
    ap.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS, default="INFO", help="log level (default %(default)s)")
    ap.add_argument("--log-file", type=Path, default=None, help=f"log file (default: <outdir>/{LOG_FILE_NAME})")
    ap.add_argument("--no-log-file", action="store_true", help="log to stderr only")
    args = ap.parse_args()

    if args.list:
        for n in mido.get_input_names():
            print(n)
        return 0

    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    log_file = None if args.no_log_file else (args.log_file or outdir / LOG_FILE_NAME)
    setup_logging(args.log_level, log_file)

    q: queue.Queue[Event] = queue.Queue()
    stop = threading.Event()
    threading.Thread(
        target=port_manager,
        args=(q, stop),
        daemon=True,
    ).start()
    logger.info("midi recorder active, writing to %s (Ctrl-C to quit)", outdir.resolve())

    try:
        session_loop(q, outdir, args.idle, args.max_hold)

    except KeyboardInterrupt:
        pass

    finally:
        stop.set()

    return 0


if __name__ == "__main__":
    sys.exit(main())
