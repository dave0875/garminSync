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
import plistlib
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

VERSION = "1.5.0"
DESTINATION = "gdrive:Garmin/Fenix8_Backup"
ARCHIVE_BASE = "gdrive:Garmin/Fenix8_Archive"
IS_DARWIN = sys.platform == "darwin"
SOURCE_ROOT = Path(__file__).resolve().parent
INSTALLED_ROOT = Path.home() / ".local/bin/garmin-sync"


def resolve_executable(name, *preferred):
    found = shutil.which(name)
    if found:
        return found
    for candidate in preferred:
        if Path(candidate).is_file():
            return candidate
    return preferred[0] if preferred else name


RCLONE = resolve_executable(
    "rclone", "/usr/bin/rclone", "/opt/local/bin/rclone",
    "/usr/local/bin/rclone", "/opt/homebrew/bin/rclone")
MTP_FILETREE = resolve_executable(
    "mtp-filetree", "/opt/local/bin/mtp-filetree", "/usr/local/bin/mtp-filetree",
    "/opt/homebrew/bin/mtp-filetree")
MTP_FILES = resolve_executable(
    "mtp-files", "/opt/local/bin/mtp-files", "/usr/local/bin/mtp-files",
    "/opt/homebrew/bin/mtp-files")
MTP_CONNECT = resolve_executable(
    "mtp-connect", "/opt/local/bin/mtp-connect", "/usr/local/bin/mtp-connect",
    "/opt/homebrew/bin/mtp-connect")
IOREG = "/usr/sbin/ioreg"
GARMIN_USB_VENDOR_ID = 0x091E
FENIX8_USB_PRODUCT_ID = 0x51B5
MACOS_LAUNCHD_LABEL = "com.dave0875.garmin-sync"
RUNTIME = (Path.home() / "Library/Caches/garmin-sync" if IS_DARWIN
           else Path(f"/run/user/{os.getuid()}/garmin-sync"))
STATE_PATH = Path.home() / ".local/state/garmin-sync/state.json"
INSPECTOR = (INSTALLED_ROOT.parent / "garmin_fit_inspect.py"
             if (INSTALLED_ROOT.parent / "garmin_fit_inspect.py").is_file()
             else SOURCE_ROOT / "garmin_fit_inspect.py")
SDK_PYTHON = (INSTALLED_ROOT / "venv/bin/python3"
              if (INSTALLED_ROOT / "venv/bin/python3").is_file()
              else Path(sys.executable))
DEVICE_PIN_PATH = Path.home() / ".config/garmin-sync/device.json"
BUILD_PATH = INSTALLED_ROOT / "build.json"
EXCLUDED_EXTENSIONS = (
    "img|gma|unl|gcd|rgn|mp3|m4a|wav|aac|flac|ogg|wma|mp4|m4v|"
    "avi|mov|mkv|jpg|jpeg|png|gif|bmp|webp|heic|tif|tiff"
)
EXCLUDED_DIRECTORIES = "map|maps|custommaps|music|audio|audiobooks|podcasts|sid|remotesw|updatemanager"
EXCLUDED_EXTENSION_SET = {value.casefold() for value in EXCLUDED_EXTENSIONS.split("|")}
EXCLUDED_DIRECTORY_SET = {value.casefold() for value in EXCLUDED_DIRECTORIES.split("|")}
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


SYNC_FRESHNESS_WINDOW = dt.timedelta(hours=24)


