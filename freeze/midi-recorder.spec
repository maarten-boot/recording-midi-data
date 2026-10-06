# PyInstaller build description for midi-recorder: both programs as standalone executables.
#
#   make binary
#   (or: pyinstaller --noconfirm --clean --distpath build/bin --workpath build/pyinstaller freeze/midi-recorder.spec)
#
# Build on each operating system separately: PyInstaller cannot cross-compile.
# Output in build/bin, NOT dist/: `make testpypi` and `make pypi` upload everything in dist/.
#   Linux, Windows: midi-recorder[.exe]  midi-recorder-gui[.exe]       (one file each)
#   macOS:          midi-recorder        "MIDI Recorder.app"            (PyInstaller has no one-file app bundles)
# This file is Python, run by PyInstaller, which provides Analysis, PYZ, EXE, COLLECT, BUNDLE and SPECPATH.

import os
import re
import sys

ROOT = os.path.dirname(SPECPATH)  # SPECPATH is this file's folder; the sources are one level up
with open(os.path.join(ROOT, "midi_recorder.py"), encoding="utf-8") as fh:
    VERSION = re.search(r'^__version__ = "(.+)"', fh.read(), re.M).group(1)

# mido imports its MIDI backend by name at run time (importlib), so PyInstaller cannot see it.
# Without this the binary starts but reports "No module named 'mido.backends.rtmidi'".
HIDDEN_IMPORTS = ["mido.backends.rtmidi"]


def analysis(script):
    return Analysis([os.path.join(ROOT, script)], pathex=[ROOT], hiddenimports=HIDDEN_IMPORTS)


# --- command line program, one file
cli = analysis("midi_recorder.py")
EXE(PYZ(cli.pure), cli.scripts, cli.binaries, cli.datas, [], name="midi-recorder", console=True, upx=False)

# --- status window
gui = analysis("midi_recorder_gui.py")
if sys.platform == "darwin":
    gui_exe = EXE(PYZ(gui.pure), gui.scripts, [], exclude_binaries=True, name="midi-recorder-gui", console=False, upx=False)
    BUNDLE(
        COLLECT(gui_exe, gui.binaries, gui.datas, name="midi-recorder-gui-files", upx=False),
        name="MIDI Recorder.app",
        bundle_identifier="io.github.maarten-boot.midi-recorder",
        version=VERSION,
    )
else:
    EXE(PYZ(gui.pure), gui.scripts, gui.binaries, gui.datas, [], name="midi-recorder-gui", console=False, upx=False)
