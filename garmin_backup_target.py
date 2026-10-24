"""Fail-closed, installation-scoped Google Drive destination binding.

This module deliberately supports Google Drive OAuth remotes only. Each remote
must be rooted at the already existing, explicitly approved Drive folder ID;
a textual folder path is *never* used for account or destination authorization.
No credentials or installation profiles are stored in the source repository.
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
ABOUT_URL = "https://www.googleapis.com/drive/v3/about?fields=user(permissionId,emailAddress)"


class BindingError(ValueError):
    """Missing/unauthorized installation identity. Never fall back to a remote."""


def clean_rclone_environment():
    # A user- or job-provided RCLONE_CONFIG_* override would defeat the pin.
    return {k: v for k, v in os.environ.items() if not k.upper().startswith("RCLONE_")}


def private_file(path):
    path = Path(path)
    if not path.is_absolute() or path.is_symlink():
        raise BindingError("Configuration must be an absolute, non-symlink path")
    try:
        details = path.stat()
    except OSError as exc:
        raise BindingError("Required private configuration file is unavailable") from exc
    if not stat.S_ISREG(details.st_mode) or details.st_uid != os.getuid():
        raise BindingError("Configuration must be a regular file owned by this user")
    if details.st_mode & 0o077:
        raise BindingError("Configuration is not private (chmod 600 required)")
    return path


def _valid_remote(remote):
    if not isinstance(remote, str) or not REMOTE_NAME.fullmatch(remote):
        raise BindingError("Remote name must be a simple rclone name without a path")
    return remote


def _valid_folder(folder):
    if not isinstance(folder, str) or not FOLDER_ID.fullmatch(folder):
        raise BindingError("Google folder ID must be an explicit non-root folder ID")
    return folder


def load_profile(path=PROFILE_PATH):
    path = private_file(path)
    try:
        config = json.loads(path.read_text())
    except (OSError, ValueError) as exc:
        raise BindingError("Installation profile is not valid JSON") from exc
    if not isinstance(config, dict) or config.get("schema_version") != 1:
        raise BindingError("Unsupported or absent installation profile version")
    try:
        uuid.UUID(config["installation_id"])
        config["google_permission_id"] = str(config["google_permission_id"])
        if not config["google_permission_id"].strip():
            raise ValueError("missing Google account identity")
        backup = _valid_remote(config["backup_remote"])
        archive = _valid_remote(config["archive_remote"])
        if backup == archive:
            raise BindingError("Backup and archive must use distinct remotes")
        first = _valid_folder(config["backup_folder_id"])
        second = _valid_folder(config["archive_folder_id"])
        if first == second:
            raise BindingError("Backup and archive cannot be the same Drive folder")
        config["rclone_config"] = str(private_file(Path(config["rclone_config"])))
    except (KeyError, ValueError, TypeError, AttributeError) as exc:
        raise BindingError("Incomplete or invalid installation identity binding") from exc
    return config


def remote_sections(rclone_config, backup_remote, archive_remote):
    path = private_file(rclone_config)
    parser = configparser.ConfigParser(interpolation=None, strict=True)
    try:
        with path.open(encoding="utf-8") as handle:
            parser.read_file(handle)
    except (OSError, configparser.Error) as exc:
        raise BindingError("Invalid dedicated rclone configuration") from exc
    result = {}
    for name in (backup_remote, archive_remote):
        _valid_remote(name)
        if not parser.has_section(name) or parser.get(name, "type", fallback="") != "drive":
            raise BindingError("Both configured rclone remotes must use Google Drive")
        if (parser.has_option(name, "service_account_file") or
                parser.has_option(name, "service_account_credentials") or
                parser.has_option(name, "impersonate") or
                parser.has_option(name, "team_drive")):
            raise BindingError("Service accounts, impersonation and shared drives are not supported by this binding version")
        result[name] = {
            "folder_id": _valid_folder(parser.get(name, "root_folder_id", fallback="")),
            "token": parser.get(name, "token", fallback=""),
        }
    return result


def _request_json(url, token):
    request = urllib.request.Request(
        url, headers={"Authorization": "Bearer " + token,
                      "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.loads(response.read(256 * 1024))
    except (urllib.error.URLError, ValueError, OSError) as exc:
        # Don't emit HTTP error payloads, credential-bearing URLs or tokens.
        raise BindingError("Google identity/folder verification unavailable") from exc


def _access_token(raw):
    try:
        result = json.loads(raw)
        value = result["access_token"]
        if isinstance(value, str) and value:
            return value
    except (ValueError, KeyError, TypeError):
        pass
    raise BindingError("OAuth access token missing; reconnect the dedicated remote")


def inspect_remotes(rclone_binary, config_path, backup_remote, archive_remote):
    """Read-only remote inspection. rclone may refresh local OAuth tokens."""
    for name in (backup_remote, archive_remote):
        result = subprocess.run(
            [rclone_binary, "lsf", name + ":", "--max-depth", "1", "--dirs-only",
             "--config", str(config_path), "--timeout", "15s", "--retries", "1"],
            env=clean_rclone_environment(), capture_output=True, timeout=45, check=False)
        if result.returncode:
            raise BindingError("Google remote could not be accessed for identity verification")
    sections = remote_sections(config_path, backup_remote, archive_remote)
    out = {}
    for name, section in sections.items():
        token = _access_token(section["token"])
        about = _request_json(ABOUT_URL, token)
        user = about.get("user") or {}
        principal = user.get("permissionId")
        if not isinstance(principal, str) or not principal:
            raise BindingError("Google account stable permission ID is unavailable")
        folder = section["folder_id"]
        url = ("https://www.googleapis.com/drive/v3/files/" +
               urllib.parse.quote(folder, safe="") +
               "?fields=id,mimeType,trashed,capabilities(canAddChildren)&supportsAllDrives=true")
        info = _request_json(url, token)
        if (info.get("id") != folder or info.get("trashed") or
                info.get("mimeType") != DRIVE_DIRECTORY or
                not (info.get("capabilities") or {}).get("canAddChildren")):
            raise BindingError("Pinned Google Drive destination is not a writable folder")
        out[name] = {"permission_id": principal, "email": user.get("emailAddress"),
                     "folder_id": folder}
    return out


def verify_binding(config, inspected):
    if inspected[config["backup_remote"]]["permission_id"] != config["google_permission_id"]:
        raise BindingError("Google account differs from explicitly approved account")
    if inspected[config["archive_remote"]]["permission_id"] != config["google_permission_id"]:
        raise BindingError("Archive Google account differs from explicitly approved account")
    if inspected[config["backup_remote"]]["folder_id"] != config["backup_folder_id"]:
        raise BindingError("Google backup folder identity changed")
    if inspected[config["archive_remote"]]["folder_id"] != config["archive_folder_id"]:
        raise BindingError("Google archive folder identity changed")


@contextlib.contextmanager
def prepared_target(rclone_binary, runtime, path=PROFILE_PATH):
    """Use one private credential snapshot for preflight, copy, archive and check."""
    config = load_profile(path)
    source = Path(config["rclone_config"])
    runtime = Path(runtime)
    runtime.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="drive-bound-", dir=runtime) as folder:
        snapshot = Path(folder) / "rclone.conf"
        shutil.copyfile(source, snapshot)
        snapshot.chmod(0o600)
        # Verify configuration *before* and *after* token refresh to resist
        # remotes being redefined during the read-only preflight.
        first = remote_sections(snapshot, config["backup_remote"], config["archive_remote"])
        for remote, field in ((config["backup_remote"], "backup_folder_id"),
                              (config["archive_remote"], "archive_folder_id")):
            if first[remote]["folder_id"] != config[field]:
                raise BindingError("rclone root_folder_id differs from approved Drive folder ID")
        checked = inspect_remotes(rclone_binary, snapshot,
                                  config["backup_remote"], config["archive_remote"])
        verify_binding(config, checked)
        second = remote_sections(snapshot, config["backup_remote"], config["archive_remote"])
        for remote, field in ((config["backup_remote"], "backup_folder_id"),
                              (config["archive_remote"], "archive_folder_id")):
            if second[remote]["folder_id"] != config[field]:
                raise BindingError("rclone remote changed during preflight")
        yield {
            "destination": config["backup_remote"] + ":",
            "archive_base": config["archive_remote"] + ":",
            "rclone_options": ["--config", str(snapshot)],
            "installation_id": config["installation_id"],
            "google_permission_id": config["google_permission_id"],
        }


def bind_profile(rclone_binary, rclone_config, backup_remote, archive_remote,
                 approved_permission_id, approved_backup_folder_id,
                 approved_archive_folder_id, path=PROFILE_PATH):
    """Explicit enrollment only. Never silently update an existing binding."""
    if Path(path).exists() or Path(path).is_symlink():
        raise BindingError("Installation already bound; explicit migration/rebind required")
    backup_remote = _valid_remote(backup_remote)
    archive_remote = _valid_remote(archive_remote)
    if backup_remote == archive_remote:
        raise BindingError("Distinct backup/archive remotes are required")
    candidate = {
        "schema_version": 1,
        "installation_id": str(uuid.uuid4()),
        "rclone_config": str(private_file(Path(rclone_config).expanduser().absolute())),
        "backup_remote": backup_remote,
        "archive_remote": archive_remote,
        "google_permission_id": approved_permission_id,
        "backup_folder_id": _valid_folder(approved_backup_folder_id),
        "archive_folder_id": _valid_folder(approved_archive_folder_id),
    }
    remote_sections(candidate["rclone_config"], backup_remote, archive_remote)
    with tempfile.TemporaryDirectory(prefix="garmin-bind-") as folder:
        snapshot = Path(folder) / "config"
        shutil.copyfile(candidate["rclone_config"], snapshot)
        snapshot.chmod(0o600)
        verified = inspect_remotes(rclone_binary, snapshot, backup_remote, archive_remote)
    verify_binding(candidate, verified)
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.parent.is_symlink() or target.parent.stat().st_mode & 0o077:
        raise BindingError("Installation configuration directory must be private")
    fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(candidate, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return {"installation_id": candidate["installation_id"],
            "principal": verified[backup_remote]["email"],
            "backup_folder_id": candidate["backup_folder_id"],
            "archive_folder_id": candidate["archive_folder_id"]}
