"""Per-installation Google Drive account and same-remote folder bindings.

No cloud transfer is authorized before device identity + a private installation
profile + Google principal, parent, backup, and archive folder checks pass.
Only explicitly enrolled OAuth Google Drive remotes are supported.
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
DRIVE_DIRECTORY = "application/vnd.google-apps.folder"
DRIVE_URL = "https://www.googleapis.com/drive/v3"
ABOUT_URL = DRIVE_URL + "/about?fields=user(permissionId,emailAddress)"


class BindingError(ValueError):
    """Untrusted or unavailable installation binding; refuse all cloud writes."""


def clean_rclone_environment():
    """Do not let ambient rclone remote/config environment override our pin."""
    return {key: value for key, value in os.environ.items()
            if not key.upper().startswith("RCLONE_")}


def private_file(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise BindingError("Configuration must be an absolute non-symlink path")
    try:
        st = path.stat()
    except OSError as exc:
        raise BindingError("Required private configuration is unavailable") from exc
    if not stat.S_ISREG(st.st_mode) or st.st_uid != os.getuid():
        raise BindingError("Configuration must be a regular file owned by this user")
    if st.st_mode & 0o077:
        raise BindingError("Configuration is not private; chmod 600 required")
    return path


def valid_remote(value):
    if not isinstance(value, str) or not REMOTE_NAME.fullmatch(value):
        raise BindingError("Invalid rclone remote alias")
    return value


def valid_folder(value):
    if not isinstance(value, str) or not FOLDER_ID.fullmatch(value):
        raise BindingError("An explicit non-root Google Drive folder ID is required")
    return value


def valid_child(value):
    if (not isinstance(value, str) or not value or len(value) > 150 or
            value in (".", "..") or value != value.strip() or
            any(char in value for char in "/\\:\x00\r\n") or
            any(ord(char) < 32 for char in value)):
        raise BindingError("Folder name must be a safe single path component")
    return value


def validated_profile(profile):
    if not isinstance(profile, dict) or profile.get("schema_version") != 2:
        raise BindingError("Unsupported/unmigrated installation profile version")
    try:
        uuid.UUID(profile["installation_id"])
        if not isinstance(profile["google_permission_id"], str) or not profile["google_permission_id"]:
            raise BindingError("Missing approved Google account identity")
        valid_remote(profile["remote"])
        for field in ("parent_folder_id", "backup_folder_id", "archive_folder_id"):
            valid_folder(profile[field])
        if len({profile["parent_folder_id"], profile["backup_folder_id"],
                profile["archive_folder_id"]}) != 3:
            raise BindingError("Parent, backup and archive folder IDs must be distinct")
        names = (valid_child(profile["backup_directory"]),
                 valid_child(profile["archive_directory"]))
        if names[0].casefold() == names[1].casefold():
            raise BindingError("Backup and archive directories must differ")
        profile["rclone_config"] = str(private_file(Path(profile["rclone_config"])))
    except (KeyError, TypeError, AttributeError, ValueError) as exc:
        raise BindingError("Incomplete or invalid installation profile") from exc
    return profile


def load_profile(path=PROFILE_PATH):
    path = private_file(path)
    try:
        profile = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise BindingError("Invalid installation profile JSON") from exc
    return validated_profile(profile)


def remote_section(rclone_config, remote):
    valid_remote(remote)
    path = private_file(rclone_config)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BindingError("Invalid dedicated rclone configuration") from exc
    if not parser.has_section(remote) or parser.get(remote, "type", fallback="") != "drive":
        raise BindingError("Approved rclone remote must use the Google Drive backend")
    if any(parser.has_option(remote, key) for key in (
            "service_account_file", "service_account_credentials",
            "impersonate", "team_drive")):
        raise BindingError("Service accounts, impersonation and shared drives unsupported")
    return {
        "folder_id": valid_folder(parser.get(remote, "root_folder_id", fallback="")),
        "token": parser.get(remote, "token", fallback=""),
    }


def _request_json(url, access_token):
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + access_token,
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read(256 * 1024))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise BindingError("Google account/folder verification unavailable") from exc


def _access_token(raw):
    try:
        token = json.loads(raw)["access_token"]
        if isinstance(token, str) and token:
            return token
    except (ValueError, KeyError, TypeError):
        pass
    raise BindingError("Google OAuth token missing; reconnect approved account")


def _file_details(folder_id, token):
    fields = "id,name,mimeType,parents,trashed,capabilities(canAddChildren)"
    return _request_json(DRIVE_URL + "/files/" + urllib.parse.quote(folder_id, safe="") +
                         "?" + urllib.parse.urlencode({
                             "fields": fields, "supportsAllDrives": "false"}), token)


def _unique_child(parent, name, folder_id, token):
    # Drive supports duplicate names. Never let rclone choose the wrong sibling.
    q = ("'" + parent.replace("\\", "\\\\").replace("'", "\\'") +
         "' in parents and name = '" +
         name.replace("\\", "\\\\").replace("'", "\\'") +
         "' and trashed = false")
    result = _request_json(DRIVE_URL + "/files?" + urllib.parse.urlencode({
        "q": q, "fields": "nextPageToken,files(id,name,mimeType,parents)",
        "pageSize": "100", "supportsAllDrives": "false"}), token)
    entries = result.get("files")
    if (not isinstance(entries, list) or result.get("nextPageToken") or
            len(entries) != 1 or entries[0].get("id") != folder_id):
        raise BindingError("Ambiguous, missing or replaced Drive folder name")


def inspect_remote(rclone_binary, config_path, remote, backup_dir, archive_dir,
                   backup_id, archive_id):
    """Read-only authorization/metadata lookup; rclone may refresh local token."""
    remote = valid_remote(remote)
    backup_dir, archive_dir = valid_child(backup_dir), valid_child(archive_dir)
    result = subprocess.run(
        [rclone_binary, "lsf", remote + ":", "--max-depth", "1", "--dirs-only",
         "--config", str(private_file(config_path)), "--timeout", "15s",
         "--retries", "1"], env=clean_rclone_environment(),
        capture_output=True, check=False, timeout=45)
    if result.returncode:
        raise BindingError("Approved Google Drive remote could not be inspected")

    section = remote_section(config_path, remote)
    token = _access_token(section["token"])
    about = _request_json(ABOUT_URL, token)
    user = about.get("user") or {}
    account = user.get("permissionId")
    if not isinstance(account, str) or not account:
        raise BindingError("Stable Google account identity unavailable")

    parent = section["folder_id"]
    root = _file_details(parent, token)
    if (root.get("id") != parent or root.get("trashed") or
            root.get("mimeType") != DRIVE_DIRECTORY or
            not (root.get("capabilities") or {}).get("canAddChildren")):
        raise BindingError("Approved parent folder is unavailable or not writable")

    for folder_id, name in ((valid_folder(backup_id), backup_dir),
                            (valid_folder(archive_id), archive_dir)):
        details = _file_details(folder_id, token)
        if (details.get("id") != folder_id or details.get("name") != name or
                details.get("trashed") or details.get("mimeType") != DRIVE_DIRECTORY or
                parent not in (details.get("parents") or []) or
                not (details.get("capabilities") or {}).get("canAddChildren")):
            raise BindingError("Selected backup/archive folder identity or parent changed")
        _unique_child(parent, name, folder_id, token)
    return {"permission_id": account, "email": user.get("emailAddress"),
            "parent_folder_id": parent,
            "backup_folder_id": backup_id, "archive_folder_id": archive_id}


def verify_binding(profile, observed):
    for approved, observed_field in (
            ("google_permission_id", "permission_id"),
            ("parent_folder_id", "parent_folder_id"),
            ("backup_folder_id", "backup_folder_id"),
            ("archive_folder_id", "archive_folder_id")):
        if profile[approved] != observed.get(observed_field):
            raise BindingError("Google account or pinned Drive folder identity differs")


@contextlib.contextmanager
def prepared_target(rclone_binary, runtime, path=PROFILE_PATH):
    """Use a single private same-root rclone config for preflight, archive and copy."""
    profile = load_profile(path)
    source = Path(profile["rclone_config"])
    runtime = Path(runtime)
    if runtime.is_symlink():
        raise BindingError("Private runtime cannot be a symlink")
    runtime.mkdir(mode=0o700, parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="drive-bound-", dir=runtime) as temporary:
        snapshot = Path(temporary) / "rclone.conf"
        shutil.copyfile(source, snapshot)
        snapshot.chmod(0o600)
        first = remote_section(snapshot, profile["remote"])
        if first["folder_id"] != profile["parent_folder_id"]:
            raise BindingError("Remote parent folder differs from approved ID")
        observed = inspect_remote(
            rclone_binary, snapshot, profile["remote"],
            profile["backup_directory"], profile["archive_directory"],
            profile["backup_folder_id"], profile["archive_folder_id"])
        verify_binding(profile, observed)
        after = remote_section(snapshot, profile["remote"])
        if after["folder_id"] != profile["parent_folder_id"]:
            raise BindingError("Remote changed during verification")
        # rclone copy --backup-dir only supports directories on the *same* remote.
        remote_prefix = profile["remote"] + ":"
        yield {
            "destination": remote_prefix + profile["backup_directory"],
            "archive_base": remote_prefix + profile["archive_directory"],
            "rclone_options": ["--config", str(snapshot)],
            "installation_id": profile["installation_id"],
            "google_permission_id": profile["google_permission_id"],
        }


def bind_profile(rclone_binary, rclone_config, remote, backup_dir, archive_dir,
                 approved_permission_id, approved_parent_id, approved_backup_id,
                 approved_archive_id, path=PROFILE_PATH):
    """Explicit one-time binding, never overwrite or silently rebind."""
    if Path(path).exists() or Path(path).is_symlink():
        raise BindingError("Installation already bound; deliberate rebind required")
    config_path = str(private_file(Path(rclone_config).expanduser().absolute()))
    profile = validated_profile({
        "schema_version": 2, "installation_id": str(uuid.uuid4()),
        "rclone_config": config_path, "remote": valid_remote(remote),
        "backup_directory": valid_child(backup_dir),
        "archive_directory": valid_child(archive_dir),
        "google_permission_id": approved_permission_id,
        "parent_folder_id": approved_parent_id,
        "backup_folder_id": approved_backup_id,
        "archive_folder_id": approved_archive_id,
    })
    with tempfile.TemporaryDirectory(prefix="garmin-bind-") as temporary:
        snapshot = Path(temporary) / "rclone.conf"
        shutil.copyfile(config_path, snapshot)
        snapshot.chmod(0o600)
        observed = inspect_remote(
            rclone_binary, snapshot, profile["remote"], profile["backup_directory"],
            profile["archive_directory"], profile["backup_folder_id"],
            profile["archive_folder_id"])
    verify_binding(profile, observed)
    target = Path(path)
    target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if target.parent.is_symlink() or target.parent.stat().st_mode & 0o077:
        raise BindingError("Private installation directory permissions required")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(profile, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"installation_id": profile["installation_id"],
            "principal": observed.get("email"), "remote": profile["remote"],
            "parent_folder_id": profile["parent_folder_id"],
            "backup_folder_id": profile["backup_folder_id"],
            "archive_folder_id": profile["archive_folder_id"]}
