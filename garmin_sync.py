#!/usr/bin/python3
"""Copy and verify native files from a physically mounted fenix 8."""

import argparse
import contextlib
import datetime as dt
import fcntl
import getpass
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unicodedata
import urllib.parse
import uuid
import xml.etree.ElementTree as ET

VERSION = "1.1.0"
DESTINATION = "gdrive:Garmin/Fenix8_Backup"
ARCHIVE_BASE = "gdrive:Garmin/Fenix8_Archive"
RCLONE = "/usr/bin/rclone"
RUNTIME = Path(f"/run/user/{os.getuid()}/garmin-sync")
STATE_PATH = Path.home() / ".local/state/garmin-sync/state.json"
INSPECTOR = Path.home() / ".local/bin/garmin_fit_inspect.py"
SDK_PYTHON = Path.home() / ".local/bin/garmin-sync/venv/bin/python3"
DEVICE_PIN_PATH = Path.home() / ".config/garmin-sync/device.json"
BUILD_PATH = Path.home() / ".local/bin/garmin-sync/build.json"
EXCLUDED_EXTENSIONS = (
    "img|gma|unl|gcd|rgn|mp3|m4a|wav|aac|flac|ogg|wma|mp4|m4v|"
    "avi|mov|mkv|jpg|jpeg|png|gif|bmp|webp|heic|tif|tiff"
)
EXCLUDED_DIRECTORIES = "map|maps|custommaps|music|audio|audiobooks|podcasts|sid|remotesw|updatemanager"
# Exclusions precede the final inclusion; physiological FIT files have no size cap.
FILTERS = [
    "--filter", "- {{(?i).*\\.(" + EXCLUDED_EXTENSIONS + ")}}",
    "--filter", "- {{(?i)(" + EXCLUDED_DIRECTORIES + ")}}/**",
    "--filter", "+ **",
]
FINITE = ["--retries", "3", "--low-level-retries", "5", "--timeout", "60s",
          "--contimeout", "15s"]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def log(event, **fields):
    print(json.dumps({"timestamp": now(), "event": event, **fields}, ensure_ascii=True),
          flush=True)


def private_dir(path):
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    path.chmod(0o700)
    return path


