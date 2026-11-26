#!/usr/bin/python3
"""Session-aware GTK companion for the existing GORF Garmin Sync daemon.

This observer never reads the watch, uploads data, or holds Drive credentials.
It reads only the daemon's private status JSON and Linux USB identifiers.
"""
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys
import time

STATE_PATH = Path.home() / ".local/state/garmin-sync/state.json"
LOCK_PATH = Path.home() / ".local/state/garmin-sync/ui.lock"
SYSFS_ROOT = Path("/sys/bus/usb/devices")
ACTIVE_PHASES = {
    "detected", "inventory", "staging", "preview", "preview_source_check",
    "copy", "copy_source_check", "verification", "final_check", "fit_inspection",
}
PHASE_NAMES = {
    "detected": "Preparing backup",
    "inventory": "Reading watch inventory",
    "staging": "Reading watch files",
    "preview": "Comparing with Drive",
    "preview_source_check": "Checking watch before transfer",
    "copy": "Transferring files",
    "copy_source_check": "Checking watch after transfer",
    "verification": "Verifying Google Drive backup",
    "final_check": "Checking watch identity",
    "fit_inspection": "Inspecting FIT files",
}


def usb_presence(root=SYSFS_ROOT):
    """Read-only sysfs probe; 091e:0003 still counts as a connected Garmin."""
    try:
        devices = list(root.iterdir())
    except OSError:
        return "unavailable"
    transient = False
    for device in devices:
        try:
            vendor = (device / "idVendor").read_text().strip().lower()
            product = (device / "idProduct").read_text().strip().lower()
        except OSError:
            continue
        if vendor != "091e":
            continue
        if product == "51b5":
            return "fenix_mtp_interface"
        if product == "0003":
            transient = True
    return "garmin_transient" if transient else "absent"


