#!/usr/bin/env python3
"""Local activity-time tracker with a dependency-free web dashboard."""

from __future__ import annotations

import argparse
import ctypes
import ctypes.util
import datetime as dt
import errno
import fcntl
import html
import json
import os
import re
import signal
import socket
import sqlite3
import struct
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any


APP_NAME = "activity-time-tracker"
DEFAULT_PORT = 8765
DEFAULT_HOST = "127.0.0.1"
DEFAULT_IDLE_THRESHOLD_SECONDS = 60
DEFAULT_SAMPLE_SECONDS = 5
MIN_STALE_SAMPLE_SECONDS = 60
DEFAULT_INPUT_DEVICES = (
    "/dev/input/by-path/platform-i8042-serio-0-event-kbd",
    "/dev/input/by-path/platform-AMDI0010:00-event-mouse",
    "/dev/input/by-id/usb-Logitech_Wireless_Receiver-event-mouse",
)


def xdg_data_dir() -> Path:
    return Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local/share")) / APP_NAME


def xdg_state_dir() -> Path:
    return Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local/state")) / APP_NAME


def db_path() -> Path:
    return xdg_data_dir() / "activity.sqlite3"


def log_path() -> Path:
    return xdg_state_dir() / "tracker.log"


def lock_path() -> Path:
    return xdg_state_dir() / "daemon.lock"


def now_local() -> dt.datetime:
    return dt.datetime.now().astimezone()


def log(message: str) -> None:
    xdg_state_dir().mkdir(parents=True, exist_ok=True)
    stamp = now_local().isoformat(timespec="seconds")
    with log_path().open("a", encoding="utf-8") as fh:
        fh.write(f"{stamp} {message}\n")


