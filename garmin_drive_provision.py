"""First-party Google Drive folder provisioning and private rclone configuration.

Building block for the Garmin Sync setup wizard. No client ID is bundled until
the publisher registers the production Google OAuth app. This module requires
an explicit user-confirmed default-folder creation decision from the caller.
"""
import configparser
import datetime as dt
import json
import os
from pathlib import Path
import stat
import tempfile
import urllib.error
import urllib.parse
import urllib.request

import garmin_google_oauth as oauth
import garmin_backup_target as target

DRIVE_FILES = "https://www.googleapis.com/drive/v3/files"
FOLDER_MIME = "application/vnd.google-apps.folder"


class SetupError(ValueError):
    """Never expose Google API error payloads or OAuth tokens."""


def _drive_json(token, url, body=None):
    if not isinstance(token, str) or not token:
        raise SetupError("Google authentication is required")
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        url, data=data, method="POST" if body is not None else "GET",
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/json",
                 "Content-Type": "application/json; charset=utf-8"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            return json.loads(response.read(256 * 1024))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise SetupError("Unable to provision the authorized Drive folder") from exc


def _created_folder(payload, expected_name):
    folder = payload if isinstance(payload, dict) else {}
    folder_id = folder.get("id")
    if folder.get("name") != expected_name or folder.get("mimeType") != FOLDER_MIME:
        raise SetupError("Drive did not confirm the requested folder")
    try:
        return target.valid_folder(folder_id)
    except target.BindingError as exc:
        raise SetupError("Drive returned an invalid folder identifier") from exc


def create_folder(access_token, name, parent_id=None):
    """Create exactly one app-owned folder after explicit user confirmation."""
    name = target.valid_child(name)
    if parent_id is not None:
        target.valid_folder(parent_id)
    fields = "id,name,mimeType,parents"
    payload = {"name": name, "mimeType": FOLDER_MIME}
    if parent_id:
        payload["parents"] = [parent_id]
    response = _drive_json(
        access_token, DRIVE_FILES + "?" +
        urllib.parse.urlencode({"fields": fields}), payload)
    folder_id = _created_folder(response, name)
    if parent_id and parent_id not in (response.get("parents") or []):
        raise SetupError("Created folder did not have the authorized parent")
    return folder_id


def create_default_layout(access_token, *, confirmed=False,
                          parent_name="Garmin Sync",
                          backup_name="Backups", archive_name="Archive"):
    """User opt-in only. Never delete created folders if a later step fails."""
    if confirmed is not True:
        raise SetupError("Explicit user consent is required before Drive folder creation")
    for value in (parent_name, backup_name, archive_name):
        target.valid_child(value)
    if backup_name.casefold() == archive_name.casefold():
        raise SetupError("Backup and archive folders must be distinct")
    parent_id = create_folder(access_token, parent_name)
    backup_id = create_folder(access_token, backup_name, parent_id)
    archive_id = create_folder(access_token, archive_name, parent_id)
    return {"parent_folder_id": parent_id, "backup_folder_id": backup_id,
            "archive_folder_id": archive_id, "backup_directory": backup_name,
            "archive_directory": archive_name}


def create_selected_layout(access_token, selected_parent_id, *,
                           confirmed=False, backup_name="Backups",
                           archive_name="Archive"):
    """Provision children inside a Picker-granted parent after explicit opt-in.

    Picker grants access to the chosen parent; never infer that arbitrary
    unrelated existing children become visible under drive.file.
    """
    if confirmed is not True:
        raise SetupError("Explicit confirmation required for selected Drive folder")
    parent_id = target.valid_folder(selected_parent_id)
    for value in (backup_name, archive_name):
        target.valid_child(value)
    if backup_name.casefold() == archive_name.casefold():
        raise SetupError("Backup and archive folders must differ")
    query = urllib.parse.urlencode({
        "fields": "id,mimeType,trashed,capabilities(canAddChildren)"})
    parent = _drive_json(access_token, DRIVE_FILES + "/" +
                         urllib.parse.quote(parent_id, safe="") + "?" + query)
    if (parent.get("id") != parent_id or parent.get("trashed") or
            parent.get("mimeType") != FOLDER_MIME or
            not (parent.get("capabilities") or {}).get("canAddChildren")):
        raise SetupError("Selected Drive folder is not accessible and writable")
    backup_id = create_folder(access_token, backup_name, parent_id)
    archive_id = create_folder(access_token, archive_name, parent_id)
    return {"parent_folder_id": parent_id, "backup_folder_id": backup_id,
            "archive_folder_id": archive_id, "backup_directory": backup_name,
            "archive_directory": archive_name}


def rclone_token(token_response):
    token = oauth.validate_token_response(token_response)
    expiry = (dt.datetime.now(dt.timezone.utc) +
              dt.timedelta(seconds=token["expires_in"])).isoformat()
    return {"access_token": token["access_token"],
            "refresh_token": token["refresh_token"],
            "token_type": "Bearer", "expiry": expiry}


def create_private_rclone_config(path, *, client_id, token_response,
                                 parent_folder_id, remote="garmin",
                                 client_secret=""):
    """Atomic new-file-only config; do not inherit gdrive or replace credentials.

    Google Desktop client secret, if issued, is *public* app metadata, never
    a security boundary. Token storage in this file is a transitional backend;
    public release requires OS-backed secret storage / host isolation (#35).
    """
    if not client_id.endswith(".apps.googleusercontent.com"):
        raise SetupError("Publisher Desktop OAuth client ID has not been configured")
    if not isinstance(client_secret, str):
        raise SetupError("Invalid Desktop OAuth client configuration")
    remote = target.valid_remote(remote)
    parent_folder_id = target.valid_folder(parent_folder_id)
    token = rclone_token(token_response)
    config = configparser.ConfigParser(interpolation=None)
    config[remote] = {
        "type": "drive", "scope": "drive.file",
        "client_id": client_id, "root_folder_id": parent_folder_id,
        "token": json.dumps(token, separators=(",", ":")),
    }
    if client_secret:
        config[remote]["client_secret"] = client_secret
    path = Path(path).expanduser().absolute()
    if path.exists() or path.is_symlink():
        raise SetupError("Dedicated rclone config already exists; explicit rebind required")
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    if path.parent.is_symlink() or path.parent.stat().st_mode & 0o077:
        raise SetupError("Private configuration directory permissions required")
    fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            config.write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise SetupError("OAuth credentials were not written privately")
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    return path


def bind_created_layout(rclone_binary, config_path, layout,
                        approved_google_permission_id, *,
                        remote="garmin", profile_path=target.PROFILE_PATH):
    """Verify actual principal and folder IDs, then record installation binding."""
    return target.bind_profile(
        rclone_binary, config_path, remote, layout["backup_directory"],
        layout["archive_directory"], approved_google_permission_id,
        layout["parent_folder_id"], layout["backup_folder_id"],
        layout["archive_folder_id"], profile_path)