def sync_health(state, service_ready, enrolled=True, current_time=None):
    """Operator-facing backup health, independent of deployment readiness.

    Waiting for a physically absent watch is normal. A successful process
    launch, proposed transfer, or copied file never counts as verification.
    """
    current_time = current_time or dt.datetime.now(dt.timezone.utc)
    last = state.get("last_success")
    age = None
    if last:
        try:
            verified_time = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
            if verified_time.tzinfo is not None:
                age = max(0, int((current_time - verified_time).total_seconds()))
        except (TypeError, ValueError, OverflowError):
            pass
    freshness = ("never_verified" if not last else "unknown" if age is None else
                 "stale" if age > SYNC_FRESHNESS_WINDOW.total_seconds() else "recent")
    phase = state.get("status") or "unknown"
    detected = bool(state.get("detected"))
    if not service_ready:
        condition = "service_unavailable"
    elif not enrolled or phase == "unenrolled":
        condition = "unenrolled"
    elif phase in ("blocked", "failed", "retry_wait"):
        condition = "degraded"
    elif not detected:
        condition = "waiting_for_device"
    elif phase in ("copy", "verifying", "dry-run"):
        condition = "syncing"
    elif phase == "success" and last:
        condition = "verified"
    else:
        condition = "awaiting_verification"
    return {
        "condition": condition,
        "device_availability": "mounted" if detected else "not_mounted",
        "phase": phase,
        "last_verified_backup_at": last,
        "last_verified_backup_age_seconds": age,
        "backup_freshness": freshness,
        "last_error": state.get("last_error"),
        "last_failure_at": state.get("failed_at"),
        "last_connection_change_at": state.get("connection_changed_at"),
    }


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
                         if node.tag.rsplit("}", 1)[-1] == "Id"), "").strip()
        if not identity:
            return None
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
    if len(found) > 1:
        log("multiple_devices", sources=[item["root"] for item in found])
        raise DiscoveryError("Multiple fenix 8 devices detected; connect only the enrolled watch")
    if expected_identity:
        if found and found[0]["identity"] != expected_identity:
            log("untrusted_device", source=found[0]["root"])
            raise DiscoveryError("Connected fenix 8 is not the enrolled device")
        return found[0] if found else None
    return found[0] if found else None


class Cancelled(Exception):
    pass


class CommandFailed(Exception):
    pass


_MTP_TREE_LINE = re.compile(r"^(?P<indent> *)(?P<id>\d+)\s+(?P<name>.+?)\s*$")
_MTP_FILE_ID = re.compile(r"^\s*File ID:\s*(?P<id>\d+)\s*$")
_MTP_FILE_SIZE = re.compile(r"^\s*File size\s+(?P<size>\d+)\b")
_MTP_PARENT_ID = re.compile(r"^\s*Parent ID:\s*(?P<id>\d+)\s*$")
_MTP_STORAGE_ID = re.compile(r"^\s*Storage ID:\s*0x(?P<id>[0-9A-Fa-f]+)\s*$")


def normalize_mtp_line(raw):
    """Normalize libmtp's terminal whitespace without changing object names."""
    return raw.replace("\u00a0", " ").expandtabs(2)


def parse_mtp_filetree(output):
    """Return MTP object-id -> slash-separated device path from mtp-filetree."""
    paths = {}
    stack = {}
    for raw in output.splitlines():
        line = normalize_mtp_line(raw)
        match = _MTP_TREE_LINE.match(line)
        if not match:
            continue
        indent = len(match.group("indent"))
        if indent % 2:
            raise CommandFailed("Unexpected mtp-filetree indentation")
        depth = indent // 2
        name = match.group("name")
        if name in (".", "..") or "/" in name or "\x00" in name:
            raise CommandFailed("Unsafe MTP object name")
        if depth:
            parent = stack.get(depth - 1)
            if parent is None:
                raise CommandFailed("Malformed mtp-filetree hierarchy")
            parts = (*parent, name)
        else:
            parts = (name,)
        stack[depth] = parts
        for key in tuple(stack):
            if key > depth:
                del stack[key]
        paths[int(match.group("id"))] = "/".join(parts)
    return paths


def parse_mtp_files(output):
    """Return metadata for actual files (folders are omitted by mtp-files)."""
    records = {}
    current = None
    for raw in output.splitlines():
        line = normalize_mtp_line(raw)
        match = _MTP_FILE_ID.match(line)
        if match:
            if current and current.get("size") is not None:
                records[current["id"]] = current
            current = {"id": int(match.group("id")), "size": None,
                       "parent_id": None, "storage_id": None}
            continue
        if current is None:
            continue
        match = _MTP_FILE_SIZE.match(line)
        if match:
            current["size"] = int(match.group("size"))
            continue
        match = _MTP_PARENT_ID.match(line)
        if match:
            current["parent_id"] = int(match.group("id"))
            continue
        match = _MTP_STORAGE_ID.match(line)
        if match:
            current["storage_id"] = int(match.group("id"), 16)
    if current and current.get("size") is not None:
        records[current["id"]] = current
    return records