class State:
    def __init__(self, path=STATE_PATH):
        self.path = path
        self.lock = threading.Lock()
        self.data = {"version": VERSION, "status": "idle", "source": None,
                     "detected": False, "considered": 0, "copied": 0,
                     "verified": 0, "archive": None, "last_success": None,
                     "fit_warnings": []}
        try:
            previous = json.loads(path.read_text())
            self.data.update(previous)
        except (OSError, ValueError):
            pass
        self.data["version"] = VERSION
        self.data["destination"] = DESTINATION
        self.data["archive_base"] = ARCHIVE_BASE

    def update(self, token=None, **fields):
        with self.lock:
            if token is not None and token != self.data.get("connection_token"):
                return False
            self.data.update(fields)
            self.data["updated_at"] = now()
            private_dir(self.path.parent)
            fd, name = tempfile.mkstemp(prefix=".state-", dir=self.path.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    json.dump(self.data, handle, ensure_ascii=True, indent=2)
                    handle.write("\n")
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(name, self.path)
            finally:
                with contextlib.suppress(FileNotFoundError):
                    os.unlink(name)
            return True

    def snapshot(self):
        with self.lock:
            return dict(self.data)


class Busy(Exception):
    pass


class DiscoveryError(Exception):
    pass


def build_info(path=BUILD_PATH):
    try:
        data = json.loads(path.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def enrolled_device(path=DEVICE_PIN_PATH):
    try:
        data = json.loads(path.read_text())
        identity = data.get("identity")
        if isinstance(identity, str) and re.fullmatch(r"[0-9a-f]{64}", identity):
            return data
    except (OSError, ValueError):
        pass
    return None


def save_enrollment(device, path=DEVICE_PIN_PATH):
    private_dir(path.parent)
    payload = {
        "identity": device["identity"],
        "model": device["model"],
        "enrolled_at": now(),
    }
    fd, name = tempfile.mkstemp(prefix=".device-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(payload, handle, ensure_ascii=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(name, 0o600)
        os.replace(name, path)
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(name)
    return payload


class RunLocks:
    """Both locks also exclude the retired launcher during transition."""
    def __init__(self, runtime=RUNTIME, legacy=None):
        self.paths = [runtime / "lock", legacy or runtime.parent / "garmin_autodev_sync.lock"]
        self.handles = []

    def __enter__(self):
        private_dir(self.paths[0].parent)
        try:
            for path in self.paths:
                fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_CLOEXEC, 0o600)
                handle = os.fdopen(fd, "r+")
                self.handles.append(handle)
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise Busy("Another Garmin copy process holds " + str(path)) from exc
            return self
        except BaseException:
            self.__exit__(None, None, None)
            raise

    def __exit__(self, *_):
        for handle in reversed(self.handles):
            handle.close()
        self.handles.clear()


def model_at(root):
    try:
        tree = ET.parse(root / "GarminDevice.xml")
        description = next((node.text or "" for node in tree.iter()
                            if node.tag.rsplit("}", 1)[-1] == "Description"), "")
        model = unicodedata.normalize("NFKD", description).encode("ascii", "ignore").decode()
        if not re.match(r"fenix\s*8(?:\D|$)", model, re.IGNORECASE):
            return None
        identity = next((node.text or "" for node in tree.iter()
                         if node.tag.rsplit("}", 1)[-1] == "Id"), "")
        return {"root": str(root), "model": description,
                "identity": hashlib.sha256(identity.encode()).hexdigest()}
    except (OSError, ET.ParseError):
        return None


def candidate_roots(base):
    # Only shallow mount/storage levels: never search a user's files recursively.
    for prefix in (base, base / "Internal Storage", base / "Primary", base / "Storage"):
        for name in ("GARMIN", "Garmin", "garmin"):
            yield prefix / name


def garmin_hint(name):
    # Checking names precedes file access: unrelated automounts can block on stat.
    return bool(re.search(r"garmin|f[ēe]nix|091e(?:_|:|/|$)", name, re.IGNORECASE))


def native_mount_paths(mounts):
    paths = []
    for mount in mounts:
        root = mount.get_root()
        path = root.get_path()
        if path and garmin_hint(mount.get_name() + " " + root.get_uri() + " " + path):
            paths.append(path)
    return paths


def discover(mount_paths=(), uid=None, username=None, expected_identity=None):
    uid = os.getuid() if uid is None else uid
    username = getpass.getuser() if username is None else username
    bases = list(dict.fromkeys(Path(path) for path in mount_paths))
    for parent in (Path(f"/run/user/{uid}/gvfs"), Path("/media") / username,
                   Path("/run/media") / username):
        try:
            for path in sorted(parent.iterdir()):
                if garmin_hint(path.name) and path not in bases:
                    bases.append(path)
        except OSError:
            pass
    found = []
    for base in bases:
        for root in candidate_roots(base):
            item = model_at(root)
            if item:
                found.append(item)
    if expected_identity:
        matches = [item for item in found if item["identity"] == expected_identity]
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            raise DiscoveryError("Enrolled Garmin identity appeared at multiple mount roots")
        if found:
            log("untrusted_device", sources=[item["root"] for item in found])
            raise DiscoveryError("Connected fenix 8 is not the enrolled device")
        return None
    if len(found) > 1:
        log("multiple_devices", sources=[item["root"] for item in found])
        raise DiscoveryError("Multiple fenix 8 devices detected; enroll or connect only one")
    return found[0] if found else None


class Cancelled(Exception):
    pass


class CommandFailed(Exception):
    pass


class Runner:
    def __init__(self, cancellation=None):
        self.cancellation = cancellation or threading.Event()
        self.lock = threading.Lock()
        self.process = None

    def cancel(self):
        self.cancellation.set()
        proc = self.process
        if proc and proc.poll() is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(proc.pid, signal.SIGTERM)

    def run(self, args, stderr_path, timeout=1230, accepted_returncodes=(0,)):
        if self.cancellation.is_set():
            raise Cancelled("Connection interrupted")
        with open(stderr_path, "ab") as stderr:
            proc = subprocess.Popen(args, stdout=subprocess.PIPE, stderr=stderr,
                                    start_new_session=True)
            with self.lock:
                self.process = proc
            start = time.monotonic()
            terminated = None
            try:
                while True:
                    if self.cancellation.is_set() or time.monotonic() - start > timeout:
                        if terminated is None:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(proc.pid, signal.SIGTERM)
                            terminated = time.monotonic()
                        elif time.monotonic() - terminated > 3:
                            with contextlib.suppress(ProcessLookupError):
                                os.killpg(proc.pid, signal.SIGKILL)
                    try:
                        stdout, _ = proc.communicate(timeout=0.2)
                        break
                    except subprocess.TimeoutExpired:
                        continue
                if self.cancellation.is_set():
                    raise Cancelled("Connection interrupted")
                if terminated is not None:
                    raise CommandFailed("Command exceeded finite duration")
                if proc.returncode not in accepted_returncodes:
                    raise CommandFailed(f"{Path(args[0]).name} {args[1]} exited {proc.returncode}; see {stderr_path}")
                return stdout.decode("utf-8")
            finally:
                with self.lock:
                    self.process = None


def signature(inventory):
    return sorted((item["Path"], item["Size"], item.get("ModTime")) for item in inventory)


def read_changes(path):
    objects = {}
    stats = {}
    try:
        for line in path.read_text(errors="replace").splitlines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry.get("stats"), dict):
                stats = entry["stats"]
            message = entry.get("msg", "")
            if "Copied" in message or "Skipped copy" in message:
                object_name = entry.get("object")
                if object_name:
                    objects[object_name] = message
    except OSError:
        pass
    return objects, stats


def representative_files(inventory):
    groups = ("activity/", "monitor/", "debug/metricsbackup/", "hrvstatus/", "sleep/", "metrics/")
    selected = []
    paths = sorted((item["Path"] for item in inventory if item["Path"].casefold().endswith(".fit")),
                   reverse=True)
    for group in groups:
        choice = next((name for name in paths if name.casefold().startswith(group)), None)
        if choice:
            selected.append(choice)
    return selected


def mount_matches_source(mount, source):
    root = mount.get_root()
    path = root.get_path()
    if path:
        return source == path or source.startswith(path.rstrip("/") + "/")
    # Be conservative for URI-only mounts. A scheme match alone can confuse an
    # unrelated phone removal with the Garmin. The scheduled rescan handles
    # transports whose URI host cannot be correlated to the GVFS host.
    uri = root.get_uri()
    scheme = uri.partition(":")[0]
    if scheme not in ("mtp", "gphoto2"):
        return False
    match = re.search(r"/gvfs/" + re.escape(scheme) + r":host=([^/]+)", source)
    if not match:
        return False
    source_host = urllib.parse.unquote(match.group(1)).strip("[]")
    uri_host = urllib.parse.unquote(urllib.parse.urlparse(uri).netloc).strip("[]")
    return bool(uri_host) and uri_host == source_host


class SyncRun:
    def __init__(self, state, runner=None, runtime=RUNTIME, destination=DESTINATION,
                 archive_base=ARCHIVE_BASE, token=None, inspect=True,
                 inspector=INSPECTOR, sdk_python=SDK_PYTHON):
        self.state = state
        self.runner = runner or Runner()
        self.runtime = runtime
        self.destination = destination
        self.archive_base = archive_base
        self.token = token
        self.inspect = inspect
        self.inspector = Path(inspector)
        self.sdk_python = Path(sdk_python)

    def update(self, **fields):
        if self.state:
            self.state.update(token=self.token, **fields)

    def execute(self, source, mode="copy"):
        private_dir(self.runtime)
        run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ") + "-" + uuid.uuid4().hex
        with tempfile.TemporaryDirectory(prefix="run-", dir=self.runtime) as dirname:
            directory = Path(dirname)
            log_root = (self.state.path.parent if self.state else STATE_PATH.parent) / "runs"
            private_dir(log_root)
            log_directory = private_dir(log_root / run_id)
            # Only this program's UUID run directories qualify for bounded retention.
            previous = sorted(path for path in log_root.iterdir() if path.is_dir() and
                              re.fullmatch(r"\d{8}T\d{6}\.\d{6}Z-[0-9a-f]{32}", path.name))
            for path in previous[:-32]:
                shutil.rmtree(path)
            logfile = log_directory / "rclone.jsonl"
            stderr = log_directory / "stderr.log"
            self.update(log_path=str(log_directory))
            original_device = model_at(Path(source))
            if not original_device:
                raise Cancelled("Device identity missing at start")
            common = FINITE + ["--use-json-log", "--log-level", "INFO", "--log-file", str(logfile)]

            def inventory():
                result = self.runner.run([RCLONE, "lsjson", source, "-R", "--files-only",
                                          *FILTERS, *common], stderr, timeout=300)
                items = json.loads(result)
                for item in items:
                    name = item["Path"]
                    if name.startswith("/") or ".." in Path(name).parts:
                        raise CommandFailed("Unsafe inventory path")
                return items

            before = inventory()
            (log_directory / "inventory.json").write_text(json.dumps(before, ensure_ascii=True, indent=2))
            if not before:
                raise CommandFailed("Selected native file inventory is empty")
            # Rclone displays physical LF/CR as Unicode control pictures in lsjson.
            odd_names = any(any(character in item["Path"] for character in "\n\r\u240a\u240d")
                            for item in before)
            manifest = directory / "files.txt"
            if odd_names:
                selection = FILTERS
                log("selection_filter_fallback", reason="Filename contains a line separator")
            else:
                manifest.write_text("".join(item["Path"] + "\n" for item in before))
                manifest.chmod(0o600)
                selection = ["--files-from-raw", str(manifest)]
            archive = self.archive_base.rstrip("/") + "/" + run_id
            self.update(status=mode, considered=len(before), copied=0, verified=0,
                        archive=archive if mode == "copy" else None, run_started_at=now(),
                        selection="filters" if odd_names else "manifest", last_error=None)
            log("inventory", source=source, files=len(before), bytes=sum(i["Size"] for i in before),
                run_id=run_id, mode=mode, representative_fit=representative_files(before))
            if mode in ("copy", "dry-run"):
                args = [RCLONE, "copy", source, self.destination, "--checksum", "--no-traverse",
                        "--backup-dir", archive, "--max-duration", "20m", "--stats", "30s",
                        *selection, *common]
                # Every whole-copy retry gets its own archive directory in the controller.
                args += ["--retries", "1"]
                if mode == "dry-run":
                    args.append("--dry-run")
                else:
                    self.runner.run([*args, "--dry-run"], stderr)
                    proposed, preview_stats = read_changes(logfile)
                    log("preview_completed", run_id=run_id, proposed_count=len(proposed),
                        proposed_files=list(proposed), stats=preview_stats)
                    if logfile.exists():
                        os.replace(logfile, log_directory / "preview.jsonl")
                    if signature(inventory()) != signature(before):
                        raise CommandFailed("Source inventory changed during preview; retry required")
                    if self.runner.cancellation.is_set() or model_at(Path(source)) != original_device:
                        raise Cancelled("Device disconnected or replaced during preview")
                self.runner.run(args, stderr)
                changes, stats = read_changes(logfile)
                copied = len(changes)
                self.update(copied=copied, copy_completed_at=now(), transfer_stats=stats)
                log("copy_completed", run_id=run_id, copied=copied,
                    changed_files=list(changes), dry_run=mode == "dry-run", stats=stats)
                if signature(inventory()) != signature(before):
                    raise CommandFailed("Source inventory changed during copy; retry required")
            else:
                copied = 0
            if mode == "dry-run":
                return {"mode": mode, "considered": len(before), "copied": copied,
                        "verified": 0, "archive": None, "fit_warnings": []}
            self.update(status="verifying")
            self.runner.run([RCLONE, "check", source, self.destination, "--one-way",
                             *selection, *common], stderr)
            if signature(inventory()) != signature(before):
                raise CommandFailed("Source inventory changed during verification; retry required")
            if self.runner.cancellation.is_set() or model_at(Path(source)) != original_device:
                raise Cancelled("Device disconnected before completion")
            warnings = self.inspect_fits(source, before, stderr) if self.inspect else []
            if self.runner.cancellation.is_set() or model_at(Path(source)) != original_device:
                raise Cancelled("Device disconnected during FIT inspection")
            log("verified", run_id=run_id, files=len(before), copied=copied, fit_warnings=warnings)
            return {"mode": mode, "considered": len(before), "copied": copied,
                    "verified": len(before), "archive": archive if mode == "copy" else None,
                    "fit_warnings": warnings, "verified_at": now()}

    def inspect_fits(self, source, inventory, stderr):
        files = representative_files(inventory)
        if not files:
            return []
        if not self.inspector.exists() or not self.sdk_python.exists():
            return ["Optional FIT inspector unavailable; copied bytes were verified"]
        try:
            output = self.runner.run([str(self.sdk_python), str(self.inspector),
                                      *[str(Path(source) / name) for name in files]], stderr, timeout=90,
                                     accepted_returncodes=(0, 1))
            results = json.loads(output)
            warnings = [result for result in results
                        if not result.get("decode_ok") or not result.get("integrity_ok")]
            log("fit_inspected", results=results)
            return warnings
        except Cancelled:
            raise
        except (CommandFailed, ValueError) as exc:
            return [str(exc)]


class Controller:
    """Connection state changes stay on the GLib thread; worker completion returns there."""
    def __init__(self, state, launch, clock=time.monotonic):
        self.state = state
        self.launch = launch
        self.clock = clock
        self.source = None
        self.epoch = 0
        self.worker = None
        self.pending = None
        self.successful = False
        self.retry_at = 0
        self.failures = 0
        self.stopping = False
        self.state.update(status="waiting", source=None, detected=False,
                          connection_token=0, daemon_started_at=now(),
                          daemon_pid=os.getpid(), build=build_info(),
                          enrolled=bool(enrolled_device()))

    def observe(self, source, disconnected_root=None):
        if self.stopping:
            return
        changed = source != self.source or (self.source and disconnected_root == self.source["root"])
        if changed:
            self.epoch += 1
            if self.worker:
                self.worker.cancel()
            self.source = source
            self.successful = False
            self.failures = 0
            self.retry_at = 0
            self.state.update(source=source["root"] if source else None,
                              model=source["model"] if source else None,
                              detected=bool(source), connection_token=self.epoch,
                              status="connected" if source else "waiting", connection_changed_at=now())
            log("connected" if source else "disconnected", source=source["root"] if source else None)
        self.tick()

    def request(self, mode):
        log("request", mode=mode, deferred=bool(self.worker))
        if self.stopping:
            return
        self.pending = mode
        self.tick()

    def tick(self):
        if self.stopping or not self.source or self.worker:
            return
        mode = self.pending
        if mode is None:
            if self.successful or self.clock() < self.retry_at:
                return
            mode = "copy"
        self.pending = None
        self.worker = self.launch(self.source, mode, self.epoch, self.complete)

    def complete(self, epoch, mode, result=None, error=None):
        self.worker = None
        if self.stopping or epoch != self.epoch or not self.source:
            self.tick()
            return
        if error:
            if mode == "copy":
                self.successful = False
            self.failures += 1
            delay = min(900, 120 * 2 ** min(self.failures - 1, 4))
            self.retry_at = self.clock() + delay
            self.state.update(status="retry_wait", last_error=str(error), failed_at=now(),
                              retry_after_seconds=delay, retry_at=dt.datetime.fromtimestamp(
                                  time.time() + delay, dt.timezone.utc).isoformat())
            log("run_failed", error=str(error), retry_after_seconds=delay)
        else:
            fields = dict(result or {})
            fields.update(status="connected" if mode == "dry-run" else "success", completed_at=now())
            if mode == "copy":
                self.successful = True
                self.failures = 0
                self.retry_at = 0
                fields.update(last_success=now(), retry_at=None, last_error=None)
                fields["last_success_details"] = {**result, "source": self.source["root"],
                                                  "completed_at": fields["last_success"]}
            self.state.update(**fields)
        self.tick()

    def stop(self):
        self.stopping = True
        if self.worker:
            self.worker.cancel()
        self.state.update(status="stopping", stopped_at=now())


def daemon(state):
    import gi
    gi.require_version("Gio", "2.0")
    gi.require_version("GLibUnix", "2.0")
    from gi.repository import Gio, GLib, GLibUnix
    loop = GLib.MainLoop()
    monitor = Gio.VolumeMonitor.get()
    threads = []

    def launch(source, mode, epoch, complete):
        runner = Runner()
        threads[:] = [thread for thread in threads if thread.is_alive()]

        def work():
            try:
                result = SyncRun(state, runner=runner, token=epoch).execute(source["root"], mode)
                error = None
            except Exception as exc:
                result, error = None, exc
            GLib.idle_add(lambda: (complete(epoch, mode, result, error), False)[1])

        thread = threading.Thread(target=work, name="garmin-copy", daemon=False)
        threads.append(thread)
        thread.start()
        return runner

    controller = Controller(state, launch)

    def mounts():
        return native_mount_paths(monitor.get_mounts())

    def scan(*_):
        pin = enrolled_device()
        if not pin:
            controller.observe(None)
            state.update(status="unenrolled", detected=False, source=None,
                         enrolled=False, last_error="No Garmin device is enrolled")
            return True
        try:
            source = discover(mounts(), expected_identity=pin["identity"])
        except DiscoveryError as exc:
            controller.observe(None)
            state.update(status="blocked", detected=False, source=None,
                         enrolled=True, last_error=str(exc))
            log("discovery_blocked", error=str(exc))
            return True
        state.update(enrolled=True)
        controller.observe(source)
        return True

    def mount_removed(_, mount):
        if controller.source and mount_matches_source(mount, controller.source["root"]):
            controller.observe(None)
        GLib.timeout_add(500, lambda: (scan(), False)[1])

    def shutdown():
        controller.stop()
        loop.quit()
        return False

    for name in ("mount-added", "mount-changed", "volume-added", "volume-changed"):
        monitor.connect(name, lambda *_: GLib.timeout_add(500, lambda: (scan(), False)[1]))
    monitor.connect("mount-removed", mount_removed)
    monitor.connect("mount-pre-unmount", mount_removed)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, shutdown)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, shutdown)
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR1,
                         lambda: (controller.request("copy"), True)[1])
    GLibUnix.signal_add(GLib.PRIORITY_DEFAULT, signal.SIGUSR2,
                         lambda: (controller.request("dry-run"), True)[1])
    GLib.timeout_add_seconds(60, scan)
    GLib.timeout_add_seconds(5, lambda: (controller.tick(), True)[1])
    scan()
    log("daemon_started", pid=os.getpid(), version=VERSION)
    try:
        loop.run()
    finally:
        controller.stop()
        for thread in threads:
            thread.join(timeout=8)
        state.update(status="stopped", stopped_at=now())
        log("daemon_stopped")
    return 0


