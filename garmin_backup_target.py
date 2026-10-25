"""Private per-installation Google Drive binding using one parent-rooted remote.

Both rclone copy destination and --backup-dir MUST be on the same remote.
No Drive writes are permitted until account, parent, child folder IDs,
names, relationships and rclone path resolution pass read-only checks.
"""

import configparser
import contextlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import uuid

PROFILE_PATH = Path.home() / ".config/garmin-sync/installation.json"
REMOTE_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,63}$")
FOLDER_ID = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
CHILD_NAME = re.compile(r"^[^/\\\x00-\x1f]{1,150}$")
DRIVE_DIRECTORY = "application/vnd.google-apps.folder"
ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(permissionId,emailAddress)"


class BindingError(ValueError):
    """Destination untrusted. Never fall back to a different remote or folder."""


def clean_rclone_environment():
    return {k: v for k, v in os.environ.items()
            if not k.upper().startswith("RCLONE_")}


def private_file(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise BindingError("Configuration must be an absolute, non-symlink path")
    try:
        st = path.stat()
    except OSError as exc:
        raise BindingError("Required private configuration file is unavailable") from exc
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise BindingError("Configuration must be a regular file owned by this user")
    if st.st_mode & 0o077:
        raise BindingError("Configuration is not private (chmod 600 required)")
    return path


def valid_remote(value):
    if not isinstance(value, str) or not REMOTE_NAME.fullmatch(value):
        raise BindingError("Invalid rclone remote alias")
    return value


def valid_folder(value):
    if not isinstance(value, str) or not FOLDER_ID.fullmatch(value):
        raise BindingError("Invalid Google Drive folder ID")
    return value


def valid_child(value):
    if (not isinstance(value, str) or not CHILD_NAME.fullmatch(value)
            or value in (".", "..") or value != value.strip()):
        raise BindingError("Backup/archive child must be one safe directory name")
    return value


def validate_profile(config):
    if not isinstance(config, dict) or config.get("schema_version") != 2:
        raise BindingError("Installation must use the approved same-remote v2 schema")
    try:
        uuid.UUID(config["installation_id"])
        valid_remote(config["remote"])
        valid_folder(config["parent_folder_id"])
        valid_folder(config["backup_folder_id"])
        valid_folder(config["archive_folder_id"])
        valid_child(config["backup_path"])
        valid_child(config["archive_path"])
        if config["backup_path"].casefold() == config["archive_path"].casefold():
            raise BindingError("Backup and archive child paths must differ")
        if len({config["parent_folder_id"], config["backup_folder_id"],
                config["archive_folder_id"]}) != 3:
            raise BindingError("Parent, backup and archive IDs must be distinct")
        if not isinstance(config["google_permission_id"], str) or not config["google_permission_id"]:
            raise BindingError("Google account identity missing")
        config["rclone_config"] = str(private_file(Path(config["rclone_config"])))
    except (KeyError, TypeError, ValueError, AttributeError) as exc:
        raise BindingError("Invalid or incomplete installation binding") from exc
    return config


def load_profile(path=PROFILE_PATH):
    path = private_file(path)
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BindingError("Installation profile is not valid JSON") from exc
    return validate_profile(config)


def remote_section(rclone_config, remote):
    path = private_file(rclone_config)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        with path.open(encoding="utf-8") as f:
            parser.read_file(f)
    except (OSError, configparser.Error) as exc:
        raise BindingError("Invalid dedicated rclone configuration") from exc
    remote = valid_remote(remote)
    if not parser.has_section(remote) or parser.get(remote, "type", fallback="") != "drive":
        raise BindingError("Bound remote must use Google Drive")
    for forbidden in ("service_account_file", "service_account_credentials",
                      "impersonate", "team_drive", "shared_with_me"):
        if parser.has_option(remote, forbidden):
            raise BindingError("Unsupported Google Drive authentication mode")
    return {"parent_id": valid_folder(parser.get(remote, "root_folder_id", fallback="")),
            "token": parser.get(remote, "token", fallback="")}


def _request_json(url, token):
    request = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + token, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read(256 * 1024))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BindingError("Google identity/folder verification unavailable") from exc


def _access_token(raw):
    try:
        value = json.loads(raw)["access_token"]
        if isinstance(value, str) and value:
            return value
    except (ValueError, KeyError, TypeError):
        pass
    raise BindingError("OAuth token missing; reauthorize this installation")


def _folder_info(folder_id, token):
    url = ("https://www.googleapis.com/drive/v3/files/" +
           urllib.parse.quote(valid_folder(folder_id), safe="") +
           "?fields=id,name,parents,mimeType,trashed,capabilities(canAddChildren)&supportsAllDrives=true")
    result = _request_json(url, token)
    if (result.get("id") != folder_id or result.get("trashed")
            or result.get("mimeType") != DRIVE_DIRECTORY):
        raise BindingError("Pinned Drive folder does not exist or is not a folder")
    return result