def mtp_selected_inventory(tree_output, files_output):
    """Join libmtp listings and apply the same native-file exclusions as rclone."""
    paths = parse_mtp_filetree(tree_output)
    files = parse_mtp_files(files_output)
    selected = []
    seen = set()
    for object_id, metadata in files.items():
        path = paths.get(object_id)
        if not path:
            continue
        parts = path.split("/")
        garmin_index = next((index for index, part in enumerate(parts)
                             if part.casefold() == "garmin"), None)
        if garmin_index is None or garmin_index == len(parts) - 1:
            continue
        relative_parts = parts[garmin_index + 1:]
        if any(part.casefold() in EXCLUDED_DIRECTORY_SET for part in relative_parts[:-1]):
            continue
        suffix = Path(relative_parts[-1]).suffix.lstrip(".").casefold()
        if suffix in EXCLUDED_EXTENSION_SET:
            continue
        relative = "/".join(relative_parts)
        if relative.startswith("/") or ".." in Path(relative).parts:
            raise CommandFailed("Unsafe MTP inventory path")
        if relative in seen:
            raise CommandFailed("Duplicate MTP path: " + relative)
        seen.add(relative)
        selected.append({"id": object_id, "path": relative, "size": metadata["size"]})
    return sorted(selected, key=lambda item: item["path"].casefold())


def mtp_inventory(runner, stderr):
    for tool in (MTP_FILETREE, MTP_FILES, MTP_CONNECT):
        if not Path(tool).is_file():
            raise CommandFailed(f"Required libmtp tool not found: {tool}")
    tree = runner.run([MTP_FILETREE], stderr, timeout=300)
    files = runner.run([MTP_FILES], stderr, timeout=300)
    selected = mtp_selected_inventory(tree, files)
    if not selected:
        raise DiscoveryError("No GARMIN files were exposed by the connected MTP device")
    return selected


def _mtp_signature(items):
    return sorted((item["path"], item["size"]) for item in items)


def _mtp_download_batches(entries, object_root, max_items=1024, arg_budget=96 * 1024):
    """Pack getfile operations while leaving ample headroom below macOS exec argv limits."""
    batches = []
    batch = []
    used = len(str(MTP_CONNECT)) + 1
    for item in entries:
        target = object_root / f"{item['id']}.part"
        # Approximate exec argv bytes including separators/NULs for both arguments.
        cost = len("--getfile") + 1 + len(str(item["id"])) + 1 + len(str(target)) + 2
        if batch and (len(batch) >= max_items or used + cost > arg_budget):
            batches.append(batch)
            batch = []
            used = len(str(MTP_CONNECT)) + 1
        batch.append(item)
        used += cost
    if batch:
        batches.append(batch)
    return batches


def download_mtp_entries(entries, garmin_root, runner, stderr, max_items=1024,
                         arg_budget=96 * 1024):
    """Download read-only MTP objects using as few persistent libmtp sessions as practical."""
    garmin_root.mkdir(parents=True, exist_ok=True)
    object_root = private_dir(garmin_root.parent / "objects")
    batches = _mtp_download_batches(entries, object_root, max_items=max_items,
                                    arg_budget=arg_budget)
    for batch in batches:
        args = [MTP_CONNECT]
        object_paths = {}
        for item in batch:
            target = object_root / f"{item['id']}.part"
            with contextlib.suppress(FileNotFoundError):
                target.unlink()
            object_paths[item["id"]] = target
            args.extend(["--getfile", f"{item['id']},{target}"])
        runner.run(args, stderr, timeout=1230)
        for item in batch:
            staged = object_paths[item["id"]]
            if not staged.is_file():
                raise CommandFailed(f"MTP object {item['id']} was not downloaded")
            if staged.stat().st_size != item["size"]:
                raise CommandFailed(
                    f"MTP object {item['id']} size changed during download "
                    f"({staged.stat().st_size} != {item['size']})")
            destination = garmin_root / item["path"]
            destination.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, destination)
    return len(batches)


