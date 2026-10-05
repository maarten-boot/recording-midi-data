#!/usr/bin/env python3
"""midi_recorder: record every incoming MIDI event from all input ports.

* A session starts at the first event and ends when playing has stopped
  (no events for --idle seconds, and no notes held / sustain pedal up).
* Files are named after the start of the session: YYYY-MM-DD_HH-MM-SS.mid
* Every event is kept (notes, pedals, aftertouch, pitch bend, ...), except
  realtime noise (clock, active_sensing).
* One track per input port. 1 tick = 1 ms, so files play back in real time.
* Ports that are plugged in later are picked up automatically.
* Crash safety: while recording, every event is also appended to
  <outdir>/YYYY-MM-DD_HH-MM-SS.journal. On a clean save the journal is deleted;
  after a crash the next start converts leftover journals into .mid files.
  (Run only one recorder per output directory.)

Logging: INFO to stderr and to <outdir>/midi_recorder.log (rotated); see --log-level, --log-file, --no-log-file.
A small status window is available as midi_recorder_gui.py.

Requires: pip install mido python-rtmidi
"""

import argparse
import contextlib
import json
import logging
import logging.handlers
import os
import queue
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import TextIO

import mido

__version__ = "0.1.0"

SKIP_TYPES = {"clock", "active_sensing"}
SKIP_PORT_PARTS = ("midi through", "rtmidi")  # loopback / our own ports
RESCAN_SECONDS = 2.0
POLL_SECONDS = 0.2  # how often the worker threads look at their stop flag
TICKS_PER_BEAT = 1000  # with tempo 1_000_000 us/beat -> 1 tick = 1 ms
IDLE_TIME = 30.0
MAX_HOLD = 120.0
DEFAULT_TEMPO = 1_000_000
TICKS_PER_SECOND = TICKS_PER_BEAT * 1_000_000 / DEFAULT_TEMPO  # 1000.0 with the values above

JOURNAL_SUFFIX = ".journal"
JOURNAL_SYNC_SECONDS = 1.0  # fsync the journal at most this often
JOURNAL_VERSION = 1
RECENT_FILES = 50  # how many saved files the status keeps

LOGGER_NAME = "midi_recorder"
LOG_FILE_NAME = "midi_recorder.log"
LOG_LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")
LOG_MAX_BYTES = 1_000_000
LOG_BACKUPS = 3

logger = logging.getLogger(LOGGER_NAME)

# (time.perf_counter() at arrival, input port name, message).
# perf_counter, not monotonic: on Windows time.monotonic() ticks at ~16 ms before Python 3.13.
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


@dataclass
class Status:
    """Live state for a user interface. Every field has exactly one writer thread, so no lock is needed."""

    state: str = "idle"  # "idle" or "recording"
    events: int = 0  # events in the take being recorded
    started: datetime | None = None  # wall-clock start of the take being recorded
    ports: tuple[str, ...] = ()  # input ports currently open
    saved: tuple[Path, ...] = ()  # recently saved files, oldest first

    def elapsed_text(self, now: datetime) -> str:
        """HH:MM:SS since the current take started, or '' when not recording."""
        if self.started is None:
            return ""
        seconds = max(0, int((now - self.started).total_seconds()))
        hours, rest = divmod(seconds, 3600)
        minutes, seconds = divmod(rest, 60)
        return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


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


def file_stem(started: datetime) -> str:
    return f"{started:%Y-%m-%d_%H-%M-%S}"


def save(events: list[Event], started: datetime, outdir: Path | str, suffix: str = "") -> Path:
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

    path = Path(outdir) / f"{file_stem(started)}{suffix}.mid"
    mid.save(path)
    return path


