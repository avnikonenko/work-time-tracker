# Work Time Tracker

Local, dependency-free activity tracker for Linux desktops.

It counts time as active when the system idle timer says keyboard or mouse
activity happened within the last 60 seconds. It stores daily and hourly totals
in SQLite, not keystrokes, mouse positions, window titles, or screenshots.

Hourly charts start from the time this version is running. Older records only
have daily totals because earlier versions did not store hourly buckets.

## Commands

```bash
python3 tools/activity_tracker/activity_tracker.py --serve
python3 tools/activity_tracker/activity_tracker.py --start
python3 tools/activity_tracker/activity_tracker.py --open
python3 tools/activity_tracker/activity_tracker.py --status
python3 tools/activity_tracker/activity_tracker.py --stop
python3 tools/activity_tracker/activity_tracker.py --install
python3 tools/activity_tracker/activity_tracker.py --uninstall
```

The dashboard is served at:

```text
http://127.0.0.1:8765/
```

## Files

- Data: `~/.local/share/activity-time-tracker/activity.sqlite3`
- Log: `~/.local/state/activity-time-tracker/tracker.log`
- Autostart file: `~/.config/autostart/activity-time-tracker.desktop`
- App launcher: `~/.local/share/applications/activity-time-tracker.desktop`

## Idle Backends

The tracker tries these input-idle backends, in order:

1. Direct Linux input events through stable `/dev/input/by-path` and `/dev/input/by-id` device links
2. `xprintidle`
3. X11 `libXss` through Python `ctypes`
4. GNOME Mutter D-Bus through `gdbus`
5. KDE screensaver D-Bus through `qdbus`

If none are available, the daemon logs a clear error and does not count time.

## Advanced Option

Direct Linux input tracking is optional. Without readable input devices, the
tracker falls back to desktop idle timers, which can be less accurate on some
systems. See [Linux input events setup](docs/linux-input-events.md) for setup,
permission risks, and alternatives.