def read_state(path=STATE_PATH):
    try:
        with path.open("r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def verified_this_connection(state, usb):
    """Never interpret a historic successful run or dry-run as Done."""
    progress = state.get("sync_progress") or {}
    details = state.get("last_success_details") or {}
    token = state.get("connection_token")
    if not (usb == "fenix_mtp_interface" and state.get("detected")
            and state.get("source") and state.get("mtp_mounted")
            and state.get("status") == "success"
            and token is not None and token == state.get("verified_connection_token")
            and progress.get("phase") == "complete" and progress.get("mode") == "copy"
            and details.get("mode") == "copy"
            and isinstance(details.get("verified"), int) and details["verified"] > 0
            and isinstance(progress.get("verified"), int) and progress["verified"] > 0):
        return False
    activity = details.get("verified_activity")
    if activity and activity.get("run_id") != progress.get("run_id"):
        return False
    return True


def progress_summary(progress):
    phase = progress.get("phase") or "detected"
    name = PHASE_NAMES.get(phase, "Preparing backup")
    counts = []
    for current, total, noun in (("transferred", "transfers_total", "files transferred"),
                                 ("checked", "checks_total", "files checked")):
        if isinstance(progress.get(current), int):
            done = progress[current]
            total_value = progress.get(total)
            counts.append((str(done) + ("/" + str(total_value) if
                           isinstance(total_value, int) and total_value > 0 else "")
                           + " " + noun))
    if not counts and isinstance(progress.get("total_files"), int):
        counts.append(str(progress["total_files"]) + " files selected")
    index, total = progress.get("phase_index"), progress.get("phase_count")
    prefix = (f"Step {index}/{total}: " if isinstance(index, int)
              and isinstance(total, int) and index > 0 and total > 0 else "")
    fraction = None
    if phase == "copy":
        current, maximum = progress.get("transferred"), progress.get("transfers_total")
    elif phase == "verification":
        current, maximum = progress.get("checked"), progress.get("checks_total")
    else:
        current = maximum = None
    if isinstance(current, int) and isinstance(maximum, int) and maximum > 0:
        fraction = max(0.0, min(1.0, current / maximum))
    return prefix + name, " | ".join(counts) if counts else "Working...", fraction


def presentation(state, usb):
    """Pure, testable projection of daemon state; no secret paths in UI."""
    progress = state.get("sync_progress") or {}
    phase = progress.get("phase")
    mounted = bool(state.get("detected") and state.get("mtp_mounted"))
    alert = state.get("active_alert") or {}
    if usb == "garmin_transient":
        return ("attention", "Garmin connected. Select MTP.",
                "On your watch: Settings → System → Advanced → USB Mode → MTP. "
                "Then reconnect if file transfer does not start.", None, False)
    if usb == "fenix_mtp_interface" and not mounted:
        return ("attention", "Watch detected. Waiting for files.",
                "MTP is available but the watch filesystem has not mounted. "
                "Unlock the watch if prompted, then reconnect the data cable or try another USB port.",
                None, False)
    if usb == "absent" or (usb == "unavailable" and not mounted):
        return ("waiting", "Waiting for Garmin watch",
                "Connect your watch with a USB data cable. The backup runs automatically.",
                None, False)
    if verified_this_connection(state, usb):
        verified = (state.get("last_success_details") or {}).get("verified", 0)
        copied = (state.get("last_success_details") or {}).get("copied", 0)
        return ("done", "Done. Garmin backup verified.",
                f"{verified} files verified in Google Drive; {copied} transferred this run. "
                "You may disconnect your watch.", 1.0, False)
    if state.get("status") in ("retry_wait", "failed", "blocked") or phase in ("retry_wait", "failed", "interrupted"):
        reason = (alert.get("message") or "The backup did not complete. "
                  "Check the cable and use Retry while the watch is mounted.")
        return ("attention", "Backup needs attention", reason, None, mounted)
    if mounted:
        heading, description, fraction = progress_summary(progress)
        if phase in ACTIVE_PHASES or state.get("status") in ("inventory", "copy", "verifying", "staging", "dry-run"):
            return ("syncing", heading, description, fraction, False)
        return ("connected", "Watch connected. Preparing sync.",
                "The daemon is preparing to copy and verify your Garmin files.", None, True)
    return ("waiting", "Waiting for Garmin watch", "Connect your watch to begin.", None, False)


def acquire_single_instance(path=LOCK_PATH):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        return None
    return fd


def launch():
    if not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        print("Garmin Sync UI requires a graphical session; daemon is unaffected.", file=sys.stderr)
        return 0
    try:
        import gi
        gi.require_version("Gtk", "3.0")
        from gi.repository import GLib, Gtk
    except (ImportError, ValueError) as exc:
        print("Garmin Sync UI needs PyGObject and GTK 3: " + str(exc), file=sys.stderr)
        return 1

    lock_fd = acquire_single_instance()
    if lock_fd is None:
        return 0

    class Companion(Gtk.Window):
        def __init__(self):
            super().__init__(title="Garmin Sync")
            self.set_default_size(480, 230)
            self.set_border_width(20)
            self.set_resizable(False)
            self.connect("delete-event", self.on_close)
            self.previous_present = False
            self.previous_kind = None
            self.dismissed = False
            self.last_snapshot = {}
            self.last_usb = "absent"
            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
            self.add(outer)
            self.heading = Gtk.Label()
            self.heading.set_xalign(0)
            self.heading.set_line_wrap(True)
            outer.pack_start(self.heading, False, False, 0)
            self.description = Gtk.Label()
            self.description.set_xalign(0)
            self.description.set_line_wrap(True)
            self.description.set_max_width_chars(62)
            outer.pack_start(self.description, True, True, 0)
            self.bar = Gtk.ProgressBar()
            self.bar.set_show_text(True)
            outer.pack_start(self.bar, False, False, 0)
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            outer.pack_start(actions, False, False, 0)
            self.retry = Gtk.Button(label="Retry sync")
            self.retry.connect("clicked", self.request_retry)
            actions.pack_start(self.retry, False, False, 0)
            close = Gtk.Button(label="Hide")
            close.connect("clicked", lambda *_: self.hide_window())
            actions.pack_end(close, False, False, 0)
            self.show_all()
            self.hide()
            self.tick()
            GLib.timeout_add(1000, self.tick)

        def on_close(self, *_):
            self.hide_window()
            return True

        def hide_window(self):
            self.dismissed = True
            self.hide()

        def request_retry(self, *_):
            if self.last_usb != "fenix_mtp_interface" or not self.last_snapshot.get("mtp_mounted"):
                return
            try:
                subprocess.Popen(
                    ["/usr/bin/systemctl", "--user", "kill", "--kill-who=main",
                     "--signal=SIGUSR1", "garmin-sync.service"],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.description.set_text("Requested a fresh sync from the existing daemon.")
            except OSError:
                self.description.set_text("Could not request sync. Check garmin-sync.service.")

        def tick(self):
            usb = usb_presence()
            state = read_state()
            self.last_usb, self.last_snapshot = usb, state
            kind, title, message, fraction, can_retry = presentation(state, usb)
            present = usb in ("garmin_transient", "fenix_mtp_interface")
            # A previously dismissed window reappears on each physical reconnect.
            if present and not self.previous_present:
                self.dismissed = False
                self.show_all()
                self.present()
            # Alert and verified Done always get their own visible transition.
            if present and kind in ("attention", "done") and kind != self.previous_kind:
                self.dismissed = False
                self.show_all()
                self.present()
            if not present and self.previous_present and kind == "waiting":
                self.hide()
            self.heading.set_text(title)
            self.description.set_text(message)
            self.retry.set_visible(can_retry)
            if kind == "syncing" and fraction is None:
                self.bar.pulse()
                self.bar.set_text("Syncing and verifying")
            else:
                self.bar.set_fraction(fraction if fraction is not None else 0)
                self.bar.set_text("Verified" if kind == "done" else
                                  (f"{round(fraction * 100)}% of current stage" if
                                   fraction is not None else kind.title()))
            if present and not self.dismissed and not self.get_visible():
                self.show_all()
                self.retry.set_visible(can_retry)
            self.previous_present, self.previous_kind = present, kind
            return True

    Companion()
    Gtk.main()
    os.close(lock_fd)
    return 0


if __name__ == "__main__":
    sys.exit(launch())