@contextlib.contextmanager
def stage_macos_device(expected_identity=None, metadata_only=False, runner=None):
    """Materialize a verified read-only snapshot of a Fenix MTP filesystem."""
    if not IS_DARWIN:
        raise DiscoveryError("The libmtp staging transport is only used on macOS")
    runner = runner or Runner()
    private_dir(RUNTIME)
    with tempfile.TemporaryDirectory(prefix="mtp-", dir=RUNTIME) as dirname:
        root = Path(dirname)
        stderr = root / "mtp.stderr.log"
        started = time.monotonic()
        before = mtp_inventory(runner, stderr)
        log("mtp_inventory_completed", files=len(before),
            seconds=round(time.monotonic() - started, 3))
        xml = [item for item in before if item["path"].casefold() == "garmindevice.xml"]
        if len(xml) != 1:
            raise DiscoveryError("Expected exactly one GARMIN/GarminDevice.xml over MTP")
        selected = xml if metadata_only else before
        garmin_root = root / "GARMIN"
        download_started = time.monotonic()
        sessions = download_mtp_entries(selected, garmin_root, runner, stderr)
        log("mtp_download_completed", files=len(selected),
            bytes=sum(item["size"] for item in selected), sessions=sessions,
            seconds=round(time.monotonic() - download_started, 3))
        device = model_at(garmin_root)
        if not device:
            raise DiscoveryError("Connected MTP device is not a Fenix 8 with a valid GarminDevice.xml")
        if expected_identity and device["identity"] != expected_identity:
            raise DiscoveryError("Connected fenix 8 is not the enrolled device")
        if not metadata_only:
            reinventory_started = time.monotonic()
            after = mtp_inventory(runner, stderr)
            log("mtp_reinventory_completed", files=len(after),
                seconds=round(time.monotonic() - reinventory_started, 3),
                stage_seconds=round(time.monotonic() - started, 3))
            if _mtp_signature(after) != _mtp_signature(before):
                raise CommandFailed("MTP source inventory changed during staging; retry required")
            final_device = model_at(garmin_root)
            if final_device != device:
                raise Cancelled("Device identity changed during MTP staging")
        device["display_source"] = "mtp:Garmin/fenix8"
        device["transport"] = "libmtp"
        yield device


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
                    action = args[1] if len(args) > 1 else Path(args[0]).name
                    raise CommandFailed(
                        f"{Path(args[0]).name} {action} exited {proc.returncode}; see {stderr_path}")
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

    def execute(self, source, mode="copy", source_snapshot=False):
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
                run_id=run_id, mode=mode, source_snapshot=source_snapshot,
                representative_fit=representative_files(before))
            if mode in ("copy", "dry-run"):
                args = [RCLONE, "copy", source, self.destination, "--checksum"]
                # A verified macOS MTP snapshot is immutable for the lifetime of this run.
                # Let rclone enumerate the remote in bulk instead of doing per-directory or
                # per-file Drive lookups.
                if source_snapshot:
                    args.append("--fast-list")
                else:
                    args.append("--no-traverse")
                args += ["--backup-dir", archive, "--max-duration", "20m", "--stats", "30s",
                         *selection, *common]
                # Every whole-copy retry gets its own archive directory in the controller.
                args += ["--retries", "1"]
                if mode == "dry-run":
                    args.append("--dry-run")
                elif source_snapshot:
                    log("preview_skipped", run_id=run_id, reason="verified_snapshot")
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
            check_args = [RCLONE, "check", source, self.destination, "--one-way"]
            if source_snapshot:
                check_args.append("--fast-list")
            self.runner.run([*check_args, *selection, *common], stderr)
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

    runner = Runner()
    old_handlers = {}
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig] = signal.signal(sig, lambda *_: runner.cancel())

    def execute(source):
        display_source = source.get("display_source", source["root"])
        if state:
            state.update(detected=True, source=display_source, model=source["model"],
                         transport=source.get("transport", "filesystem"))
        source_snapshot = source.get("transport") == "libmtp"
        result = SyncRun(state, runner=runner).execute(
            source["root"], mode, source_snapshot=source_snapshot)
        if state:
            fields = {**result, "status": "success", "completed_at": now(), "last_error": None}
            if mode == "copy":
                fields["last_success"] = now()
                fields["last_success_details"] = {
                    **result, "source": display_source, "completed_at": fields["last_success"]}
            state.update(**fields)
        return 0

    try:
        if IS_DARWIN:
            with stage_macos_device(pin["identity"], runner=runner) as source:
                return execute(source)

        try:
            source = discover(expected_identity=pin["identity"])
        except DiscoveryError as exc:
            if state:
                state.update(status="blocked", detected=False, source=None,
                             enrolled=True, last_error=str(exc), failed_at=now())
            log("discovery_blocked", error=str(exc))
            return 1
        if not source:
            if state:
                state.update(detected=False, source=None, status="waiting")
            log("device_absent")
            return 2
        return execute(source)
    except DiscoveryError as exc:
        if state:
            state.update(status="blocked", detected=False, source=None,
                         enrolled=True, last_error=str(exc), failed_at=now())
        log("discovery_blocked", error=str(exc))
        return 1
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
    enrollment = enrolled_device()
    payload = {"ready": bool(ready), "service": service, "state": state,
               "build": build, "enrollment": enrollment,
               "sync_health": sync_health(state, ready, enrolled=bool(enrollment))}
    return bool(ready), payload


