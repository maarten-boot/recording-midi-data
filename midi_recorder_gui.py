#!/usr/bin/env python3
"""midi_recorder_gui: a small status window for midi_recorder.

Shows whether the recorder is idle or recording, the open MIDI inputs and the recently saved files.
Takes the same options as midi_recorder.py (--outdir, --idle, --max-hold, --log-*).
On Debian/Ubuntu the tkinter module comes from `sudo apt install python3-tk`, on Fedora from `sudo dnf install python3-tkinter`.
"""

import argparse
import os
import subprocess
import sys
import tkinter as tk
from datetime import datetime
from pathlib import Path
from tkinter import ttk

import midi_recorder as rec

REFRESH_MS = 250
COLOR_RECORDING = "#c0392b"
COLOR_READY = "#1e8449"
COLOR_WARNING = "#b9770e"


def open_folder(path: Path) -> None:
    """Show a folder in the platform's file manager."""
    try:
        if sys.platform == "win32":
            os.startfile(path)
        elif sys.platform == "darwin":
            subprocess.run(["open", str(path)], check=False)
        else:
            subprocess.run(["xdg-open", str(path)], check=False)
    except OSError as exc:
        rec.logger.warning("cannot open folder %s: %s", path, exc)


class App:
    def __init__(self, root: tk.Tk, recorder: rec.Recorder) -> None:
        self.root = root
        self.recorder = recorder
        self._ports_shown: tuple[str, ...] | None = None
        self._files_shown: tuple[Path, ...] | None = None

        root.title("MIDI recorder")
        root.minsize(420, 360)
        frame = ttk.Frame(root, padding=12)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(0, weight=1)
        frame.rowconfigure(5, weight=1)

        self.state_var = tk.StringVar()
        self.state_label = tk.Label(frame, textvariable=self.state_var, font=("TkDefaultFont", 18, "bold"), anchor="w")
        self.state_label.grid(row=0, column=0, columnspan=2, sticky="ew")

        self.detail_var = tk.StringVar()
        ttk.Label(frame, textvariable=self.detail_var).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 8))

        ttk.Label(frame, text="MIDI inputs").grid(row=2, column=0, sticky="w")
        self.ports_list = tk.Listbox(frame, height=3, activestyle="none")
        self.ports_list.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 8))

        ttk.Label(frame, text="Recent recordings (newest first)").grid(row=4, column=0, sticky="w")
        self.files_list = tk.Listbox(frame, height=8, activestyle="none")
        self.files_list.grid(row=5, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frame, orient="vertical", command=self.files_list.yview)
        scroll.grid(row=5, column=1, sticky="ns")
        self.files_list.configure(yscrollcommand=scroll.set)

        ttk.Button(frame, text="Open folder", command=lambda: open_folder(recorder.outdir)).grid(row=6, column=0, sticky="w", pady=(8, 0))
        ttk.Label(frame, text=str(recorder.outdir.resolve()), foreground="gray").grid(row=7, column=0, columnspan=2, sticky="w")

        self.refresh()

    def refresh(self) -> None:
        status = self.recorder.status

        if status.state == "recording":
            self.state_var.set("\u25cf RECORDING")
            self.state_label.configure(fg=COLOR_RECORDING)
            self.detail_var.set(f"{status.events} events   {status.elapsed_text(datetime.now())}")
        elif status.ports:
            self.state_var.set("Ready \u2013 waiting for MIDI")
            self.state_label.configure(fg=COLOR_READY)
            self.detail_var.set(f"Recording stops after {self.recorder.idle:g} s of silence")
        else:
            self.state_var.set("No MIDI input found")
            self.state_label.configure(fg=COLOR_WARNING)
            self.detail_var.set("Connect a MIDI device; it is picked up automatically")

        if status.ports != self._ports_shown:  # only touch a listbox when its content changed
            self._ports_shown = status.ports
            self.ports_list.delete(0, "end")
            for name in status.ports:
                self.ports_list.insert("end", name)

        if status.saved != self._files_shown:
            self._files_shown = status.saved
            self.files_list.delete(0, "end")
            for path in reversed(status.saved):
                self.files_list.insert("end", path.name)

        self.root.after(REFRESH_MS, self.refresh)

    def close(self) -> None:
        self.root.destroy()


def main() -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    rec.add_common_arguments(ap)
    args = ap.parse_args()

    try:
        root = tk.Tk()
    except tk.TclError as exc:
        print(f"error: cannot open a window: {exc}", file=sys.stderr)
        return 1

    outdir = rec.prepare(args)
    recorder = rec.Recorder(outdir, args.idle, args.max_hold)
    recorder.start()
    rec.logger.info("midi recorder (GUI) active, writing to %s", outdir.resolve())

    app = App(root, recorder)
    root.protocol("WM_DELETE_WINDOW", app.close)

    try:
        root.mainloop()
    finally:
        recorder.stop()  # saves the take in progress

    return 0


if __name__ == "__main__":
    sys.exit(main())