class Journal:
    """Append-only crash-safety log of one take: a JSON header line, then one line per event, flushed as it arrives.

    Journal problems (disk full, unwritable folder) are logged once and never stop the recording itself.
    """

    def __init__(self, path: Path, started: datetime, t0: float) -> None:
        self.path = path
        self._t0 = t0
        self._file: TextIO | None = None
        self._last_sync = time.perf_counter()
        try:
            self._file = path.open("w", encoding="utf-8")
        except OSError as exc:
            logger.warning("cannot write journal %s: %s (crash protection is off for this take)", path, exc)
            return
        self._write({"version": JOURNAL_VERSION, "started": started.isoformat()})

    def _write(self, record: object) -> None:
        if self._file is None:
            return
        try:
            self._file.write(json.dumps(record) + "\n")
            self._file.flush()
            now = time.perf_counter()
            if now - self._last_sync >= JOURNAL_SYNC_SECONDS:
                os.fsync(self._file.fileno())
                self._last_sync = now
        except OSError as exc:
            logger.warning("journal write failed for %s: %s (crash protection is off for the rest of this take)", self.path, exc)
            self.close()

    def append(self, event: Event) -> None:
        t, port, msg = event
        self._write([round(t - self._t0, 6), port, msg.hex()])

    def close(self) -> None:
        if self._file is not None:
            with contextlib.suppress(OSError):
                self._file.close()
            self._file = None

    def remove(self) -> None:
        """Close and delete the journal (the take was saved)."""
        self.close()
        with contextlib.suppress(OSError):
            self.path.unlink(missing_ok=True)


def load_journal(path: Path) -> tuple[datetime, list[Event]]:
    """Read a journal. Events get their elapsed seconds as time. Damaged lines (e.g. a write cut off by a crash) are skipped."""
    started: datetime | None = None
    events: list[Event] = []

    with path.open(encoding="utf-8") as fh:
        for number, line in enumerate(fh, start=1):
            try:
                record = json.loads(line)
                if number == 1:
                    started = datetime.fromisoformat(record["started"])
                else:
                    elapsed, port, text = record
                    events.append((float(elapsed), str(port), mido.Message.from_hex(text)))

            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("%s line %d skipped: %s", path.name, number, exc)

    if started is None:
        raise ValueError(f"{path.name}: no valid journal header")
    return started, events


def recover_journals(outdir: Path) -> list[Path]:
    """Turn journals left behind by a crash into .mid files; returns the files written."""
    recovered: list[Path] = []

    for journal in sorted(outdir.glob(f"*{JOURNAL_SUFFIX}")):
        try:
            started, events = load_journal(journal)
            if not events:
                logger.info("discarding empty journal %s", journal.name)
                journal.unlink()
                continue

            suffix = "_recovered" if (outdir / f"{file_stem(started)}.mid").exists() else ""
            path = save(events, started, outdir, suffix)
            journal.unlink()
            recovered.append(path)
            logger.warning("recovered %s from %s (%d events)", path.name, journal.name, len(events))

        except (OSError, ValueError) as exc:
            logger.error("cannot recover %s: %s (left in place)", journal.name, exc)

    return recovered


def session_loop(
    q: queue.Queue[Event],
    outdir: Path | str,
    idle: float,
    max_hold: float,
    status: Status | None = None,
    stop: threading.Event | None = None,
) -> None:
    """Block for the first event, collect until playing stops, save, repeat.

    Returns when `stop` is set, after saving the take in progress.
    """
    status = status or Status()
    stop = stop or threading.Event()
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    while not stop.is_set():
        try:
            first = q.get(timeout=POLL_SECONDS)
        except queue.Empty:
            continue

        started = datetime.now()
        events = [first]
        state = HoldState()
        state.update(first[1], first[2])
        journal = Journal(outdir / f"{file_stem(started)}{JOURNAL_SUFFIX}", started, first[0])
        journal.append(first)

        status.started = started
        status.events = 1
        status.state = "recording"
        logger.info("recording started")

        try:
            while not (stop.is_set() and q.empty()):  # on stop, still take what is already queued
                limit = max_hold if state.active else idle
                remaining = limit - (time.perf_counter() - events[-1][0])

                if remaining <= 0:
                    break
                try:
                    item = q.get(timeout=min(remaining, POLL_SECONDS))
                except queue.Empty:
                    continue

                events.append(item)
                state.update(item[1], item[2])
                journal.append(item)
                status.events = len(events)

        finally:  # also runs when the thread is interrupted, so the current take is not lost
            journal.close()
            path = save(events, started, outdir)
            journal.remove()  # only reached when save worked; otherwise the journal stays for recovery
            status.saved = (*status.saved, path)[-RECENT_FILES:]
            status.state = "idle"
            status.events = 0
            status.started = None
            logger.info("saved %s (%d events)", path, len(events))