def conflicting_autostarts(directory=None):
    directory = directory or (Path.home() / ".config/autostart")
    conflicts = []
    try:
        candidates = sorted(directory.glob("*.desktop"))
    except OSError:
        return conflicts
    pattern = re.compile(r"garmin_autodev_sync[.]sh|garmin-sync/garmin_sync[.]py\s+daemon",
                         re.IGNORECASE)
    for path in candidates:
        try:
            if pattern.search(path.read_text(errors="replace")):
                conflicts.append(str(path))
        except OSError:
            continue
    return conflicts


def doctor():
    checks = {}
    checks["platform"] = "darwin" if IS_DARWIN else "linux"
    checks["rclone_binary"] = Path(RCLONE).is_file()
    checks["gdrive_configured"] = False
    checks["gdrive_available"] = False
    checks["archive_policy"] = "preserve_indefinitely_manual_review"
    checks["archive_run_directories"] = None
    if checks["rclone_binary"]:
        remotes = subprocess.run([RCLONE, "listremotes"], capture_output=True, text=True)
        checks["gdrive_configured"] = remotes.returncode == 0 and "gdrive:" in remotes.stdout.splitlines()
        if checks["gdrive_configured"]:
            remote_args = ["--timeout", "15s", "--contimeout", "5s", "--retries", "1",
                           "--low-level-retries", "1"]
            try:
                probe = subprocess.run(
                    [RCLONE, "lsf", "gdrive:Garmin", "--max-depth", "1", "--dirs-only",
                     *remote_args], capture_output=True, text=True, timeout=30)
                checks["gdrive_available"] = probe.returncode == 0
                archive = subprocess.run(
                    [RCLONE, "lsf", ARCHIVE_BASE, "--max-depth", "1", "--dirs-only",
                     *remote_args], capture_output=True, text=True, timeout=30)
                if archive.returncode == 0:
                    checks["archive_run_directories"] = len(
                        [line for line in archive.stdout.splitlines() if line.strip()])
            except (OSError, subprocess.TimeoutExpired) as exc:
                checks["gdrive_error"] = str(exc)

    if IS_DARWIN:
        checks["mtp_tools"] = all(
            Path(tool).is_file() for tool in (MTP_FILETREE, MTP_FILES, MTP_CONNECT))
        checks["gio"] = "not_required"
        checks["build_metadata"] = "not_required"
        checks["conflicting_autostarts"] = []
        checks["legacy_autostart_retired"] = True
    else:
        gio = subprocess.run(["/usr/bin/python3", "-c",
                              "import gi; gi.require_version('Gio','2.0'); from gi.repository import Gio"],
                             capture_output=True, text=True)
        checks["gio"] = gio.returncode == 0
        checks["build_metadata"] = bool(build_info().get("commit"))
        checks["conflicting_autostarts"] = conflicting_autostarts()
        checks["legacy_autostart_retired"] = not checks["conflicting_autostarts"]

    sdk = subprocess.run([str(SDK_PYTHON), "-c",
                          "from importlib.metadata import version; print(version('garmin-fit-sdk'))"],
                         capture_output=True, text=True) if SDK_PYTHON.exists() else None
    checks["fit_sdk"] = bool(sdk and sdk.returncode == 0)
    checks["fit_sdk_version"] = sdk.stdout.strip() if checks["fit_sdk"] else None
    checks["inspector"] = INSPECTOR.is_file()
    checks["enrolled_device"] = bool(enrolled_device())

    required = ["rclone_binary", "gdrive_configured", "gdrive_available",
                "fit_sdk", "inspector", "enrolled_device"]
    if IS_DARWIN:
        required.append("mtp_tools")
    else:
        required.extend(["gio", "build_metadata", "legacy_autostart_retired"])
    ok = all(bool(checks[name]) for name in required)
    print(json.dumps({"ok": ok, "checks": checks}, indent=2))
    return 0 if ok else 1

