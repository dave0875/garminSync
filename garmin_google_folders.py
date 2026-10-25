"""Explicitly approved Google Drive folder provisioning for new installations.

No writes happen on import or during a read-only inspection. Called only after
Google consent and user confirmation; never used for legacy GORF migration.
"""
from dataclasses import dataclass
import json
import secrets
import urllib.error
import urllib.parse
import urllib.request

from garmin_backup_target import DRIVE_DIRECTORY, valid_folder


class FolderProvisionError(ValueError):
    """Cannot safely provision a consumer Drive destination."""


@dataclass(frozen=True)
class ProvisionedFolders:
    """The app-owned parent is the rclone remote root for both child paths."""
    parent_folder_id: str
    backup_folder_id: str
    archive_folder_id: str
    backup_directory: str
    archive_directory: str


def _post_folder(access_token, parent_id, name, http=None):
    if not isinstance(access_token, str) or not access_token:
        raise FolderProvisionError("Google access token missing")
    if not isinstance(name, str) or not name or "/" in name or len(name) > 150:
        raise FolderProvisionError("Unsafe Drive folder name")
    if parent_id != "root":
        valid_folder(parent_id)
    body = json.dumps({"name": name, "mimeType": DRIVE_DIRECTORY,
                       "parents": [parent_id]}).encode("utf-8")
    request = urllib.request.Request(
        "https://www.googleapis.com/drive/v3/files?" +
        urllib.parse.urlencode({"fields": "id,name,mimeType,parents"}),
        data=body, method="POST",
        headers={"Authorization": "Bearer " + access_token,
                 "Content-Type": "application/json",
                 "Accept": "application/json"})
    call = http or urllib.request.urlopen
    try:
        with call(request, timeout=15) as response:
            result = json.loads(response.read(128 * 1024))
    except (OSError, urllib.error.URLError, ValueError) as exc:
        raise FolderProvisionError("Drive folder creation did not complete") from exc
    if (not isinstance(result, dict) or result.get("name") != name or
            result.get("mimeType") != DRIVE_DIRECTORY or
            parent_id not in (result.get("parents") or [])):
        raise FolderProvisionError("Google returned unexpected folder metadata")
    try:
        folder_id = valid_folder(result.get("id"))
    except (ValueError, TypeError) as exc:
        raise FolderProvisionError("Google did not return a valid folder ID") from exc
    return folder_id


def create_consumer_destination(access_token, *, user_confirmed,
                                selected_parent_id=None, http=None):
    """Create a new app-owned container + Backups and Archive child folders.

    If a parent was selected using Picker, pass its authenticated ID. The
    caller MUST verify Picker/drive.file access before calling; this module
    never assumes a folder-name string confers permission.

    No automatic deletes/rollback on partial failures: a caller can show an
    actionable error and preserve user data until an explicit recovery flow.
    """
    if user_confirmed is not True:
        raise FolderProvisionError("User must confirm the chosen Drive location")
    if selected_parent_id is not None:
        valid_folder(selected_parent_id)
    parent_id = selected_parent_id or "root"
    # Unique app container avoids a collision with user-owned existing folders
    # that may be invisible to the narrower drive.file scope.
    folder_name = "Garmin Sync " + secrets.token_hex(5)
    container = _post_folder(access_token, parent_id, folder_name, http=http)
    backup = _post_folder(access_token, container, "Backups", http=http)
    archive = _post_folder(access_token, container, "Archive", http=http)
    if len({container, backup, archive}) != 3:
        raise FolderProvisionError("Google Drive returned overlapping folder IDs")
    return ProvisionedFolders(container, backup, archive, "Backups", "Archive")
