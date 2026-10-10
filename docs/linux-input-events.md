# Advanced Option: More Accurate Linux Input Events

[Back to README](../README.md)

This setup is optional. Without readable input devices, the tracker uses desktop
idle timers: `xprintidle`/`libXss` on X11, GNOME Mutter D-Bus, or KDE screensaver
D-Bus, depending on what is available. These require no extra device permissions,
but can be less reliable on some desktop/session combinations: unavailable or
incorrect idle readings can cause missing activity or overcounting.

Direct input tracking avoids relying on those timers.
Granting raw keyboard access has the security tradeoff described below.

Other approaches are possible: a working desktop idle monitor can provide reliable
activity detection without raw keyboard access (see [Mutter IdleMonitor](https://mutter.gnome.org/meta/class.IdleMonitor.html)).
A separate privileged helper could read devices and expose only an activity
timestamp, reducing access granted to ordinary apps, but requires additional
implementation and security review. This tool does not implement that helper.

Assumptions: you run the tracker as your desktop user and want to grant read
access only to your selected keyboard, touchpad, and mouse. The paths below are
for this notebook; discover your own paths before using them on another machine.

1. Find stable device links and inspect their properties:

   ```bash
   ls -l /dev/input/by-id /dev/input/by-path
   udevadm info -q property -n /dev/input/by-path/platform-i8042-serio-0-event-kbd
   udevadm info -q property -n /dev/input/by-path/platform-AMDI0010:00-event-mouse
   ```

   Look for `ID_PATH` and `ID_INPUT_KEYBOARD`, `ID_INPUT_TOUCHPAD`, or
   `ID_INPUT_MOUSE`. Inspect your external mouse link too, if connected.
   Avoid hardcoding `event4` or similar numbers: they can change after reboot.

2. Create a dedicated group (skip `groupadd` if it already exists) and add your
   desktop user:

   ```bash
   getent group activitytrack || sudo groupadd --system activitytrack
   sudo usermod -aG activitytrack "$USER"
   ```

3. Open the rules file:

   ```bash
   sudo nano /etc/udev/rules.d/90-activitytrack-input.rules
   ```

   Add these rules, adjusting `ID_PATH` to the properties found in step 1:

   ```udev
   SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_KEYBOARD}=="1", ENV{ID_PATH}=="platform-i8042-serio-0", GROUP="activitytrack", MODE="0640"
   SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_TOUCHPAD}=="1", ENV{ID_PATH}=="platform-AMDI0010:00", GROUP="activitytrack", MODE="0640"
   SUBSYSTEM=="input", KERNEL=="event*", ENV{ID_INPUT_MOUSE}=="1", ENV{ID_PATH}=="pci-0000:04:00.3-usb-0:2:1.0", GROUP="activitytrack", MODE="0640"
   ```

4. Reload the rules, then reboot. Rebooting applies both device permissions and
   the new group membership to the automatically started tracker:

   ```bash
   sudo udevadm control --reload-rules
   ```

5. After reboot, verify permissions and the active backend:

   ```bash
   groups
   ls -lL /dev/input/by-path/platform-i8042-serio-0-event-kbd /dev/input/by-path/platform-AMDI0010:00-event-mouse
   check-work-time --status
   ```

   Your groups should include `activitytrack`; selected devices should have
   group `activitytrack` and group read permission. Status should show
   `idle_backend: linux-input-events`. The dashboard warning disappears when
   this backend is active. Verify every device you use: the backend can start
   with only one readable device, leaving activity on other devices untracked.

The default device links are:

```text
/dev/input/by-path/platform-i8042-serio-0-event-kbd
/dev/input/by-path/platform-AMDI0010:00-event-mouse
/dev/input/by-id/usb-Logitech_Wireless_Receiver-event-mouse
```

To override them for a manual foreground run, execute this from the tracker
directory (stop the existing tracker first with `check-work-time --stop`):

```bash
ACTIVITY_TRACKER_INPUT_DEVICES=/dev/input/by-path/YOUR-KEYBOARD-event-kbd,/dev/input/by-path/YOUR-TOUCHPAD-event-mouse python3 activity_tracker.py --serve
```

This environment override applies only to that run, not automatically to the
autostart launcher. On another machine, configure the same variable in the
autostart launch command as well.

Security tradeoff: a dedicated group limits access to the selected devices,
but every process running as your user can read those raw input events. The
tracker stores totals only, not keystrokes or coordinates. You do not need to
run the tracker as root or join the broader `input` group.

