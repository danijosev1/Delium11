"""macOS launchd scheduler for the Daily Scan (Part 3A).

A tiny, testable wrapper around a launchd *user agent* that runs
`uv run delium scan --scheduled` once (or more) per day. The plist is always
GENERATED here and loaded with `launchctl` — never hand-edited — so the schedule
and the paths stay consistent.

Design:
  * `build_plist(...)` is PURE (string in, XML string out) so tests can assert the
    generated plist without touching launchd.
  * `Scheduler` owns the concrete locations (plist under ~/Library/LaunchAgents,
    a log file under the repo data dir) and the launchctl calls. The launchctl
    calls only run on macOS; every other method is portable.
  * Absolute paths to `uv` and the repo are baked into the plist, because a
    launchd agent runs with a minimal environment and an unknown working dir.
  * A sleeping Mac: launchd runs a missed StartCalendarInterval on the next wake.
    The scheduled scan path itself skips if a scan already completed today, so a
    wake-triggered catch-up never double-spends.

`notify(...)` posts a macOS banner (osascript) when a scan finishes or aborts;
it is a no-op off macOS or when osascript is missing.
"""

from __future__ import annotations

import plistlib
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from delium.utils.paths import get_data_dir

LABEL = "com.delium.dailyscan"
DEFAULT_TIMES: tuple[str, ...] = ("06:00",)


def parse_times(spec: str) -> tuple[tuple[int, int], ...]:
    """Parse a `--times` spec ("06:00" or "06:00,18:00") into (hour, minute)
    pairs. Raises ValueError on a malformed or out-of-range time."""
    out: list[tuple[int, int]] = []
    for chunk in spec.split(","):
        token = chunk.strip()
        if not token:
            continue
        if ":" not in token:
            raise ValueError(f"time {token!r} must be HH:MM")
        hh, mm = token.split(":", 1)
        try:
            hour, minute = int(hh), int(mm)
        except ValueError as exc:
            raise ValueError(f"time {token!r} must be HH:MM") from exc
        if not (0 <= hour <= 23 and 0 <= minute <= 59):
            raise ValueError(f"time {token!r} is out of range")
        out.append((hour, minute))
    if not out:
        raise ValueError("at least one time is required")
    # Stable, de-duplicated order.
    return tuple(sorted(set(out)))


def build_plist(
    *,
    label: str,
    uv_path: str,
    repo_dir: str,
    times: tuple[tuple[int, int], ...],
    log_path: str,
    scan_args: tuple[str, ...] = ("scan", "--scheduled"),
) -> str:
    """Render the launchd plist as an XML string (pure). The agent runs
    `<uv_path> run delium <scan_args...>` from `repo_dir` at each of `times`,
    appending stdout+stderr to `log_path`."""
    entries = [{"Hour": h, "Minute": m} for h, m in times]
    # launchd accepts either a single dict or a list of dicts.
    calendar: list[dict[str, int]] | dict[str, int] = entries[0] if len(entries) == 1 else entries
    program = [uv_path, "run", "delium", *scan_args]
    payload: dict[str, object] = {
        "Label": label,
        "ProgramArguments": program,
        "WorkingDirectory": repo_dir,
        "StartCalendarInterval": calendar,
        "StandardOutPath": log_path,
        "StandardErrorPath": log_path,
        # Run a missed interval when the Mac wakes, rather than skipping it.
        "RunAtLoad": False,
        "ProcessType": "Background",
    }
    return plistlib.dumps(payload, sort_keys=False).decode("utf-8")


@dataclass(frozen=True)
class ScheduleStatus:
    installed: bool
    plist_path: str
    times: tuple[tuple[int, int], ...]
    log_path: str
    log_tail: str