def inspect_target(rclone_binary, config_path, remote, backup_path, archive_path):
    """Read-only authorization + rclone path checks on the same remote."""
    remote = valid_remote(remote)
    backup_path, archive_path = valid_child(backup_path), valid_child(archive_path)
    if backup_path.casefold() == archive_path.casefold():
        raise BindingError("Backup and archive paths overlap")
    section = remote_section(config_path, remote)
    # lsjson refreshes OAuth token if necessary, then returns Drive object IDs.
    # Never create directories to check them.
    command = [rclone_binary, "lsjson", remote + ":", "--dirs-only",
               "--max-depth", "1", "--config", str(config_path),
               "--timeout", "15s", "--retries", "1"]
    try:
        result = subprocess.run(command, env=clean_rclone_environment(),
                                capture_output=True, timeout=45, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BindingError("rclone remote inspection unavailable") from exc
    if result.returncode:
        raise BindingError("Unable to inspect the approved Drive parent")
    try:
        entries = json.loads(result.stdout)
        if not isinstance(entries, list):
            raise ValueError("rclone lsjson did not list directories")
    except ValueError as exc:
        raise BindingError("Invalid rclone Drive directory listing") from exc
    child_ids = {}
    for name in (backup_path, archive_path):
        exact = [x for x in entries if x.get("Path") == name and x.get("IsDir")]
        if len(exact) != 1 or not exact[0].get("ID"):
            raise BindingError("Cannot prove exact rclone child folder ID")
        child_ids[name] = valid_folder(exact[0]["ID"])
    refreshed = remote_section(config_path, remote)
    if refreshed["parent_id"] != section["parent_id"]:
        raise BindingError("rclone Drive root changed during OAuth refresh")
    token = _access_token(refreshed["token"])
    about = _request_json(ABOUT_URL, token)
    user = about.get("user") or {}
    principal = user.get("permissionId")
    if not isinstance(principal, str) or not principal:
        raise BindingError("Stable Google account permission ID unavailable")
    parent = _folder_info(section["parent_id"], token)
    backup = _folder_info(child_ids[backup_path], token)
    archive = _folder_info(child_ids[archive_path], token)
    if not (parent.get("capabilities") or {}).get("canAddChildren"):
        raise BindingError("Chosen parent is not writable")
    for child, expected in ((backup, backup_path), (archive, archive_path)):
        if (child.get("name") != expected or
                parent["id"] not in (child.get("parents") or []) or
                not (child.get("capabilities") or {}).get("canAddChildren")):
            raise BindingError("Backup/archive child is not an approved writable sibling")
    return {"permission_id": principal, "email": user.get("emailAddress"),
            "parent_folder_id": parent["id"],
            "backup_folder_id": backup["id"], "archive_folder_id": archive["id"],
            "backup_path": backup_path, "archive_path": archive_path}


def verify_binding(config, inspected):
    for key in ("google_permission_id", "parent_folder_id",
                "backup_folder_id", "archive_folder_id"):
        observed = "permission_id" if key == "google_permission_id" else key
        if config[key] != inspected[observed]:
            raise BindingError("Google account or pinned destination ID mismatch")
    for key in ("backup_path", "archive_path"):
        if config[key] != inspected[key]:
            raise BindingError("Drive child folder path changed")


@contextlib.contextmanager
def prepared_target(rclone_binary, runtime, path=PROFILE_PATH):
    """Keep one private config snapshot for preflight, copy and archive."""
    config = load_profile(path)
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    if runtime.is_symlink() or runtime.stat().st_mode & 0o077:
        raise BindingError("Runtime directory must be private")
    with tempfile.TemporaryDirectory(prefix="drive-bound-", dir=runtime) as folder:
        snapshot = Path(folder) / "rclone.conf"
        shutil.copyfile(config["rclone_config"], snapshot)
        snapshot.chmod(0o600)
        pre = remote_section(snapshot, config["remote"])
        if pre["parent_id"] != config["parent_folder_id"]:
            raise BindingError("rclone root folder differs from approved parent")
        observed = inspect_target(rclone_binary, snapshot, config["remote"],
                                  config["backup_path"], config["archive_path"])
        verify_binding(config, observed)
        post = remote_section(snapshot, config["remote"])
        if post["parent_id"] != config["parent_folder_id"]:
            raise BindingError("rclone remote changed during verification")
        prefix = config["remote"] + ":"
        yield {"destination": prefix + config["backup_path"],
               "archive_base": prefix + config["archive_path"],
               "rclone_options": ["--config", str(snapshot)],
               "installation_id": config["installation_id"],
               "google_permission_id": config["google_permission_id"]}


def bind_profile(rclone_binary, rclone_config, remote, backup_path, archive_path,
                 approved_permission_id, approved_parent_folder_id,
                 approved_backup_folder_id, approved_archive_folder_id,
                 path=PROFILE_PATH):
    """Persist an installation only after explicit approved identity match."""
    target = Path(path)
    if target.exists() or target.is_symlink():
        raise BindingError("Installation already bound; explicit rebind required")
    rclone_config = str(private_file(Path(rclone_config).expanduser().absolute()))
    candidate = {
        "schema_version": 2, "installation_id": str(uuid.uuid4()),
        "rclone_config": rclone_config, "remote": valid_remote(remote),
        "google_permission_id": approved_permission_id,
        "parent_folder_id": valid_folder(approved_parent_folder_id),
        "backup_folder_id": valid_folder(approved_backup_folder_id),
        "archive_folder_id": valid_folder(approved_archive_folder_id),
        "backup_path": valid_child(backup_path), "archive_path": valid_child(archive_path)}
    validate_profile(candidate)
    with tempfile.TemporaryDirectory(prefix="garmin-bind-") as dirname:
        snapshot = Path(dirname) / "rclone.conf"
        shutil.copyfile(rclone_config, snapshot)
        snapshot.chmod(0o600)
        observed = inspect_target(rclone_binary, snapshot, candidate["remote"],
                                  candidate["backup_path"], candidate["archive_path"])
        verify_binding(candidate, observed)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink() or target.parent.stat().st_mode & 0o077:
        raise BindingError("Installation directory must be private")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(candidate, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"installation_id": candidate["installation_id"],
            "principal": observed.get("email"),
            "parent_folder_id": candidate["parent_folder_id"],
            "backup_folder_id": candidate["backup_folder_id"],
            "archive_folder_id": candidate["archive_folder_id"]}
