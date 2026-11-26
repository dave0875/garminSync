#!/usr/bin/python3
"""Session-aware GTK companion for the existing GORF Garmin Sync daemon.

This observer never reads the watch, uploads data, or holds Drive credentials.
It reads only the daemon's private status JSON and Linux USB identifiers.
"""
import datetime as dt
import fcntl
import math
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
    "detected", "inventory", "staging", "staging_source_check", "preview", "preview_source_check",
    "copy", "copy_source_check", "verification", "final_check", "fit_inspection",
}
PHASE_NAMES = {
    "detected": "Preparing backup",
    "inventory": "Reading watch inventory",
    "staging": "Capturing watch data",
    "staging_source_check": "Checking captured files",
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
    proposed = phase == "preview" or progress.get("mode") == "dry-run"
    transfer_noun = ("proposed changes" if proposed else
                     "files captured" if phase == "staging" else "files transferred")
    for current, total, noun in (("transferred", "transfers_total", transfer_noun),
                                 ("checked", "checks_total", "files checked")):
        if isinstance(progress.get(current), int):
            done = progress[current]
            total_value = progress.get(total)
            counts.append((str(done) + ("/" + str(total_value) if
                           isinstance(total_value, int) and total_value > 0 else "")
                           + " " + noun))
    if isinstance(progress.get("bytes"), int) and progress["bytes"] >= 0:
        amount = f"{progress['bytes'] / 1048576:.1f} MiB"
        total_bytes = progress.get("bytes_total")
        if isinstance(total_bytes, int) and total_bytes > 0:
            amount += f"/{total_bytes / 1048576:.1f} MiB"
        counts.append(amount)
    if not counts and isinstance(progress.get("total_files"), int):
        counts.append(str(progress["total_files"]) + " files selected")
    index, total = progress.get("phase_index"), progress.get("phase_count")
    prefix = (f"Step {index}/{total}: " if isinstance(index, int)
              and isinstance(total, int) and index > 0 and total > 0 else "")
    fraction = None
    # rclone dynamically discovers transfers and checks. Neither changing
    # denominator is proof of whole-stage completion.
    if phase == "staging":
        current, maximum = progress.get("transferred"), progress.get("total_files")
    elif phase == "verification":
        current, maximum = progress.get("checked"), progress.get("total_files")
    else:
        current = maximum = None
    if isinstance(current, int) and isinstance(maximum, int) and maximum > 0:
        fraction = max(0.0, min(0.99, current / maximum))
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
        # Unfiltered daemon errors may contain private device paths or tokens.
        # Show only actionable, constant recovery instructions in the GUI.
        reason = ("The backup was not verified. The daemon may retry automatically. "
                  "Confirm MTP, cable and USB port; select Retry when mounted.")
        return ("attention", "Backup needs attention", reason, None, mounted)
    if mounted:
        heading, description, fraction = progress_summary(progress)
        if phase in ACTIVE_PHASES or state.get("status") in ("inventory", "copy", "verifying", "staging", "dry-run"):
            return ("syncing", heading, description, fraction, False)
        return ("connected", "Watch connected. Preparing sync.",
                "The daemon is preparing to copy and verify your Garmin files.", None, True)
    return ("waiting", "Waiting for Garmin watch", "Connect your watch to begin.", None, False)


class SessionClock:
    """Monotonic timer, with explicitly labeled wall-clock restart recovery."""
    def __init__(self):
        self.started = None
        self.frozen = None
        self.present = False
        self.recovered = False

    def update(self, present, kind, moment, recovered_seconds=None):
        if present and not self.present:
            recovered_seconds = max(0, recovered_seconds or 0)
            self.started = moment - recovered_seconds
            self.frozen = None
            self.recovered = recovered_seconds > 0
        if self.started is not None and self.frozen is None:
            if not present or kind == "done":
                self.frozen = max(0, moment - self.started)
        self.present = present
        return self.seconds(moment)

    def seconds(self, moment):
        if self.started is None:
            return 0
        return int(self.frozen if self.frozen is not None else max(0, moment - self.started))


def format_elapsed(seconds):
    hours, rest = divmod(max(0, int(seconds)), 3600)
    minutes, seconds = divmod(rest, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}"


def recovered_elapsed(state, kind, wall=None):
    # The daemon timestamp starts at MTP discovery, not necessarily first USB.
    # Use only for companion startup during an already mounted connection.
    if not state.get("detected") or not state.get("mtp_mounted"):
        return 0
    try:
        start = dt.datetime.fromisoformat(state["connection_changed_at"])
        end = (dt.datetime.fromisoformat(state["last_success"])
               if kind == "done" else wall or dt.datetime.now(dt.timezone.utc))
        return max(0, (end - start).total_seconds())
    except (KeyError, ValueError, TypeError):
        return 0


def done_key(state):
    progress = state.get("sync_progress") or {}
    return (state.get("daemon_started_at"), state.get("connection_token"),
            progress.get("run_id"))


def stage_timeline(kind, progress):
    phase = progress.get("phase")
    if kind == "done":
        return (2, 2, 2, 2, 2)
    if kind in ("waiting", "attention") and phase not in ACTIVE_PHASES:
        return (1 if kind == "attention" else 0, 0, 0, 0, 0)
    if kind == "connected" or phase in ("detected", "inventory", "staging", "staging_source_check"):
        return (2, 1, 0, 0, 0)
    if phase in ("preview", "preview_source_check", "copy", "copy_source_check"):
        return (2, 2, 1, 0, 0)
    if phase in ("verification", "final_check", "fit_inspection", "complete"):
        return (2, 2, 2, 1, 0)
    return (2, 0, 0, 0, 0)


CSS = """
window.garmin { background-color: #101820; color: #f2f6fb; }
.garmin label { color: #f2f6fb; }
.garmin .brand { font-size: 17px; font-weight: bold; letter-spacing: 2px; }
.garmin .muted { color: #a1b4c6; font-size: 12px; }
.garmin .timer { font-size: 25px; font-weight: bold; font-family: monospace; }
.garmin .headline { font-size: 23px; font-weight: bold; }
.garmin .message { color: #bac9d7; font-size: 13px; }
.garmin .tile { background-color: #1b2933; border-radius: 12px; padding: 12px; }
.garmin .metric { font-size: 25px; font-weight: bold; }
.garmin .step { font-size: 12px; }
.garmin .step-complete { color: #36d399; }
.garmin .step-active { color: #7cbaff; font-weight: bold; }
.garmin .step-pending { color: #a1b4c6; }
.garmin.done .headline { color: #36d399; }
.garmin.attention .headline { color: #f6bd60; }
.garmin progressbar trough { background: #26394a; border: 0; border-radius: 8px; min-height: 9px; }
.garmin progressbar progress { background: #60a5fa; border: 0; border-radius: 8px; min-height: 9px; }
.garmin.done progressbar progress { background: #36d399; }
.garmin button { background: #233545; color: #f2f6fb; border: 1px solid #3d5365; border-radius: 8px; padding: 7px 16px; box-shadow: none; text-shadow: none; }
.garmin button:hover { background: #344d62; }
.garmin separator { background: #30404e; }
"""


def artwork(kind, frame=0):
    """Generate a tiny original vector scene; no external assets or network."""
    color = "#36d399" if kind == "done" else "#f6bd60" if kind == "attention" else "#60a5fa"
    moving = kind in ("syncing", "connected")
    angle = frame * 12 if moving else 0
    dash = "118 290" if moving else "409 0" if kind == "done" else "56 353"
    symbol = ("<path d='M151 78l12 12 25-28' fill='none' stroke-width='5' stroke-linecap='round' stroke-linejoin='round'/>"
              if kind == "done" else "<path d='M170 61v21m0 9v2' fill='none' stroke-width='5' stroke-linecap='round'/>"
              if kind == "attention" else "<path d='M151 79h10l5-13 9 25 6-12h8' fill='none' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>")
    dots = "".join(f"<circle cx='{44 + i * 11}' cy='78' r='3' fill='{color}' opacity='{0.3 + 0.7 * ((frame+i)%5)/4:.2f}'/>" for i in range(5))
    return f"""<svg xmlns='http://www.w3.org/2000/svg' width='340' height='156' viewBox='0 0 340 156'>
    <circle cx='170' cy='78' r='65' fill='#17242e' stroke='#26394a' stroke-width='7'/>
    <circle cx='170' cy='78' r='65' fill='none' stroke='{color}' stroke-width='6' stroke-linecap='round' stroke-dasharray='{dash}' transform='rotate({angle-90} 170 78)'/>
    <rect x='158' y='26' width='24' height='29' rx='5' fill='#526574'/>
    <rect x='158' y='101' width='24' height='29' rx='5' fill='#526574'/>
    <circle cx='170' cy='78' r='37' fill='#202e38' stroke='#9bacba' stroke-width='4'/>
    <circle cx='170' cy='78' r='29' fill='#101820'/>
    <g stroke='{color}'>{symbol}</g>{dots}
    <path d='M263 97c-22 0-25-25-8-31-1-20 27-27 38-10 22-6 32 19 15 31-4 7-14 10-23 10z' fill='#26394a' stroke='{color}' stroke-width='2'/>
    {"<path d='M271 77l8 8 14-17' fill='none' stroke='"+color+"' stroke-width='3' stroke-linecap='round' stroke-linejoin='round'/>" if kind == "done" else ""}
    </svg>"""


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
        from gi.repository import GLib, Gtk, Gdk, GdkPixbuf
    except (ImportError, ValueError) as exc:
        print("Garmin Sync UI needs PyGObject and GTK 3: " + str(exc), file=sys.stderr)
        return 1

    lock_fd = acquire_single_instance()
    if lock_fd is None:
        return 0

    class Companion(Gtk.Window):
        def __init__(self):
            super().__init__(title="Garmin Sync")
            self.set_default_size(540, 670)
            self.set_border_width(22)
            self.set_resizable(False)
            self.connect("delete-event", self.on_close)
            self.previous_present = False
            self.previous_kind = None
            self.dismissed = False
            self.last_snapshot = {}
            self.last_usb = "absent"
            self.session = SessionClock()
            self.first_tick = True
            self.prior_done = None
            self.frame = 0
            self.visual_kind = "waiting"
            self.get_style_context().add_class("garmin")
            provider = Gtk.CssProvider()
            provider.load_from_data(CSS.encode())
            Gtk.StyleContext.add_provider_for_screen(
                Gdk.Screen.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)

            def label(text="", style=None, align=0):
                widget = Gtk.Label(label=text)
                widget.set_xalign(align)
                if style:
                    widget.get_style_context().add_class(style)
                return widget

            outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=13)
            self.add(outer)
            header = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            outer.pack_start(header, False, False, 0)
            branding = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=3)
            branding.pack_start(label("GARMIN SYNC", "brand"), False, False, 0)
            branding.pack_start(label("NATIVE WATCH BACKUP", "muted"), False, False, 0)
            header.pack_start(branding, True, True, 0)
            clockbox = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            self.clock_label = label("ELAPSED TIME", "muted", 1)
            self.timer = label("00:00:00", "timer", 1)
            clockbox.pack_start(self.clock_label, False, False, 0)
            clockbox.pack_start(self.timer, False, False, 0)
            header.pack_end(clockbox, False, False, 0)
            self.hero = Gtk.Image()
            outer.pack_start(self.hero, False, False, 0)
            self.heading = label("", "headline", .5)
            self.heading.set_line_wrap(True)
            outer.pack_start(self.heading, False, False, 0)
            self.description = label("", "message", .5)
            self.description.set_line_wrap(True)
            self.description.set_max_width_chars(58)
            outer.pack_start(self.description, False, False, 0)
            tiles = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=9, homogeneous=True)
            self.metrics = []
            for caption in ("Discovered", "Transferred", "Verified"):
                tile = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
                tile.get_style_context().add_class("tile")
                value = label("—", "metric", .5)
                tile.pack_start(value, False, False, 0)
                tile.pack_start(label(caption, "muted", .5), False, False, 0)
                tiles.pack_start(tile, True, True, 0)
                self.metrics.append(value)
            outer.pack_start(tiles, False, False, 0)
            self.bar = Gtk.ProgressBar()
            self.bar.set_show_text(False)
            outer.pack_start(self.bar, False, False, 0)
            self.bar_caption = label("", "muted", .5)
            outer.pack_start(self.bar_caption, False, False, 0)
            outer.pack_start(Gtk.Separator(), False, False, 0)
            self.steps = []
            for name in ("Watch connected", "Files inventoried / captured", "Changes uploaded",
                         "Checksum verification", "Safe to disconnect"):
                step = label(name, "step")
                outer.pack_start(step, False, False, 0)
                self.steps.append((step, name))
            actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
            outer.pack_end(actions, False, False, 0)
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
            GLib.timeout_add(160, self.animate)

        def animate(self):
            if self.get_visible():
                self.frame = (self.frame + 1) % 30
                loader = GdkPixbuf.PixbufLoader.new_with_type("svg")
                loader.write(artwork(self.visual_kind, self.frame).encode())
                loader.close()
                self.hero.set_from_pixbuf(loader.get_pixbuf())
                if self.visual_kind == "syncing" and self.indeterminate:
                    self.bar.pulse()
            return True

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
            if not present:
                self.prior_done = done_key(state) if state.get("status") == "success" else self.prior_done
            if kind == "done" and done_key(state) == self.prior_done:
                kind, title, message, fraction, can_retry = (
                    "connected", "Preparing a new backup", "Waiting for current connection confirmation.", None, False)
            restored = recovered_elapsed(state, kind) if self.first_tick and present else 0
            seconds = self.session.update(present, kind, time.monotonic(), restored)
            self.first_tick = False
            self.timer.set_text(format_elapsed(seconds))
            self.clock_label.set_text("ELAPSED SINCE MOUNT" if self.session.recovered else "ELAPSED TIME")
            context = self.get_style_context()
            for name in ("attention", "done", "syncing", "connected", "waiting"):
                context.remove_class(name)
            context.add_class(kind)
            self.visual_kind = kind
            self.indeterminate = kind == "syncing" and fraction is None
            if present and not self.previous_present:
                self.dismissed = False
                self.show_all()
                self.present()
            if present and kind in ("attention", "done") and kind != self.previous_kind:
                self.dismissed = False
                self.show_all()
                self.present()
            if not present and self.previous_present and kind == "waiting":
                self.hide()
            self.heading.set_text("You're all synced!" if kind == "done" else title)
            self.description.set_text(message)
            self.retry.set_visible(can_retry)
            progress = state.get("sync_progress") or {} if present and kind not in ("waiting", "attention") else {}
            if kind == "done":
                details = state.get("last_success_details") or {}
                numbers = (details.get("considered"), details.get("copied"), details.get("verified"))
            elif kind == "syncing":
                phase = progress.get("phase")
                copied = (progress.get("transferred") if phase == "copy" and progress.get("mode") != "dry-run"
                          else progress.get("copied"))
                checked = progress.get("checked") if phase == "verification" else None
                numbers = (progress.get("total_files"), copied, checked)
            else:
                numbers = (None, None, None)
            for widget, number in zip(self.metrics, numbers):
                widget.set_text(str(number) if isinstance(number, int) else "—")
            if self.indeterminate:
                self.bar.pulse()
                self.bar_caption.set_text("Working • live progress")
            else:
                self.bar.set_fraction(fraction if fraction is not None else 0)
                self.bar_caption.set_text("Verified • safe to disconnect" if kind == "done" else
                                          (f"{round(fraction * 100)}% of current stage" if fraction is not None else kind.title()))
            for (widget, name), status in zip(self.steps, stage_timeline(kind, progress)):
                style = widget.get_style_context()
                for cls in ("step-complete", "step-active", "step-pending"):
                    style.remove_class(cls)
                style.add_class(("step-pending", "step-active", "step-complete")[status])
                widget.set_text(("○  ", "●  ", "✓  ")[status] + name)
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
