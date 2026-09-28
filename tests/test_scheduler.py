"""Scheduler (Part 3A) — plist generation + install/status without launchd.

These tests never touch launchctl: they exercise the PURE plist builder and the
portable file-writing paths against a temp HOME + data dir. The launchctl calls
are macOS-only and guarded by `Scheduler.is_macos()`, so they don't run here.
"""

from __future__ import annotations

import plistlib
from pathlib import Path

import pytest

from delium.scheduler import LABEL, Scheduler, build_plist, parse_times


def test_parse_times_single_and_multiple() -> None:
    assert parse_times("06:00") == ((6, 0),)
    assert parse_times("06:00,18:30") == ((6, 0), (18, 30))
    # De-duplicated and sorted.
    assert parse_times("18:00,06:00,06:00") == ((6, 0), (18, 0))


@pytest.mark.parametrize("bad", ["", "6", "25:00", "06:60", "aa:bb", ","])
def test_parse_times_rejects_bad(bad: str) -> None:
    with pytest.raises(ValueError):
        parse_times(bad)


def test_build_plist_single_time_is_a_dict() -> None:
    xml = build_plist(
        label=LABEL,
        uv_path="/opt/uv",
        repo_dir="/repo",
        times=((6, 0),),
        log_path="/repo/data/scheduled-scan.log",
    )
    data = plistlib.loads(xml.encode("utf-8"))
    assert data["Label"] == LABEL
    # Absolute uv + the scan --scheduled command.
    assert data["ProgramArguments"] == ["/opt/uv", "run", "delium", "scan", "--scheduled"]
    assert data["WorkingDirectory"] == "/repo"
    assert data["StandardOutPath"] == "/repo/data/scheduled-scan.log"
    assert data["StandardErrorPath"] == "/repo/data/scheduled-scan.log"
    # A single time is encoded as one dict, not a list.
    assert data["StartCalendarInterval"] == {"Hour": 6, "Minute": 0}


def test_build_plist_multiple_times_is_a_list() -> None:
    xml = build_plist(
        label=LABEL,
        uv_path="/opt/uv",
        repo_dir="/repo",
        times=((6, 0), (18, 0)),
        log_path="/repo/data/log",
    )
    data = plistlib.loads(xml.encode("utf-8"))
    assert data["StartCalendarInterval"] == [
        {"Hour": 6, "Minute": 0},
        {"Hour": 18, "Minute": 0},
    ]


def test_write_plist_and_read_times_back(tmp_path: Path) -> None:
    sched = Scheduler(home=tmp_path / "home", data_dir=tmp_path / "data")
    assert not sched.status().installed
    path = sched.write_plist(((6, 0), (18, 30)))
    assert path.exists()
    assert path == tmp_path / "home" / "Library" / "LaunchAgents" / f"{LABEL}.plist"
    st = sched.status()
    assert st.installed
    assert st.times == ((6, 0), (18, 30))


def test_uninstall_removes_plist(tmp_path: Path) -> None:
    sched = Scheduler(home=tmp_path / "home", data_dir=tmp_path / "data")
    sched.write_plist(((6, 0),))
    assert sched.uninstall() is True
    assert not sched.plist_path.exists()
    # Idempotent: a second uninstall reports nothing was installed.
    assert sched.uninstall() is False


def test_scan_completed_on_detects_todays_run(initialized_db: Path) -> None:
    """The skip-today guard: a completed scan today is found; a different day is
    not. This is what stops a wake-triggered launchd catch-up double-spending."""
    from datetime import UTC, datetime

    from delium.database import get_connection, repository

    today = datetime.now(UTC).date().isoformat()
    with get_connection() as conn:
        assert repository.scan_completed_on(conn, today) is None
        run_id = repository.insert_run(conn, command="scan", input_="US")
        scan_id = repository.create_scan(
            conn, run_id=run_id, marketplaces=["US"], profile_id=None, profile_name="p", params={}
        )
        # Still running → not counted.
        assert repository.scan_completed_on(conn, today) is None
        repository.update_scan(conn, scan_id, status="complete")
        conn.commit()
        found = repository.scan_completed_on(conn, today)
        assert found is not None and found["id"] == scan_id
        # A different calendar day has no completed scan.
        assert repository.scan_completed_on(conn, "2000-01-01") is None


def test_log_tail_reads_last_lines(tmp_path: Path) -> None:
    sched = Scheduler(home=tmp_path / "home", data_dir=tmp_path / "data")
    sched.log_path.parent.mkdir(parents=True, exist_ok=True)
    sched.log_path.write_text("\n".join(f"line {i}" for i in range(50)))
    tail = sched.log_tail(lines=5)
    assert tail.splitlines() == ["line 45", "line 46", "line 47", "line 48", "line 49"]
