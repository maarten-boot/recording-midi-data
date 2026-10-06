# recording-midi-data

**midi-recorder** records everything that comes in on your MIDI inputs and saves it as a standard MIDI file.
There is nothing to press: the first note starts a recording, and when you stop playing the file is closed and saved.

Each file is named after the moment the recording started: `2026-10-05_21-35-12.mid`.

- Notes, sustain and other pedals, channel and polyphonic aftertouch, pitch bend and all other messages are kept. Only `clock` and `active_sensing` are dropped.
- Listens on **all** MIDI inputs at once; inputs plugged in later are picked up automatically.
- Plays back in real time in any DAW (1 tick = 1 ms).
- Crash safe: a journal is written while you play, and recovered at the next start.
- Command line, plus a small status window.
- Linux, Windows and macOS.

There is no live display of the notes; open the finished file in your DAW for that.

## Install

```
pipx install midi-recorder        # or: pip install midi-recorder
```

Python 3.10 - 3.12. The MIDI backend `python-rtmidi` has no prebuilt wheels for Python 3.13 yet, so on 3.13 pip would have to compile it.

| System | Notes |
|---|---|
| Linux (Debian, Ubuntu, Fedora) | Needs the ALSA sequencer, which any normal desktop install has. The status window needs tkinter: `sudo apt install python3-tk` (Debian/Ubuntu) or `sudo dnf install python3-tkinter` (Fedora). |
| Windows 10, 11 | A MIDI input can usually be opened by only one program at a time. Close your DAW first, or route the keyboard through a loopback driver such as loopMIDI so both can listen. |
| macOS | Should work through CoreMIDI. The automated tests run on macOS, but it has not been tried with a real keyboard. |

## Standalone binaries (no Python needed)

Each GitHub release also has archives with both programs for Linux (x86_64), Windows (x86_64) and macOS (Apple silicon), built with PyInstaller.

- Linux: needs glibc 2.35 or newer (Ubuntu 22.04+, Debian 12+, current Fedora). Unpack and run `./midi-recorder` or `./midi-recorder-gui`.
- Windows: `midi-recorder.exe` (console) and `midi-recorder-gui.exe` (window). The files are not signed, so SmartScreen will warn on first start.
- macOS: `midi-recorder` and `MIDI Recorder.app`. Not signed or notarized: open the app the first time with right-click, Open.

To build them yourself, run `make binary` on the system you want them for; they appear in `build/bin`.

## Quick start

```
midi-recorder --list              # show the MIDI inputs it can see
midi-recorder -o ~/midi           # record into ~/midi, stop with Ctrl-C
midi-recorder-gui -o ~/midi       # the same, with a status window
```

`midi-recorder --list` prints a short error instead of a traceback when no MIDI backend is available.

If you leave out `-o`, files go into the current directory.

## How a recording works

1. A recording **starts** with the first event from any input.
2. It **ends** when no event has arrived for `--idle` seconds (default 30), *and* no note is held and no sustain pedal is down. While a key or the pedal is held, it waits up to `--max-hold` seconds (default 120) instead, so a long held chord does not cut the file, and a stuck note cannot keep it open forever.
3. The file is written as a type 1 MIDI file: a tempo track plus **one track per input**, named after the input. With only one keyboard you get one note track.
4. Ctrl-C (or closing the window) saves the recording in progress before exiting.

Timestamps are taken when the event reaches the program, so their accuracy is whatever your operating system's MIDI driver and scheduling give you, typically about a millisecond.

## Options

Both programs take the same options.

| Option | Default | Meaning |
|---|---|---|
| `-o`, `--outdir` | `.` | Folder for the `.mid` files, created if missing. |
| `--idle` | `30` | Seconds of silence that end a recording. |
| `--max-hold` | `120` | Longest silence allowed while a note or the sustain pedal is held. |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR`. |
| `--log-file` | `<outdir>/midi_recorder.log` | Where to write the log. |
| `--no-log-file` | | Log to stderr only. |
| `--list` | | `midi-recorder` only: list the MIDI inputs and exit. |

## Status window

`midi-recorder-gui` shows whether it is **recording** (with the number of events and the elapsed time) or **ready**, the inputs it is listening on, the recordings saved so far, and a button that opens the output folder.
Closing the window saves the recording in progress.

## If it crashes

While you play, every event is also appended to `YYYY-MM-DD_HH-MM-SS.journal` in the output folder, and the file is flushed as events arrive.
When a recording is saved normally the journal is deleted.
If the program or the computer dies mid-recording, the next start turns each leftover journal into a `.mid` file and logs a warning. A write that was cut off by the crash is skipped. If a `.mid` with that name already exists, the recovered file gets `_recovered` in its name instead of overwriting it.

Run only one recorder per output folder, otherwise one could mistake the other's live journal for a crash.

## Logging

Messages go to stderr and to `<outdir>/midi_recorder.log`, which is rotated at 1 MB and keeps 3 old files. A failing MIDI backend or an input that cannot be opened is logged once, not every few seconds.

## Development

```
git clone https://github.com/maarten-boot/recording-midi-data
cd recording-midi-data
make check        # ruff, ruff format --check, mypy --strict, pytest (creates .venv)
make all          # the same from a fresh venv, also runs ruff format
make build        # checks, then builds the sdist and wheel into dist/ and runs twine check
make binary       # standalone executables for this system into build/bin, plus a smoke test
make run ARGS="-o ~/midi"
```

`make` with no target lists everything. The checks run on Linux, Windows and macOS in GitHub Actions.

`mido` ships no type information, so a minimal stub for the part this program uses lives in `stubs/mido/`.

## License

MIT, see `LICENSE`.

Thanks Claude.