def port_manager(q: queue.Queue[Event], stop: threading.Event, status: Status | None = None) -> None:
    """Open every input port, and keep rescanning for plugged/unplugged ones."""
    status = status or Status()
    open_ports: dict[str, mido.ports.BaseInput] = {}

    def make_callback(name: str) -> Callable[[mido.Message], None]:
        def on_msg(msg: mido.Message) -> None:  # keep tiny: runs in the MIDI driver thread
            if msg.type not in SKIP_TYPES:
                q.put((time.perf_counter(), name, msg))

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

        status.ports = tuple(sorted(open_ports))
        stop.wait(RESCAN_SECONDS)

    for port in open_ports.values():
        port.close()
    status.ports = ()


class Recorder:
    """Runs the port manager and the session loop in background threads; shared by the CLI and the GUI."""

    def __init__(self, outdir: Path, idle: float = IDLE_TIME, max_hold: float = MAX_HOLD) -> None:
        self.outdir = outdir
        self.idle = idle
        self.max_hold = max_hold
        self.status = Status()
        self._q: queue.Queue[Event] = queue.Queue()
        self._stop = threading.Event()
        self._threads: list[threading.Thread] = []

    def start(self) -> None:
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.status.saved = tuple(recover_journals(self.outdir))[-RECENT_FILES:]

        self._threads = [
            threading.Thread(target=session_loop, args=(self._q, self.outdir, self.idle, self.max_hold, self.status, self._stop), name="sessions", daemon=True),
            threading.Thread(target=port_manager, args=(self._q, self._stop, self.status), name="ports", daemon=True),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        """Stop listening and save the take in progress."""
        self._stop.set()
        for thread in self._threads:
            thread.join(timeout=5.0)
            if thread.is_alive():
                logger.warning("thread %s did not stop in time", thread.name)


def list_ports() -> int:
    """Print the MIDI input ports; a missing MIDI backend gives a short error instead of a traceback."""
    try:
        names = mido.get_input_names()
    except Exception as exc:
        print(f"error: cannot list MIDI input ports: {exc}", file=sys.stderr)
        print("hint: on Linux this needs ALSA (a sound system with /dev/snd/seq); on Windows/macOS reinstall python-rtmidi.", file=sys.stderr)
        return 1

    if not names:
        print("no MIDI input ports found", file=sys.stderr)
    for name in names:
        print(name)
    return 0


def add_common_arguments(ap: argparse.ArgumentParser) -> None:
    """Options shared by the command line and the GUI."""
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
    ap.add_argument("--log-level", type=str.upper, choices=LOG_LEVELS, default="INFO", help="log level (default %(default)s)")
    ap.add_argument("--log-file", type=Path, default=None, help=f"log file (default: <outdir>/{LOG_FILE_NAME})")
    ap.add_argument("--no-log-file", action="store_true", help="log to stderr only")


def prepare(args: argparse.Namespace) -> Path:
    """Create the output directory and configure logging; returns the output directory."""
    outdir = Path(args.outdir)
    outdir.mkdir(parents=True, exist_ok=True)
    log_file = None if args.no_log_file else (args.log_file or outdir / LOG_FILE_NAME)
    setup_logging(args.log_level, log_file)
    return outdir


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    add_common_arguments(ap)
    ap.add_argument("--list", action="store_true", help="list input ports and exit")
    args = ap.parse_args()

    if args.list:
        return list_ports()

    outdir = prepare(args)
    recorder = Recorder(outdir, args.idle, args.max_hold)
    recorder.start()
    logger.info("midi recorder active, writing to %s (Ctrl-C to quit)", outdir.resolve())

    try:
        while True:
            time.sleep(1.0)

    except KeyboardInterrupt:
        pass

    finally:
        recorder.stop()

    return 0


if __name__ == "__main__":
    sys.exit(main())