def once(state, mode):
    pin = enrolled_device()
    if not pin:
        if state:
            state.update(status="unenrolled", detected=False, source=None,
                         enrolled=False, last_error="No Garmin device is enrolled")
        log("device_unenrolled")
        return 3
    source = discover(expected_identity=pin["identity"])
    if not source:
        if state:
            state.update(detected=False, source=None, status="waiting")
        log("device_absent")
        return 2
    runner = Runner()
    old_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig] = signal.signal(sig, lambda *_: runner.cancel())
    try:
        if state:
            state.update(detected=True, source=source["root"], model=source["model"])
        result = SyncRun(state, runner=runner).execute(source["root"], mode)
        if state:
            fields = {**result, "status": "success", "completed_at": now(), "last_error": None}
            if mode == "copy":
                fields["last_success"] = now()
                fields["last_success_details"] = {**result, "source": source["root"],
                                                  "completed_at": fields["last_success"]}
            state.update(**fields)
        return 0
    except (CommandFailed, Cancelled, OSError, ValueError) as exc:
        if state:
            state.update(status="failed", last_error=str(exc), failed_at=now())
        log("run_failed", error=str(exc))
        return 130 if isinstance(exc, Cancelled) else 1
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)


def service_snapshot():
    service = subprocess.run(["/usr/bin/systemctl", "--user", "show", "garmin-sync.service",
                              "--property=ActiveState,SubState,MainPID,Result"],
                             capture_output=True, text=True)
    fields = dict(line.split("=", 1) for line in service.stdout.splitlines() if "=" in line)
    if service.returncode:
        fields["error"] = service.stderr.strip()
    return fields