def format_seconds(seconds: int | float) -> str:
    seconds = max(0, int(seconds))
    hours, rem = divmod(seconds, 3600)
    minutes = rem // 60
    return f"{hours}h {minutes:02d}m"


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS daily_activity (
                    day TEXT PRIMARY KEY,
                    active_seconds INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS hourly_activity (
                    day TEXT NOT NULL,
                    hour INTEGER NOT NULL,
                    active_seconds INTEGER NOT NULL DEFAULT 0,
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (day, hour)
                )
                """
            )
            con.execute(
                """
                CREATE TABLE IF NOT EXISTS metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            con.commit()

    def add_active_seconds(self, stamp: dt.datetime, seconds: int) -> None:
        if seconds <= 0:
            return
        day = stamp.date().isoformat()
        hour = stamp.hour
        updated_at = now_local().isoformat(timespec="seconds")
        with self._lock, closing(self._connect()) as con:
            con.execute(
                """
                INSERT INTO daily_activity(day, active_seconds, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(day) DO UPDATE SET
                    active_seconds = active_seconds + excluded.active_seconds,
                    updated_at = excluded.updated_at
                """,
                (day, int(seconds), updated_at),
            )
            con.execute(
                """
                INSERT INTO hourly_activity(day, hour, active_seconds, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(day, hour) DO UPDATE SET
                    active_seconds = active_seconds + excluded.active_seconds,
                    updated_at = excluded.updated_at
                """,
                (day, hour, int(seconds), updated_at),
            )
            con.commit()

    def set_metadata(self, key: str, value: str) -> None:
        with self._lock, closing(self._connect()) as con:
            con.execute(
                """
                INSERT INTO metadata(key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, value),
            )
            con.commit()

    def get_metadata(self) -> dict[str, str]:
        with self._lock, closing(self._connect()) as con:
            rows = con.execute("SELECT key, value FROM metadata").fetchall()
        return {row["key"]: row["value"] for row in rows}

    def today_seconds(self) -> int:
        today = now_local().date().isoformat()
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                "SELECT active_seconds FROM daily_activity WHERE day = ?",
                (today,),
            ).fetchone()
        return int(row["active_seconds"]) if row else 0

    def week_summary(self) -> dict[str, Any]:
        today = now_local().date()
        week_start = today - dt.timedelta(days=today.weekday())
        week_end = week_start + dt.timedelta(days=6)
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                """
                SELECT COALESCE(SUM(active_seconds), 0) AS active_seconds
                FROM daily_activity
                WHERE day BETWEEN ? AND ?
                """,
                (week_start.isoformat(), week_end.isoformat()),
            ).fetchone()
        seconds = int(row["active_seconds"]) if row else 0
        return {
            "week_start": week_start.isoformat(),
            "week_end": week_end.isoformat(),
            "week_seconds": seconds,
            "week_label": format_seconds(seconds),
        }

    def last_activity_saved_at(self) -> str:
        with self._lock, closing(self._connect()) as con:
            row = con.execute(
                """
                SELECT updated_at
                FROM daily_activity
                ORDER BY updated_at DESC
                LIMIT 1
                """
            ).fetchone()
        return str(row["updated_at"]) if row else ""

    def daily_rows(self, days: int) -> list[dict[str, Any]]:
        end = now_local().date()
        start = end - dt.timedelta(days=max(1, days) - 1)
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                """
                SELECT day, active_seconds
                FROM daily_activity
                WHERE day BETWEEN ? AND ?
                ORDER BY day
                """,
                (start.isoformat(), end.isoformat()),
            ).fetchall()
        by_day = {row["day"]: int(row["active_seconds"]) for row in rows}
        output = []
        current = start
        while current <= end:
            day = current.isoformat()
            output.append(
                {
                    "day": day,
                    "active_seconds": by_day.get(day, 0),
                    "hours": round(by_day.get(day, 0) / 3600, 2),
                }
            )
            current += dt.timedelta(days=1)
        return output

    def hourly_rows(self, day: str | None = None) -> list[dict[str, Any]]:
        if day is None:
            day = now_local().date().isoformat()
        with self._lock, closing(self._connect()) as con:
            rows = con.execute(
                """
                SELECT hour, active_seconds
                FROM hourly_activity
                WHERE day = ?
                ORDER BY hour
                """,
                (day,),
            ).fetchall()
        by_hour = {int(row["hour"]): int(row["active_seconds"]) for row in rows}
        return [
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "active_seconds": by_hour.get(hour, 0),
                "hours": round(by_hour.get(hour, 0) / 3600, 2),
            }
            for hour in range(24)
        ]


class IdleDetector:
    name = "unknown"

    def get_idle_ms(self) -> int:
        raise NotImplementedError


class LinuxInputIdleDetector(IdleDetector):
    name = "linux-input-events"

    # struct input_event: timeval sec/usec, unsigned short type/code, signed int value.
    _event_struct = struct.Struct("llHHi")
    _activity_event_types = {1, 2, 3}
    _ignored_event_types = {0, 4}

    def __init__(self, devices: tuple[str, ...]) -> None:
        self.devices = devices
        self.fds: list[int] = []
        self.last_activity = time.monotonic()
        errors: list[str] = []
        for device in devices:
            try:
                fd = os.open(device, os.O_RDONLY | os.O_NONBLOCK)
                self.fds.append(fd)
            except OSError as exc:
                errors.append(f"{device}: {exc}")
        if not self.fds:
            raise RuntimeError("no readable input event devices; " + "; ".join(errors))
        self._drain_events(update_activity=False)

    def _drain_events(self, update_activity: bool = True) -> None:
        for fd in self.fds:
            while True:
                try:
                    data = os.read(fd, self._event_struct.size * 64)
                except BlockingIOError:
                    break
                except OSError:
                    break
                if not data:
                    break
                for offset in range(0, len(data) - self._event_struct.size + 1, self._event_struct.size):
                    event = self._event_struct.unpack_from(data, offset)
                    event_type = event[2]
                    event_code = event[3]
                    if update_activity and event_type in self._activity_event_types:
                        self.last_activity = time.monotonic()

    def get_idle_ms(self) -> int:
        self._drain_events()
        return int((time.monotonic() - self.last_activity) * 1000)


class XPrintIdleDetector(IdleDetector):
    name = "xprintidle"

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def get_idle_ms(self) -> int:
        proc = subprocess.run(
            [self.executable],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
        return int(proc.stdout.strip())


class XScreenSaverInfo(ctypes.Structure):
    _fields_ = [
        ("window", ctypes.c_ulong),
        ("state", ctypes.c_int),
        ("kind", ctypes.c_int),
        ("til_or_since", ctypes.c_ulong),
        ("idle", ctypes.c_ulong),
        ("event_mask", ctypes.c_ulong),
    ]


class XssIdleDetector(IdleDetector):
    name = "x11-libxss"

    def __init__(self) -> None:
        if not os.environ.get("DISPLAY"):
            raise RuntimeError("DISPLAY is not set")
        lib_x11_name = ctypes.util.find_library("X11")
        lib_xss_name = ctypes.util.find_library("Xss")
        if not lib_x11_name or not lib_xss_name:
            raise RuntimeError("libX11/libXss not available")
        self.x11 = ctypes.cdll.LoadLibrary(lib_x11_name)
        self.xss = ctypes.cdll.LoadLibrary(lib_xss_name)
        self.x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
        self.x11.XOpenDisplay.restype = ctypes.c_void_p
        self.x11.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
        self.x11.XDefaultRootWindow.restype = ctypes.c_ulong
        self.x11.XCloseDisplay.argtypes = [ctypes.c_void_p]
        self.xss.XScreenSaverAllocInfo.restype = ctypes.POINTER(XScreenSaverInfo)
        self.xss.XScreenSaverQueryInfo.argtypes = [
            ctypes.c_void_p,
            ctypes.c_ulong,
            ctypes.POINTER(XScreenSaverInfo),
        ]
        self.display = self.x11.XOpenDisplay(None)
        if not self.display:
            raise RuntimeError("could not open X11 display")
        self.root = self.x11.XDefaultRootWindow(self.display)
        self.info = self.xss.XScreenSaverAllocInfo()
        if not self.info:
            raise RuntimeError("could not allocate XScreenSaverInfo")

    def get_idle_ms(self) -> int:
        ok = self.xss.XScreenSaverQueryInfo(self.display, self.root, self.info)
        if ok == 0:
            raise RuntimeError("XScreenSaverQueryInfo failed")
        return int(self.info.contents.idle)


class GdbusIdleDetector(IdleDetector):
    name = "gnome-mutter-dbus"

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def get_idle_ms(self) -> int:
        proc = subprocess.run(
            [
                self.executable,
                "call",
                "--session",
                "--dest",
                "org.gnome.Mutter.IdleMonitor",
                "--object-path",
                "/org/gnome/Mutter/IdleMonitor/Core",
                "--method",
                "org.gnome.Mutter.IdleMonitor.GetIdletime",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
        match = re.search(r"\d+", proc.stdout)
        if not match:
            raise RuntimeError(f"could not parse gdbus idle output: {proc.stdout!r}")
        return int(match.group(0))


class KdeQdbusIdleDetector(IdleDetector):
    name = "kde-screensaver-dbus"

    def __init__(self, executable: str) -> None:
        self.executable = executable

    def get_idle_ms(self) -> int:
        proc = subprocess.run(
            [
                self.executable,
                "org.kde.screensaver",
                "/ScreenSaver",
                "org.freedesktop.ScreenSaver.GetSessionIdleTime",
            ],
            check=True,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=2,
        )
        return int(proc.stdout.strip()) * 1000


def command_path(name: str) -> str | None:
    for part in os.environ.get("PATH", "").split(os.pathsep):
        candidate = Path(part) / name
        if candidate.exists() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def pick_idle_detector() -> IdleDetector:
    candidates: list[tuple[str, Any]] = []
    input_devices = tuple(
        device
        for device in os.environ.get("ACTIVITY_TRACKER_INPUT_DEVICES", ",".join(DEFAULT_INPUT_DEVICES)).split(",")
        if device
    )
    candidates.append(("linux-input-events", lambda: LinuxInputIdleDetector(input_devices)))
    xprintidle = command_path("xprintidle")
    if xprintidle:
        candidates.append(("xprintidle", lambda: XPrintIdleDetector(xprintidle)))
    candidates.append(("x11-libxss", XssIdleDetector))
    gdbus = command_path("gdbus")
    if gdbus:
        candidates.append(("gnome-mutter-dbus", lambda: GdbusIdleDetector(gdbus)))
    qdbus = command_path("qdbus")
    if qdbus:
        candidates.append(("kde-screensaver-dbus", lambda: KdeQdbusIdleDetector(qdbus)))

    errors: list[str] = []
    for name, factory in candidates:
        try:
            detector = factory()
            detector.get_idle_ms()
            log(f"using idle detector: {detector.name}")
            return detector
        except Exception as exc:
            errors.append(f"{name}: {exc}")
    raise RuntimeError("no idle detector available; " + "; ".join(errors))


class ActivityTracker:
    def __init__(
        self,
        database: Database,
        idle_threshold_seconds: int,
        sample_seconds: int,
    ) -> None:
        self.database = database
        self.idle_threshold_seconds = idle_threshold_seconds
        self.sample_seconds = sample_seconds
        self.stop_event = threading.Event()
        self.detector: IdleDetector | None = None
        self.last_idle_ms: int | None = None
        self.last_error: str | None = None
        self.started_at = now_local()

    @property
    def backend_name(self) -> str:
        return self.detector.name if self.detector else "not-started"

    def run(self) -> None:
        self.database.set_metadata("pid", str(os.getpid()))
        self.database.set_metadata("started_at", self.started_at.isoformat(timespec="seconds"))
        self.database.set_metadata("sample_seconds", str(self.sample_seconds))
        self.database.set_metadata("idle_threshold_seconds", str(self.idle_threshold_seconds))

        try:
            self.detector = pick_idle_detector()
            self.database.set_metadata("idle_backend", self.detector.name)
            self.database.set_metadata("last_error", "")
        except Exception as exc:
            self.last_error = str(exc)
            self.database.set_metadata("idle_backend", "unavailable")
            self.database.set_metadata("last_error", self.last_error)
            log(f"tracker stopped: {self.last_error}")
            return

        previous = time.monotonic()
        while not self.stop_event.wait(self.sample_seconds):
            current = time.monotonic()
            elapsed = current - previous
            previous = current
            try:
                idle_ms = self.detector.get_idle_ms()
                self.last_idle_ms = idle_ms
                self.database.set_metadata("last_idle_ms", str(idle_ms))
                self.database.set_metadata("last_seen_at", now_local().isoformat(timespec="seconds"))
                self.database.set_metadata("last_error", "")
            except Exception as exc:
                self.last_error = str(exc)
                self.database.set_metadata("last_error", self.last_error)
                log(f"idle detector error: {self.last_error}")
                continue

            if elapsed > self.sample_seconds * 3:
                log(f"skipped large sample gap: {elapsed:.1f}s")
                continue
            if idle_ms <= self.idle_threshold_seconds * 1000:
                try:
                    self.database.add_active_seconds(now_local(), round(elapsed))
                except Exception as exc:
                    self.last_error = f"database write failed: {exc}"
                    self.database.set_metadata("last_error", self.last_error)
                    log(self.last_error)

    def stop(self) -> None:
        self.stop_event.set()

    def status(self) -> dict[str, Any]:
        metadata = self.database.get_metadata()
        today = self.database.today_seconds()
        week = self.database.week_summary()
        idle_ms = self.last_idle_ms
        if idle_ms is None and metadata.get("last_idle_ms", "").isdigit():
            idle_ms = int(metadata["last_idle_ms"])
        status = {
            "pid": os.getpid(),
            "running": not self.stop_event.is_set(),
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "idle_backend": self.backend_name,
            "last_idle_seconds": None if idle_ms is None else round(idle_ms / 1000, 1),
            "last_error": self.last_error or metadata.get("last_error", ""),
            "last_sample_seen_at": metadata.get("last_seen_at", ""),
            "today_seconds": today,
            "today_label": format_seconds(today),
            "last_activity_saved_at": self.database.last_activity_saved_at(),
            "sample_seconds": self.sample_seconds,
            "idle_threshold_seconds": self.idle_threshold_seconds,
            "database": str(self.database.path),
            "log": str(log_path()),
        }
        status.update(week)
        return status


class DashboardHandler(BaseHTTPRequestHandler):
    tracker: ActivityTracker
    database: Database
    shutdown_callback: Any
    restart_callback: Any

    def log_message(self, format: str, *args: Any) -> None:
        try:
            status = int(args[1])
        except (IndexError, TypeError, ValueError):
            status = 0
        if status >= 400:
            log("http " + (format % args))

    def _send(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload: dict[str, Any], status: int = 200) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(payload).encode("utf-8"))

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/":
            self._send(200, "text/html; charset=utf-8", DASHBOARD_HTML.encode("utf-8"))
            return
        if path == "/api/status":
            self._json(self.tracker.status())
            return
        if path == "/api/summary":
            days = 60
            match = re.search(r"[?&]days=(\d+)", self.path)
            if match:
                days = max(7, min(370, int(match.group(1))))
            self._json(
                {
                    "status": self.tracker.status(),
                    "days": self.database.daily_rows(days),
                    "hours": self.database.hourly_rows(),
                }
            )
            return
        self._json({"error": "not found"}, 404)

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/stop":
            self._json({"ok": True, "message": "tracker stopping"})
            threading.Thread(target=self.shutdown_callback, daemon=True).start()
            return
        if path == "/api/restart":
            self._json({"ok": True, "message": "tracker restarting"})
            threading.Thread(target=self.restart_callback, daemon=True).start()
            return
        self._json({"error": "not found"}, 404)


DASHBOARD_HTML = r"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Work Time Tracker</title>
  <style>
    :root {
      --bg: #f6f7f4;
      --panel: #ffffff;
      --text: #20252a;
      --muted: #6a7178;
      --line: #d8ddd6;
      --blue: #2f6fed;
      --green: #16865c;
      --amber: #c47713;
      --red: #bb3e3e;
      --bar: #4f7f9f;
      --bar2: #f2a65a;
    }
    * { box-sizing: border-box; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      color: var(--text);
      background: var(--bg);
    }
    header {
      border-bottom: 1px solid var(--line);
      background: var(--panel);
    }
    .wrap {
      max-width: 1120px;
      margin: 0 auto;
      padding: 20px;
    }
    .top {
      display: flex;
      align-items: center;
      justify-content: space-between;
      gap: 16px;
      flex-wrap: wrap;
    }
    h1 {
      margin: 0;
      font-size: 24px;
      line-height: 1.2;
      font-weight: 700;
      letter-spacing: 0;
    }
    .today {
      font-size: clamp(42px, 8vw, 72px);
      line-height: 1;
      font-weight: 750;
      letter-spacing: 0;
      margin-top: 14px;
    }
    .muted { color: var(--muted); }
    button {
      border: 1px solid var(--line);
      background: var(--panel);
      color: var(--text);
      border-radius: 6px;
      padding: 9px 12px;
      font: inherit;
      cursor: pointer;
    }
    button:hover { border-color: #aeb6ad; }
    .danger { color: var(--red); }
    .grid {
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(300px, .65fr);
      gap: 18px;
      margin-top: 18px;
    }
    section {
      background: var(--panel);
      border: 1px solid var(--line);
      border-radius: 8px;
      padding: 16px;
      min-width: 0;
    }
    h2 {
      margin: 0 0 12px;
      font-size: 16px;
      letter-spacing: 0;
    }
    .bars {
      display: grid;
      grid-template-columns: repeat(30, minmax(8px, 1fr));
      align-items: end;
      gap: 5px;
      height: 260px;
      border-bottom: 1px solid var(--line);
      padding-top: 8px;
    }
    .bar {
      min-height: 2px;
      background: linear-gradient(180deg, var(--bar), var(--bar2));
      border-radius: 4px 4px 0 0;
      position: relative;
    }
    .hour-bars {
      grid-template-columns: repeat(24, minmax(8px, 1fr));
      height: 220px;
    }
    .bar:hover::after {
      content: attr(data-label);
      position: absolute;
      z-index: 2;
      bottom: calc(100% + 8px);
      left: 50%;
      transform: translateX(-50%);
      background: #20252a;
      color: #fff;
      border-radius: 4px;
      padding: 5px 7px;
      white-space: nowrap;
      font-size: 12px;
    }
    .calendar {
      display: grid;
      grid-template-columns: repeat(7, minmax(0, 1fr));
      gap: 6px;
    }
    .dow {
      color: var(--muted);
      font-size: 12px;
      text-align: center;
      padding-bottom: 3px;
    }
    .day {
      min-height: 62px;
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 6px;
      background: #f9faf8;
      display: flex;
      flex-direction: column;
      justify-content: space-between;
      overflow: hidden;
    }
    .day.empty { opacity: .35; }
    .day-num { font-size: 13px; font-weight: 650; }
    .day-hours { font-size: 12px; color: #000; }
    .stats {
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 10px;
    }
    .stat {
      border: 1px solid var(--line);
      border-radius: 6px;
      padding: 10px;
      min-width: 0;
    }
    .stat b {
      display: block;
      font-size: 12px;
      color: var(--muted);
      font-weight: 600;
      margin-bottom: 4px;
    }
    .stat span {
      overflow-wrap: anywhere;
    }
    .error {
      margin-top: 12px;
      color: var(--red);
      overflow-wrap: anywhere;
    }
    .accuracy-warning {
      margin: 0 0 18px;
      border: 2px solid var(--red);
      border-radius: 6px;
      background: #fff1f0;
      color: #861d1d;
      padding: 12px 14px;
      font-weight: 700;
      overflow-wrap: anywhere;
    }
    .accuracy-warning.hidden { display: none; }
    @media (max-width: 800px) {
      .grid { grid-template-columns: 1fr; }
      .wrap { padding: 14px; }
      .bars { height: 220px; gap: 3px; }
      .stats { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <div class="wrap">
      <div class="top">
        <div>
          <h1>Work Time Tracker</h1>
          <div class="muted" id="subtitle">Loading...</div>
        </div>
        <div>
          <button id="refresh">Refresh</button>
          <button id="restart">Restart</button>
          <button id="stop" class="danger">Stop tracker</button>
        </div>
      </div>
      <div class="today" id="today">0h 00m</div>
    </div>
  </header>
  <main class="wrap">
    <div class="accuracy-warning hidden" id="accuracy-warning"></div>
    <div class="grid">
      <section>
        <h2>Last 30 Days</h2>
        <div class="bars" id="bars"></div>
      </section>
      <section>
        <h2>Today By Hour</h2>
        <div class="bars hour-bars" id="hour-bars"></div>
      </section>
    </div>
    <section style="margin-top:18px">
      <h2>Status</h2>
      <div class="stats" id="stats"></div>
      <div class="error" id="error"></div>
    </section>
    <section style="margin-top:18px">
      <h2 id="month-title">This Month</h2>
      <div class="calendar" id="calendar"></div>
    </section>
  </main>
  <script>
    const bars = document.getElementById("bars");
    const hourBars = document.getElementById("hour-bars");
    const calendar = document.getElementById("calendar");
    const today = document.getElementById("today");
    const subtitle = document.getElementById("subtitle");
    const stats = document.getElementById("stats");
    const error = document.getElementById("error");
    const monthTitle = document.getElementById("month-title");
    const accuracyWarning = document.getElementById("accuracy-warning");

    function hoursLabel(seconds) {
      const hours = Math.floor(seconds / 3600);
      const minutes = Math.floor((seconds % 3600) / 60);
      return `${hours}h ${String(minutes).padStart(2, "0")}m`;
    }

    function dayLabel(isoDay) {
      const [year, month, day] = isoDay.split("-").map(Number);
      return new Date(year, month - 1, day).toLocaleDateString(undefined, { month: "short", day: "numeric" });
    }

    function dateTimeLabel(value) {
      if (!value) return "never";
      const date = new Date(value);
      if (Number.isNaN(date.getTime())) return value;
      return date.toLocaleString(undefined, {
        year: "numeric",
        month: "short",
        day: "numeric",
        hour: "2-digit",
        minute: "2-digit",
        second: "2-digit"
      });
    }

    function renderBars(days) {
      const recent = days.slice(-30);
      const maxSeconds = Math.max(3600, ...recent.map(d => d.active_seconds));
      bars.innerHTML = "";
      for (const day of recent) {
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.style.height = `${Math.max(2, day.active_seconds / maxSeconds * 100)}%`;
        bar.dataset.label = `${dayLabel(day.day)}: ${hoursLabel(day.active_seconds)}`;
        bars.appendChild(bar);
      }
    }

    function renderHourly(hours) {
      const maxSeconds = Math.max(900, ...hours.map(h => h.active_seconds));
      hourBars.innerHTML = "";
      for (const hour of hours) {
        const bar = document.createElement("div");
        bar.className = "bar";
        bar.style.height = `${Math.max(2, hour.active_seconds / maxSeconds * 100)}%`;
        bar.dataset.label = `${hour.label}: ${hoursLabel(hour.active_seconds)}`;
        hourBars.appendChild(bar);
      }
    }

    function heatColor(seconds) {
      const hours = seconds / 3600;
      if (hours <= 0) return "#f9faf8";
      if (hours < 2) return "#e1f0d9";
      if (hours < 5) return "#b8dfb7";
      if (hours < 8) return "#77bf9b";
      return "#459284";
    }

    function renderCalendar(days) {
      const now = new Date();
      const year = now.getFullYear();
      const month = now.getMonth();
      monthTitle.textContent = now.toLocaleDateString(undefined, { month: "long", year: "numeric" });
      const byDay = new Map(days.map(d => [d.day, d.active_seconds]));
      const first = new Date(year, month, 1);
      const last = new Date(year, month + 1, 0);
      const firstWeekday = (first.getDay() + 6) % 7;
      calendar.innerHTML = "";
      for (const label of ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]) {
        const div = document.createElement("div");
        div.className = "dow";
        div.textContent = label;
        calendar.appendChild(div);
      }
      for (let i = 0; i < firstWeekday; i++) {
        const div = document.createElement("div");
        div.className = "day empty";
        calendar.appendChild(div);
      }
      for (let day = 1; day <= last.getDate(); day++) {
        const iso = `${year}-${String(month + 1).padStart(2, "0")}-${String(day).padStart(2, "0")}`;
        const seconds = byDay.get(iso) || 0;
        const div = document.createElement("div");
        div.className = "day";
        div.style.background = heatColor(seconds);
        div.innerHTML = `<div class="day-num">${day}</div><div class="day-hours">${hoursLabel(seconds)}</div>`;
        calendar.appendChild(div);
      }
    }

    function renderStats(status) {
      const items = [
        ["Backend", status.idle_backend],
        ["Idle now", status.last_idle_seconds == null ? "unknown" : `${status.last_idle_seconds}s`],
        ["This week", status.week_label],
        ["Last sample check", dateTimeLabel(status.last_sample_seen_at)],
        ["Last saved activity", dateTimeLabel(status.last_activity_saved_at)],
        ["Threshold", `${status.idle_threshold_seconds}s`],
        ["Database", status.database],
        ["Log", status.log],
        ["PID", String(status.pid)]
      ];
      stats.innerHTML = "";
      for (const [label, value] of items) {
        const div = document.createElement("div");
        div.className = "stat";
        div.innerHTML = `<b>${label}</b><span>${value || ""}</span>`;
        stats.appendChild(div);
      }
      error.textContent = status.last_error || "";
    }

    function renderAccuracyWarning(status) {
      if (status.idle_backend === "linux-input-events") {
        accuracyWarning.classList.add("hidden");
        accuracyWarning.textContent = "";
        return;
      }
      accuracyWarning.classList.remove("hidden");
      accuracyWarning.textContent = `Warning: tracking may be inaccurate. Current backend is "${status.idle_backend || "unknown"}", not "linux-input-events", so work time can be overestimated.`;
    }

    async function refresh() {
      const response = await fetch("/api/summary?days=60", { cache: "no-store" });
      const data = await response.json();
      today.textContent = data.status.today_label;
      subtitle.textContent = `Counts time while keyboard or mouse activity happened in the last ${data.status.idle_threshold_seconds}s.`;
      renderAccuracyWarning(data.status);
      renderBars(data.days);
      renderHourly(data.hours);
      renderCalendar(data.days);
      renderStats(data.status);
    }

    document.getElementById("refresh").addEventListener("click", refresh);
    document.getElementById("restart").addEventListener("click", async () => {
      await fetch("/api/restart", { method: "POST" });
      subtitle.textContent = "Tracker is restarting.";
      setTimeout(refresh, 5000);
    });
    document.getElementById("stop").addEventListener("click", async () => {
      await fetch("/api/stop", { method: "POST" });
      subtitle.textContent = "Tracker is stopping.";
    });
    let refreshTimer = null;
    function scheduleRefresh(delay = 30000) {
      if (refreshTimer !== null) clearTimeout(refreshTimer);
      refreshTimer = setTimeout(async () => {
        if (!document.hidden) await refresh();
        scheduleRefresh(document.hidden ? 120000 : 30000);
      }, delay);
    }
    document.addEventListener("visibilitychange", () => {
      if (!document.hidden) refresh();
      scheduleRefresh(document.hidden ? 120000 : 30000);
    });
    refresh();
    scheduleRefresh();
  </script>
</body>
</html>
"""


def acquire_lock() -> Any:
    xdg_state_dir().mkdir(parents=True, exist_ok=True)
    lock_fh = lock_path().open("w", encoding="utf-8")
    try:
        fcntl.lockf(lock_fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        if exc.errno in (errno.EACCES, errno.EAGAIN):
            raise RuntimeError("activity tracker is already running")
        raise
    lock_fh.write(str(os.getpid()))
    lock_fh.truncate()
    lock_fh.flush()
    return lock_fh


def make_handler(
    tracker: ActivityTracker,
    database: Database,
    server: ThreadingHTTPServer,
    args: argparse.Namespace,
) -> type[DashboardHandler]:
    class BoundDashboardHandler(DashboardHandler):
        pass

    def stop_server() -> None:
        BoundDashboardHandler.tracker.stop()
        server.shutdown()

    def restart_server() -> None:
        subprocess.Popen(
            [
                sys.executable,
                str(Path(__file__).resolve()),
                "--restart-helper",
                "--host",
                args.host,
                "--port",
                str(args.port),
                "--idle-threshold",
                str(args.idle_threshold),
                "--sample-seconds",
                str(args.sample_seconds),
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        stop_server()

    BoundDashboardHandler.tracker = tracker
    BoundDashboardHandler.database = database
    BoundDashboardHandler.shutdown_callback = staticmethod(stop_server)
    BoundDashboardHandler.restart_callback = staticmethod(restart_server)
    return BoundDashboardHandler


def serve(args: argparse.Namespace) -> int:
    try:
        lock_fh = acquire_lock()
    except RuntimeError as exc:
        print(exc, file=sys.stderr)
        return 2

    database = Database(db_path())
    tracker_state: dict[str, Any] = {}
    tracker_state_lock = threading.Lock()
    shutdown_event = threading.Event()

    def start_tracker() -> None:
        tracker = ActivityTracker(
            database=database,
            idle_threshold_seconds=args.idle_threshold,
            sample_seconds=args.sample_seconds,
        )
        tracker_thread = threading.Thread(target=tracker.run, name="activity-tracker", daemon=True)
        with tracker_state_lock:
            tracker_state["tracker"] = tracker
            tracker_state["thread"] = tracker_thread
        tracker_thread.start()

    start_tracker()

    server = ThreadingHTTPServer((args.host, args.port), lambda *a, **kw: None)
    handler = make_handler(tracker_state["tracker"], database, server, args)
    server.RequestHandlerClass = handler
    database.set_metadata("dashboard_url", f"http://{args.host}:{args.port}/")
    log(f"dashboard serving on http://{args.host}:{args.port}/")
    print(f"Serving activity dashboard at http://{args.host}:{args.port}/")

    def watch_tracker() -> None:
        while not shutdown_event.wait(max(15, args.sample_seconds * 3)):
            with tracker_state_lock:
                tracker = tracker_state["tracker"]
                tracker_thread = tracker_state["thread"]
            if tracker_thread.is_alive():
                continue
            log("tracker thread stopped unexpectedly; restarting sampler")
            tracker.stop()
            start_tracker()
            with tracker_state_lock:
                handler.tracker = tracker_state["tracker"]

    watchdog_thread = threading.Thread(target=watch_tracker, name="activity-watchdog", daemon=True)
    watchdog_thread.start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        shutdown_event.set()
        with tracker_state_lock:
            tracker = tracker_state["tracker"]
            tracker_thread = tracker_state["thread"]
        tracker.stop()
        tracker_thread.join(timeout=args.sample_seconds + 2)
        watchdog_thread.join(timeout=1)
        lock_fh.close()
        log("tracker stopped")
    return 0


def url(host: str, port: int) -> str:
    return f"http://{host}:{port}/"


def server_is_running(host: str, port: int) -> bool:
    try:
        with urllib.request.urlopen(url(host, port) + "api/status", timeout=1) as response:
            return response.status == 200
    except (OSError, urllib.error.URLError):
        return False


def server_status(host: str, port: int) -> dict[str, Any] | None:
    try:
        with urllib.request.urlopen(url(host, port) + "api/status", timeout=1) as response:
            if response.status != 200:
                return None
            return json.loads(response.read().decode("utf-8"))
    except (OSError, json.JSONDecodeError, urllib.error.URLError):
        return None


def parse_local_datetime(value: str) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = dt.datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.astimezone()
    return parsed


def sampler_is_stale(status: dict[str, Any], sample_seconds: int) -> bool:
    last_seen = parse_local_datetime(str(status.get("last_sample_seen_at", "")))
    if last_seen is None:
        return True
    stale_after = max(MIN_STALE_SAMPLE_SECONDS, sample_seconds * 6)
    age_seconds = (now_local() - last_seen).total_seconds()
    return age_seconds > stale_after


def pid_matches_tracker(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().decode("utf-8", "replace")
    except OSError:
        return False
    return str(Path(__file__).resolve()) in cmdline and "--serve" in cmdline


def terminate_recorded_tracker(database: Database, quiet: bool = False) -> bool:
    metadata = database.get_metadata()
    pid_text = metadata.get("pid", "")
    if not pid_text.isdigit():
        if not quiet:
            print("no recorded tracker pid", file=sys.stderr)
        return False
    pid = int(pid_text)
    if not pid_matches_tracker(pid):
        if not quiet:
            print(f"recorded pid is not this tracker: {pid}", file=sys.stderr)
        return False
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return True
    except OSError as exc:
        if not quiet:
            print(f"could not stop tracker pid {pid}: {exc}", file=sys.stderr)
        return False
    for _ in range(25):
        if not pid_matches_tracker(pid):
            return True
        time.sleep(0.2)
    if not quiet:
        print(f"tracker pid {pid} did not exit after SIGTERM", file=sys.stderr)
    return False


def launch_server(args: argparse.Namespace) -> None:
    subprocess.Popen(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--serve",
            "--host",
            args.host,
            "--port",
            str(args.port),
            "--idle-threshold",
            str(args.idle_threshold),
            "--sample-seconds",
            str(args.sample_seconds),
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )


def ensure_server_running(args: argparse.Namespace) -> bool:
    status = server_status(args.host, args.port)
    if status is not None and not sampler_is_stale(status, args.sample_seconds):
        return True
    if status is not None:
        log("sampler stale; restarting tracker process")
    if status is None or sampler_is_stale(status, args.sample_seconds):
        terminate_recorded_tracker(Database(db_path()), quiet=True)
        launch_server(args)
        for _ in range(40):
            status = server_status(args.host, args.port)
            if status is not None and not sampler_is_stale(status, args.sample_seconds):
                return True
            time.sleep(0.25)
    status = server_status(args.host, args.port)
    return status is not None and not sampler_is_stale(status, args.sample_seconds)


def start_server(args: argparse.Namespace) -> int:
    if ensure_server_running(args):
        print(url(args.host, args.port))
        return 0
    print("tracker did not start within 5 seconds", file=sys.stderr)
    return 1


def restart_after_delay(args: argparse.Namespace) -> int:
    time.sleep(2)
    for _ in range(20):
        if ensure_server_running(args):
            return 0
        time.sleep(0.5)
    return 1


def open_url(address: str) -> None:
    xdg_open = command_path("xdg-open")
    if xdg_open:
        subprocess.Popen(
            [xdg_open, address],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        return
    webbrowser.open(address)


def open_dashboard(args: argparse.Namespace) -> int:
    address = url(args.host, args.port)
    if not ensure_server_running(args):
        print("tracker did not start within 5 seconds", file=sys.stderr)
        return 1
    open_url(address)
    print(address)
    return 0


def stop_server(args: argparse.Namespace) -> int:
    try:
        req = urllib.request.Request(url(args.host, args.port) + "api/stop", method="POST")
        with urllib.request.urlopen(req, timeout=2) as response:
            print(response.read().decode("utf-8"))
        return 0
    except Exception as exc:
        print(f"dashboard stop failed: {exc}", file=sys.stderr)
    if terminate_recorded_tracker(Database(db_path())):
        print("stopped recorded tracker process")
        return 0
    return 1


def install_desktop_files(args: argparse.Namespace) -> int:
    script = Path(__file__).resolve()
    python = Path(sys.executable).resolve()
    autostart_dir = Path.home() / ".config/autostart"
    apps_dir = Path.home() / ".local/share/applications"
    autostart_dir.mkdir(parents=True, exist_ok=True)
    apps_dir.mkdir(parents=True, exist_ok=True)

    autostart = autostart_dir / "activity-time-tracker.desktop"
    launcher = apps_dir / "activity-time-tracker.desktop"
    common = [
        "Type=Application",
        "Name=Work Time Tracker",
        "Comment=Track active keyboard and mouse time locally",
        "Terminal=false",
        "Categories=Utility;",
    ]
    autostart.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                *common,
                (
                    f"Exec={python} {script} --serve --host {args.host} --port {args.port} "
                    f"--idle-threshold {args.idle_threshold} --sample-seconds {args.sample_seconds}"
                ),
                "X-GNOME-Autostart-enabled=true",
                "",
            ]
        ),
        encoding="utf-8",
    )
    launcher.write_text(
        "\n".join(
            [
                "[Desktop Entry]",
                *common,
                (
                    f"Exec={python} {script} --open --host {args.host} --port {args.port} "
                    f"--idle-threshold {args.idle_threshold} --sample-seconds {args.sample_seconds}"
                ),
                "",
            ]
        ),
        encoding="utf-8",
    )
    autostart.chmod(0o644)
    launcher.chmod(0o644)
    print(f"Installed autostart: {autostart}")
    print(f"Installed launcher: {launcher}")
    return 0


def uninstall_desktop_files() -> int:
    paths = [
        Path.home() / ".config/autostart/activity-time-tracker.desktop",
        Path.home() / ".local/share/applications/activity-time-tracker.desktop",
    ]
    for path in paths:
        try:
            path.unlink()
            print(f"Removed {path}")
        except FileNotFoundError:
            print(f"Already absent: {path}")
    return 0


def show_status(args: argparse.Namespace) -> int:
    if server_is_running(args.host, args.port):
        with urllib.request.urlopen(url(args.host, args.port) + "api/status", timeout=2) as response:
            payload = json.loads(response.read().decode("utf-8"))
        print(json.dumps(payload, indent=2, sort_keys=True))
        return 0
    database = Database(db_path())
    metadata = database.get_metadata()
    today = database.today_seconds()
    week = database.week_summary()
    print(
        json.dumps(
            {
                "running": False,
                "today_seconds": today,
                "today_label": format_seconds(today),
                **week,
                "metadata": metadata,
                "database": str(db_path()),
                "log": str(log_path()),
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument("--serve", action="store_true", help="run tracker daemon and dashboard server")
    action.add_argument("--start", action="store_true", help="start tracker daemon if it is not already running")
    action.add_argument("--open", action="store_true", help="open dashboard, starting the tracker if needed")
    action.add_argument("--stop", action="store_true", help="stop the running tracker")
    action.add_argument("--install", action="store_true", help="install autostart and application launcher")
    action.add_argument("--uninstall", action="store_true", help="remove autostart and application launcher")
    action.add_argument("--status", action="store_true", help="print tracker status")
    action.add_argument("--restart-helper", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--host", default=DEFAULT_HOST)
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    parser.add_argument("--idle-threshold", type=int, default=DEFAULT_IDLE_THRESHOLD_SECONDS)
    parser.add_argument("--sample-seconds", type=int, default=DEFAULT_SAMPLE_SECONDS)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.idle_threshold < 5:
        parser.error("--idle-threshold must be at least 5 seconds")
    if args.sample_seconds < 1:
        parser.error("--sample-seconds must be at least 1 second")
    if args.install:
        return install_desktop_files(args)
    if args.uninstall:
        return uninstall_desktop_files()
    if args.start:
        return start_server(args)
    if args.restart_helper:
        return restart_after_delay(args)
    if args.open:
        return open_dashboard(args)
    if args.stop:
        return stop_server(args)
    if args.status:
        return show_status(args)
    return serve(args)


if __name__ == "__main__":
    raise SystemExit(main())