def enroll(replace=False):
    existing = enrolled_device()
    if existing and not replace:
        print(json.dumps({"enrolled": True, "device": existing,
                          "message": "A device is already enrolled; use enroll --replace explicitly."},
                         indent=2))
        return 4
    if existing and replace and not IS_DARWIN:
        service = service_snapshot()
        if service.get("ActiveState") == "active":
            print("Stop garmin-sync.service before replacing the enrolled device.", file=sys.stderr)
            return 75
    try:
        if IS_DARWIN:
            with stage_macos_device(metadata_only=True) as device:
                saved = save_enrollment(device)
        else:
            device = discover()
            if not device:
                log("device_absent")
                return 2
            saved = save_enrollment(device)
    except DiscoveryError as exc:
        log("enroll_failed", error=str(exc))
        return 1
    except (CommandFailed, Cancelled, OSError, ValueError) as exc:
        log("enroll_failed", error=str(exc))
        return 1
    print(json.dumps({"enrolled": True, "device": saved}, indent=2))
    return 0


def _integer_property(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, bytes):
        return int.from_bytes(value, "little") if value else None
    if isinstance(value, str):
        try:
            return int(value, 0)
        except ValueError:
            return None
    return None


def _walk_plist(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _walk_plist(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _walk_plist(child)


def fenix_present_in_ioreg(data):
    """Return True when the enrolled Fenix USB model is visible in an IORegistry plist."""
    try:
        tree = plistlib.loads(data)
    except (plistlib.InvalidFileException, ValueError, TypeError) as exc:
        raise CommandFailed("Unable to parse macOS USB registry") from exc
    for node in _walk_plist(tree):
        vendor = _integer_property(node.get("idVendor"))
        product = _integer_property(node.get("idProduct"))
        if vendor == GARMIN_USB_VENDOR_ID and product == FENIX8_USB_PRODUCT_ID:
            return True
        if vendor == GARMIN_USB_VENDOR_ID:
            product_name = " ".join(
                str(node.get(key, "")) for key in
                ("USB Product Name", "kUSBProductString", "Product Name", "IORegistryEntryName"))
            if re.search(r"fenix\s*8", product_name, re.IGNORECASE):
                return True
    return False


def macos_launchagent_loaded():
    if not IS_DARWIN:
        return False
    result = subprocess.run(
        ["/bin/launchctl", "print", f"gui/{os.getuid()}/{MACOS_LAUNCHD_LABEL}"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    return result.returncode == 0


def macos_fenix_usb_present():
    if not IS_DARWIN:
        return False
    try:
        result = subprocess.run(
            [IOREG, "-p", "IOUSB", "-a", "-l", "-w", "0"],
            capture_output=True, timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise CommandFailed("Unable to query macOS USB registry") from exc
    if result.returncode:
        raise CommandFailed(
            "ioreg failed while querying USB devices: " +
            result.stderr.decode("utf-8", errors="replace").strip())
    return fenix_present_in_ioreg(result.stdout)


def macos_watch(state, poll_seconds=5, retry_seconds=120, presence=None, stop_event=None):
    """Sync once per Fenix USB connection; retry failures while still connected."""
    if not IS_DARWIN:
        print("watch is only supported on macOS", file=sys.stderr)
        return 64
    presence = presence or macos_fenix_usb_present
    stop_event = stop_event or threading.Event()
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            old_handlers[sig] = signal.signal(sig, lambda *_: stop_event.set())

    connected = False
    synced = False
    retry_at = 0.0
    log("watch_started", pid=os.getpid(), poll_seconds=poll_seconds)
    try:
        while not stop_event.is_set():
            try:
                present = bool(presence())
            except (CommandFailed, OSError, ValueError) as exc:
                log("usb_probe_failed", error=str(exc))
                stop_event.wait(max(poll_seconds, 15))
                continue

            if present and not connected:
                connected = True
                synced = False
                retry_at = 0.0
                log("usb_connected", vendor_id=hex(GARMIN_USB_VENDOR_ID),
                    product_id=hex(FENIX8_USB_PRODUCT_ID))
            elif not present and connected:
                connected = False
                synced = False
                retry_at = 0.0
                state.update(detected=False, source=None, status="waiting")
                log("usb_disconnected")

            if connected and not synced and time.monotonic() >= retry_at:
                try:
                    with RunLocks():
                        rc = once(state, "copy")
                except Busy as exc:
                    rc = 75
                    log("auto_sync_busy", error=str(exc))
                if rc == 0:
                    synced = True
                    retry_at = 0.0
                    log("auto_sync_completed")
                else:
                    retry_at = time.monotonic() + retry_seconds
                    log("auto_sync_retry_scheduled", returncode=rc,
                        retry_after_seconds=retry_seconds)

            stop_event.wait(poll_seconds)
    finally:
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        state.update(status="stopped", stopped_at=now())
        log("watch_stopped")
    return 0


def main(argv=None):
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=VERSION)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("daemon")
    commands.add_parser("watch")
    single = commands.add_parser("once")
    single.add_argument("--dry-run", action="store_true")
    for name in ("status", "force", "dry-run", "verify", "doctor", "readiness"):
        commands.add_parser(name)
    enrollment = commands.add_parser("enroll")
    enrollment.add_argument("--replace", action="store_true")
    args = parser.parse_args(argv)
    if args.command == "status":
        if IS_DARWIN:
            payload = {
                "platform": "darwin",
                "mode": "watch_or_manual",
                "launchagent_loaded": macos_launchagent_loaded(),
                "state": State().snapshot(),
                "enrollment": enrolled_device(),
                "rclone": RCLONE,
                "mtp_tools": [MTP_FILETREE, MTP_FILES, MTP_CONNECT],
            }
            payload["healthy"] = bool(
                payload["enrollment"] and Path(RCLONE).is_file() and
                all(Path(tool).is_file() for tool in payload["mtp_tools"]))
            payload["sync_health"] = sync_health(
                payload["state"], payload["healthy"], enrolled=bool(payload["enrollment"]))
            print(json.dumps(payload, indent=2))
            return 0 if payload["healthy"] else 1
        ready, payload = readiness()
        healthy_states = {"waiting", "connected", "success", "retry_wait", "copy", "verifying", "dry-run"}
        healthy = ready and bool(payload["enrollment"]) and payload["state"].get("status") in healthy_states
        payload["healthy"] = bool(healthy)
        print(json.dumps(payload, indent=2))
        return 0 if healthy else 1
    if args.command == "readiness":
        if IS_DARWIN:
            print(json.dumps({"ready": False, "platform": "darwin",
                              "reason": "macOS uses manual libmtp runs, not systemd readiness"}, indent=2))
            return 1
        ready, payload = readiness(os.environ.get("GARMIN_SYNC_EXPECT_COMMIT"))
        print(json.dumps(payload, indent=2))
        return 0 if ready else 1
    if args.command == "doctor":
        return doctor()
    if args.command == "enroll":
        return enroll(args.replace)
    if args.command == "watch":
        return macos_watch(State())
    if args.command in ("force", "dry-run"):
        if IS_DARWIN:
            try:
                with RunLocks():
                    return once(State(), "copy" if args.command == "force" else "dry-run")
            except Busy as exc:
                log("busy", error=str(exc))
                return 75
        sig = "USR1" if args.command == "force" else "USR2"
        log("request_sent", mode=args.command, signal=sig)
        return subprocess.call(["/usr/bin/systemctl", "--user", "kill", "--kill-who=main",
                                "--signal=" + sig, "garmin-sync.service"])
    try:
        with RunLocks():
            if args.command == "daemon":
                if IS_DARWIN:
                    print("Use the macOS 'watch' command under launchd instead of daemon.",
                          file=sys.stderr)
                    return 64
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
