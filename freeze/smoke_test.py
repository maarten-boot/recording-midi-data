"""Smoke test for the PyInstaller binaries: they start, and the MIDI backend and tkinter are inside them.

Usage: python freeze/smoke_test.py [build/bin]

It needs no MIDI device: on a machine without one, `--list` reports a backend error, which is fine;
"No module named ..." or a traceback is not.
"""

import os
import subprocess
import sys
from pathlib import Path

TIMEOUT = 120


def executable(bindir: Path, name: str) -> Path:
    return bindir / (name + ".exe" if sys.platform == "win32" else name)


def gui_executable(bindir: Path) -> Path:
    if sys.platform == "darwin":
        return bindir / "MIDI Recorder.app" / "Contents" / "MacOS" / "midi-recorder-gui"
    return executable(bindir, "midi-recorder-gui")


def run(cmd: list[str], env: dict[str, str] | None = None) -> tuple[int, str]:
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT, env=env)
    return result.returncode, result.stdout + result.stderr


def check(bindir: Path) -> list[str]:
    """Returns the problems found; empty means the binaries are fine."""
    problems: list[str] = []
    cli = executable(bindir, "midi-recorder")
    gui = gui_executable(bindir)

    for path in (cli, gui):
        if not path.is_file():
            problems.append(f"missing: {path}")
    if problems:
        return problems

    code, out = run([str(cli), "--help"])
    if code != 0 or "usage:" not in out:
        problems.append(f"{cli.name} --help: exit {code}\n{out}")

    code, out = run([str(cli), "--list"])  # 0 with a MIDI backend, 1 with a clean "error:" without one
    if code not in (0, 1) or "No module named" in out or "Traceback" in out:
        problems.append(f"{cli.name} --list: exit {code}, the MIDI backend is not usable\n{out}")

    code, out = run([str(gui), "--help"])  # a windowed Windows program has no stdout, so only the exit code counts
    if code != 0:
        problems.append(f"{gui.name} --help: exit {code}\n{out}")

    if sys.platform.startswith("linux"):  # without a display tkinter must load and report it cleanly
        env = {k: v for k, v in os.environ.items() if k not in ("DISPLAY", "WAYLAND_DISPLAY")}
        code, out = run([str(gui), "--no-log-file", "-o", str(bindir / "smoke-test-output")], env=env)
        if code != 1 or "error: cannot open a window" not in out:
            problems.append(f"{gui.name} without a display: exit {code}, expected a clean error (is tkinter bundled?)\n{out}")

    return problems


def main(argv: list[str]) -> int:
    bindir = Path(argv[1]) if len(argv) > 1 else Path("build/bin")
    problems = check(bindir)
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if not problems:
        print(f"ok: binaries in {bindir} start, and include the MIDI backend")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
