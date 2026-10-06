"""Frozen behaviour of the crash-safety journal: its on-disk format, failure handling and recovery."""

import io
import json
import logging
import os
from datetime import datetime
from pathlib import Path

import mido
import pytest
from helpers import note, pedal

import midi_recorder as m

STARTED = datetime(2026, 10, 5, 9, 40, 4)


def _lines(path: Path) -> list[str]:
    return path.read_text(encoding="utf-8").splitlines()


class TestFormat:
    def test_header_and_event_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "x.journal"
        journal = m.Journal(path, STARTED, t0=10.0)
        journal.append((10.5, "Port A", note("note_on", 60, velocity=90)))

        # readable while still open: every line is flushed as it is written
        header, event = _lines(path)
        assert json.loads(header) == {"version": 1, "started": "2026-10-05T09:40:04"}
        assert json.loads(event) == [0.5, "Port A", "90 3C 5A"]
        journal.close()

    def test_elapsed_time_is_rounded_to_microseconds(self, tmp_path: Path) -> None:
        journal = m.Journal(tmp_path / "x.journal", STARTED, t0=0.0)
        journal.append((0.1234567891, "A", note("note_on")))
        journal.close()
        assert json.loads(_lines(tmp_path / "x.journal")[1])[0] == 0.123457

    def test_unicode_and_quotes_in_port_names_roundtrip(self, tmp_path: Path) -> None:
        path = tmp_path / "x.journal"
        name = 'Pian\u00f6 "X" 20:0'
        journal = m.Journal(path, STARTED, t0=0.0)
        journal.append((0.0, name, note("note_on")))
        journal.close()
        _, events = m.load_journal(path)
        assert events[0][1] == name

    def test_sysex_survives_the_journal(self, tmp_path: Path) -> None:
        path = tmp_path / "x.journal"
        journal = m.Journal(path, STARTED, t0=0.0)
        journal.append((0.0, "A", mido.Message("sysex", data=[1, 2, 3])))
        journal.close()
        assert m.load_journal(path)[1][0][2] == mido.Message("sysex", data=[1, 2, 3])

    def test_remove_deletes_the_file_and_close_is_repeatable(self, tmp_path: Path) -> None:
        path = tmp_path / "x.journal"
        journal = m.Journal(path, STARTED, t0=0.0)
        journal.close()
        journal.close()
        assert path.exists()
        journal.remove()
        assert not path.exists()
        journal.remove()  # already gone: no error