def readiness(expected_commit=None):
    service = service_snapshot()
    state = State().snapshot()
    build = build_info()
    main_pid = service.get("MainPID")
    ready = (service.get("ActiveState") == "active" and main_pid not in (None, "", "0") and
             str(state.get("daemon_pid")) == str(main_pid) and
             state.get("status") not in ("stopped", "stopping", "failed"))
    if expected_commit:
        ready = ready and build.get("commit") == expected_commit and \
            (state.get("build") or {}).get("commit") == expected_commit
    payload = {"ready": bool(ready), "service": service, "state": state,
               "build": build, "enrollment": enrolled_device()}
    return bool(ready), payload


def doctor():
    checks = {}
    checks["rclone_binary"] = Path(RCLONE).is_file()
    checks["gdrive_configured"] = False
    checks["gdrive_available"] = False
    checks["archive_policy"] = "preserve_indefinitely_manual_review"
    checks["archive_run_directories"] = None
    if checks["rclone_binary"]:
        remotes = subprocess.run([RCLONE, "listremotes"], capture_output=True, text=True)
        checks["gdrive_configured"] = remotes.returncode == 0 and "gdrive:" in remotes.stdout.splitlines()
        if checks["gdrive_configured"]:
            probe = subprocess.run(
                [RCLONE, "lsf", "gdrive:Garmin", "--max-depth", "1", "--dirs-only",
                 "--timeout", "15s", "--contimeout", "5s", "--retries", "1",
                 "--low-level-retries", "1"],
                capture_output=True, text=True, timeout=30)
            checks["gdrive_available"] = probe.returncode == 0
            archive = subprocess.run(
                [RCLONE, "lsf", ARCHIVE_BASE, "--max-depth", "1", "--dirs-only",
                 "--timeout", "15s", "--contimeout", "5s", "--retries", "1",
                 "--low-level-retries", "1"],
                capture_output=True, text=True, timeout=30)
            if archive.returncode == 0:
                checks["archive_run_directories"] = len(
                    [line for line in archive.stdout.splitlines() if line.strip()])
    gio = subprocess.run(["/usr/bin/python3", "-c",
                          "import gi; gi.require_version('Gio','2.0'); from gi.repository import Gio"],
                         capture_output=True, text=True)
    checks["gio"] = gio.returncode == 0
    sdk = subprocess.run([str(SDK_PYTHON), "-c",
                          "from importlib.metadata import version; print(version('garmin-fit-sdk'))"],
                         capture_output=True, text=True) if SDK_PYTHON.exists() else None
    checks["fit_sdk"] = bool(sdk and sdk.returncode == 0)
    checks["fit_sdk_version"] = sdk.stdout.strip() if checks["fit_sdk"] else None
    checks["inspector"] = INSPECTOR.is_file()
    checks["enrolled_device"] = bool(enrolled_device())
    checks["build_metadata"] = bool(build_info().get("commit"))
    legacy = Path.home() / ".config/autostart/garmin-sync.desktop"
    checks["legacy_autostart_retired"] = not legacy.exists()
    ok = all(checks[name] for name in ("rclone_binary", "gdrive_configured", "gdrive_available",
                                        "gio", "fit_sdk", "inspector", "enrolled_device",
                                        "build_metadata", "legacy_autostart_retired"))
    print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    return 0 if ok else 1