class Scheduler:
    """Concrete launchd locations + the launchctl calls. Construct with defaults;
    tests can inject a temp `home` and `data_dir` to keep everything on disk."""

    def __init__(
        self,
        *,
        home: Path | None = None,
        data_dir: Path | None = None,
        label: str = LABEL,
    ) -> None:
        self.label = label
        self._home = home or Path.home()
        self._data_dir = data_dir or get_data_dir()

    # -- locations ---------------------------------------------------------
    @property
    def plist_path(self) -> Path:
        return self._home / "Library" / "LaunchAgents" / f"{self.label}.plist"

    @property
    def log_path(self) -> Path:
        return self._data_dir / "scheduled-scan.log"

    # -- pure-ish helpers --------------------------------------------------
    @staticmethod
    def uv_path() -> str:
        """Absolute path to the `uv` binary (falls back to the bare name)."""
        return shutil.which("uv") or "uv"

    @staticmethod
    def repo_dir() -> Path:
        """The repository root the agent should run from."""
        # src/delium/scheduler.py -> src/delium -> src -> <root>
        return Path(__file__).resolve().parents[2]

    def render(self, times: tuple[tuple[int, int], ...]) -> str:
        return build_plist(
            label=self.label,
            uv_path=self.uv_path(),
            repo_dir=str(self.repo_dir()),
            times=times,
            log_path=str(self.log_path),
        )

    def installed_times(self) -> tuple[tuple[int, int], ...]:
        """Parse the times back out of an installed plist (empty if none)."""
        if not self.plist_path.exists():
            return ()
        with self.plist_path.open("rb") as fh:
            data = plistlib.load(fh)
        cal = data.get("StartCalendarInterval")
        entries = cal if isinstance(cal, list) else [cal] if isinstance(cal, dict) else []
        return tuple(sorted((int(e.get("Hour", 0)), int(e.get("Minute", 0))) for e in entries))

    def log_tail(self, lines: int = 20) -> str:
        if not self.log_path.exists():
            return ""
        text = self.log_path.read_text(errors="replace").splitlines()
        return "\n".join(text[-lines:])

    def status(self) -> ScheduleStatus:
        return ScheduleStatus(
            installed=self.plist_path.exists(),
            plist_path=str(self.plist_path),
            times=self.installed_times(),
            log_path=str(self.log_path),
            log_tail=self.log_tail(),
        )

    # -- launchd (macOS only) ---------------------------------------------
    @staticmethod
    def is_macos() -> bool:
        return sys.platform == "darwin"

    def write_plist(self, times: tuple[tuple[int, int], ...]) -> Path:
        """Write the generated plist to disk (creates the directory). Portable —
        does not touch launchctl, so tests can call it anywhere."""
        self.plist_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.plist_path.write_text(self.render(times))
        return self.plist_path

    def install(self, times: tuple[tuple[int, int], ...]) -> Path:
        """Generate + write the plist, then load it into launchd (macOS)."""
        path = self.write_plist(times)
        if self.is_macos():
            # Reload cleanly: remove any prior definition, then load the new one.
            self._launchctl("unload", str(path), check=False)
            self._launchctl("load", "-w", str(path), check=True)
        return path

    def uninstall(self) -> bool:
        """Unload from launchd (macOS) and delete the plist. Returns True if a
        plist was present."""
        existed = self.plist_path.exists()
        if self.is_macos() and existed:
            self._launchctl("unload", str(self.plist_path), check=False)
        if existed:
            self.plist_path.unlink()
        return existed

    def run_now(self) -> None:
        """Kick the agent immediately (macOS). Off macOS this is a no-op; the CLI
        runs the scan inline instead."""
        if self.is_macos():
            uid = _uid()
            self._launchctl("kickstart", "-k", f"gui/{uid}/{self.label}", check=True)

    @staticmethod
    def _launchctl(*args: str, check: bool) -> None:
        subprocess.run(["launchctl", *args], check=check, capture_output=True)  # noqa: S603,S607


def _uid() -> int:
    import os

    return os.getuid()


def notify(title: str, message: str) -> None:
    """Post a macOS notification banner (osascript). No-op off macOS or when
    osascript is unavailable, and never raises."""
    if sys.platform != "darwin" or shutil.which("osascript") is None:
        return
    import contextlib

    safe_title = title.replace('"', "'")
    safe_msg = message.replace('"', "'")
    script = f'display notification "{safe_msg}" with title "{safe_title}"'
    with contextlib.suppress(Exception):  # a notification must never break the scan
        subprocess.run(["osascript", "-e", script], check=False, capture_output=True)  # noqa: S603,S607