class TestSync:
    def test_fsync_is_throttled(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        calls: list[int] = []
        monkeypatch.setattr(os, "fsync", lambda fd: calls.append(fd))

        monkeypatch.setattr(m, "JOURNAL_SYNC_SECONDS", 1000.0)
        slow = m.Journal(tmp_path / "slow.journal", STARTED, t0=0.0)
        for i in range(5):
            slow.append((float(i), "A", note("note_on")))
        slow.close()
        assert calls == []  # flushed to the OS every time, fsynced only once per interval

        monkeypatch.setattr(m, "JOURNAL_SYNC_SECONDS", 0.0)
        eager = m.Journal(tmp_path / "eager.journal", STARTED, t0=0.0)
        for i in range(5):
            eager.append((float(i), "A", note("note_on")))
        eager.close()
        assert len(calls) == 6  # the header and each of the 5 events


def _has_file(journal: m.Journal) -> bool:
    return journal._file is not None


class TestFailures:
    def test_unwritable_location_never_raises(self, tmp_path: Path, log_records: list[logging.LogRecord]) -> None:
        journal = m.Journal(tmp_path / "missing_dir" / "x.journal", STARTED, t0=0.0)
        journal.append((0.0, "A", note("note_on")))
        journal.remove()
        warnings = [r for r in log_records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "crash protection is off" in warnings[0].getMessage()

    def test_write_error_disables_the_journal_and_is_logged_once(self, tmp_path: Path, log_records: list[logging.LogRecord]) -> None:
        class Broken(io.StringIO):
            def write(self, s: str, /) -> int:
                raise OSError("disk full")

        journal = m.Journal(tmp_path / "x.journal", STARTED, t0=0.0)
        journal._file = Broken()
        for i in range(3):
            journal.append((float(i), "A", note("note_on")))

        assert not _has_file(journal)
        assert sum("journal write failed" in r.getMessage() for r in log_records) == 1


class TestLoad:
    def _write(self, path: Path, *lines: str) -> None:
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    def test_events_come_back_in_order(self, tmp_path: Path) -> None:
        path = tmp_path / "x.journal"
        journal = m.Journal(path, STARTED, t0=5.0)
        sent = [(5.0, "A", note("note_on")), (5.5, "B", pedal(127)), (6.0, "A", note("note_off"))]
        for ev in sent:
            journal.append(ev)
        journal.close()

        started, events = m.load_journal(path)
        assert started == STARTED
        assert [(t, p, msg) for t, p, msg in events] == [(0.0, "A", sent[0][2]), (0.5, "B", sent[1][2]), (1.0, "A", sent[2][2])]

    def test_header_only_journal_has_no_events(self, tmp_path: Path) -> None:
        path = tmp_path / "x.journal"
        m.Journal(path, STARTED, t0=0.0).close()
        assert m.load_journal(path) == (STARTED, [])

    @pytest.mark.parametrize("content", ["", "not json\n", '{"version": 1}\n'])
    def test_missing_or_bad_header_is_an_error(self, tmp_path: Path, content: str) -> None:
        path = tmp_path / "x.journal"
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError, match="no valid journal header"):
            m.load_journal(path)

    def test_damaged_lines_are_skipped_with_a_warning_each(self, tmp_path: Path, log_records: list[logging.LogRecord]) -> None:
        path = tmp_path / "x.journal"
        self._write(
            path,
            '{"version": 1, "started": "2026-10-05T09:40:04"}',
            '[0.0, "A", "90 3C 5A"]',
            "garbage",  # not JSON
            '[0.1, "A"]',  # wrong shape
            '[0.2, "A", "zz zz"]',  # not MIDI
            '[0.3, "A", "80 3C 00"]',
            '[0.4, "A", "90 3',  # cut off by a crash
        )
        _, events = m.load_journal(path)
        assert [e[0] for e in events] == [0.0, 0.3]
        assert sum("skipped" in r.getMessage() for r in log_records) == 4


class TestRecovery:
    def _journal(self, path: Path, started: datetime) -> None:
        journal = m.Journal(path, started, t0=0.0)
        journal.append((0.0, "A", note("note_on")))
        journal.append((0.5, "A", note("note_off")))
        journal.close()

    def test_several_journals_are_recovered_in_name_order(self, tmp_path: Path, log_records: list[logging.LogRecord]) -> None:
        self._journal(tmp_path / "2026-10-05_10-00-00.journal", datetime(2026, 10, 5, 10, 0, 0))
        self._journal(tmp_path / "2026-10-05_09-00-00.journal", datetime(2026, 10, 5, 9, 0, 0))

        recovered = m.recover_journals(tmp_path)

        assert [p.name for p in recovered] == ["2026-10-05_09-00-00.mid", "2026-10-05_10-00-00.mid"]
        assert not list(tmp_path.glob("*.journal"))
        assert sum(r.levelno == logging.WARNING and "recovered" in r.getMessage() for r in log_records) == 2

    def test_other_files_are_left_alone(self, tmp_path: Path) -> None:
        (tmp_path / "notes.txt").write_text("hi")
        (tmp_path / "2026-01-01_00-00-00.mid").write_bytes(b"x")
        assert m.recover_journals(tmp_path) == []
        assert sorted(p.name for p in tmp_path.iterdir()) == ["2026-01-01_00-00-00.mid", "notes.txt"]

    def test_a_failed_save_keeps_the_journal_for_the_next_start(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        self._journal(tmp_path / "2026-10-05_09-00-00.journal", datetime(2026, 10, 5, 9, 0, 0))
        real_save = m.save

        def broken(*args: object, **kwargs: object) -> Path:
            raise OSError("disk full")

        monkeypatch.setattr(m, "save", broken)
        assert m.recover_journals(tmp_path) == []
        assert (tmp_path / "2026-10-05_09-00-00.journal").exists()

        monkeypatch.setattr(m, "save", real_save)
        assert [p.name for p in m.recover_journals(tmp_path)] == ["2026-10-05_09-00-00.mid"]