def enroll(replace=False):
    existing = enrolled_device()
    if existing and not replace:
        print(json.dumps({"enrolled": True, "device": existing,
                          "message": "A device is already enrolled; use enroll --replace explicitly."},
                         indent=2))
        return 4
    if existing and replace:
        service = service_snapshot()
        if service.get("ActiveState") == "active":
            print("Stop garmin-sync.service before replacing the enrolled device.", file=sys.stderr)
            return 75
    try:
        device = discover()
    except DiscoveryError as exc:
        log("enroll_failed", error=str(exc))
        return 1
    if not device:
        log("device_absent")
        return 2
    saved = save_enrollment(device)
    print(json.dumps({"enrolled": True, "device": saved}, indent=2))
    return 0


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon")
    single = commands.add_parser("once")
    single.add_argument("--dry-run", action="store_true")
    for name in ("status", "force", "dry-run", "verify", "doctor", "readiness"):
        commands.add_parser(name)
    enrollment = commands.add_parser("enroll")
    enrollment.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "status":
        ready, payload = readiness()
        healthy_states = {"waiting", "connected", "success", "retry_wait", "copy", "verifying", "dry-run"}
        healthy = ready and bool(payload["enrollment"]) and payload["state"].get("status") in healthy_states
        payload["healthy"] = bool(healthy)
        print(json.dumps(payload, indent=2))
        return 0 if healthy else 1
    if args.command == "readiness":
        ready, payload = readiness(os.environ.get("GARMIN_SYNC_EXPECT_COMMIT"))
        print(json.dumps(payload, indent=2))
        return 0 if ready else 1
    if args.command == "doctor":
        return doctor()
    if args.command == "enroll":
        return enroll(args.replace)
    if args.command in ("force", "dry-run"):
        sig = "USR1" if args.command == "force" else "USR2"
        log("request_sent", mode=args.command, signal=sig)
        return subprocess.call(["/usr/bin/systemctl", "--user", "kill", "--kill-who=main",
                                "--signal=" + sig, "garmin-sync.service"])
    try:
        with RunLocks():
            if args.command == "daemon":
                return daemon(State())
            if args.command == "verify":
                return once(None, "verify")
            if args.command == "once" and args.dry_run:
                return once(None, "dry-run")
            return once(State(), "copy")
    except Busy as exc:
        log("busy", error=str(exc))
        return 75


if __name__ == "__main__":
    sys.exit(main())
