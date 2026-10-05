#!/usr/bin/env python3
"""midi_recorder: record every incoming MIDI event from all input ports.

* A session starts at the first event and ends when playing has stopped
  (no events for --idle seconds, and no notes held / sustain pedal up).
* Files are named after the start of the session: YYYY-MM-DD_HH-MM-SS.mid
* Every event is kept (notes, pedals, aftertouch, pitch bend, ...), except
  realtime noise (clock, active_sensing).
* One track per input port. 1 tick = 1 ms, so files play back in real time.
* Ports that are plugged in later are picked up automatically.

Requires: pip install mido python-rtmidi
"""
import argparse
import queue
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

import mido

SKIP_TYPES = {"clock", "active_sensing"}
SKIP_PORT_PARTS = ("midi through", "rtmidi")  # loopback / our own ports
RESCAN_SECONDS = 2.0
TICKS_PER_BEAT = 1000  # with tempo 1_000_000 us/beat -> 1 tick = 1 ms
IDLE_TIME = 30.0
MAX_HOLD = 120.0
DEFAULT_TEMPO = 1_000_000

def log(text):
    print(f"{datetime.now():%H:%M:%S} {text}", flush=True)


class HoldState:
    """Tracks held notes and sustain pedal per (port, channel)."""

    def __init__(self):
        self.notes = set()
        self.sustain = set()

    def update(self, port, msg):
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
    def active(self):
        return bool(self.notes or self.sustain)


def save(events, started, outdir):
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

            tick = round((t - t0) * 1000)
            track.append(msg.copy(time=tick - last_tick))  # delta from absolute
            last_tick = tick

        track.append(mido.MetaMessage("end_of_track", time=0))
        mid.tracks.append(track)

    path = Path(outdir) / f"{started:%Y-%m-%d_%H-%M-%S}.mid"
    mid.save(path)
    return path


def session_loop(q, outdir, idle, max_hold):
    """Block for the first event, collect until playing stops, save, repeat."""
    outdir = Path(outdir)
    outdir.mkdir(parents=True, exist_ok=True)

    while True:
        first = q.get()
        started = datetime.now()
        events = [first]
        state = HoldState()
        state.update(first[1], first[2])
        log("recording started")

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
            log(f"saved {path} ({len(events)} events)")


def port_manager(q, stop):
    """Open every input port, and keep rescanning for plugged/unplugged ones."""
    open_ports = {}

    def make_callback(name):
        def on_msg(msg):  # keep tiny: runs in the MIDI driver thread
            if msg.type not in SKIP_TYPES:
                q.put((time.monotonic(), name, msg))
        return on_msg

    while not stop.is_set():
        try:
            names = {
                n for n in mido.get_input_names()
                if not any(part in n.lower() for part in SKIP_PORT_PARTS)
            }

        except Exception as exc:  # backend hiccup, try again next round
            log(f"port scan failed: {exc}")
            names = set(open_ports)

        for name in names - set(open_ports):
            try:
                open_ports[name] = mido.open_input(name, callback=make_callback(name))
                log(f"listening on: {name}")

            except Exception as exc:
                log(f"cannot open {name}: {exc}")

        for name in set(open_ports) - names:
            try:
                open_ports.pop(name).close()

            except Exception:
                pass
            log(f"input gone: {name}")

        stop.wait(RESCAN_SECONDS)

    for port in open_ports.values():
        port.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("-o", "--outdir", default=".", help="output directory")
    ap.add_argument("--idle", type=float, default=IDLE_TIME,
                    help="seconds of silence that end a recording (default 10)")
    ap.add_argument("--max-hold", type=float, default=MAX_HOLD,
                    help="max silence while notes/pedal are held (default 120)")
    ap.add_argument("--list", action="store_true", help="list input ports and exit")
    args = ap.parse_args()

    if args.list:
        for n in mido.get_input_names():
            print(n)
        return 0

    q = queue.Queue()
    stop = threading.Event()
    threading.Thread(target=port_manager, args=(q, stop), daemon=True,).start()
    log(f"midi recorder active, writing to {Path(args.outdir).resolve()} (Ctrl-C to quit)")

    try:
        session_loop(q, args.outdir, args.idle, args.max_hold)

    except KeyboardInterrupt:
        pass

    finally:
        stop.set()

    return 0


if __name__ == "__main__":
    sys.exit(main())
