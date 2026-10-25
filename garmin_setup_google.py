"""Development-stage, zero-manual-rclone Google onboarding coordinator.

This CLI flow creates a default app-owned Drive folder after explicit consent.
The public browser wizard, existing-folder Picker, publisher credential metadata,
and secure credential store still need release qualification (#39-#43).
"""
import json
from pathlib import Path

import garmin_backup_target as target
import garmin_drive_provision as drive
import garmin_google_browser as browser
import garmin_google_oauth as oauth


class SetupError(ValueError):
    """Expected onboarding failure with no token, account data or request leakage."""


def publisher_metadata(path):
    """Publisher-managed application metadata, never an end-user Cloud project."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SetupError("Publisher Google OAuth application is not configured") from exc
    if not isinstance(data, dict):
        raise SetupError("Publisher Google OAuth metadata is invalid")
    client_id = data.get("client_id")
    if (not isinstance(client_id, str) or
            not client_id.endswith(".apps.googleusercontent.com")):
        raise SetupError("Publisher Google OAuth client ID is unavailable")
    client_secret = data.get("client_secret", "")
    if not isinstance(client_secret, str):
        raise SetupError("Publisher Google OAuth metadata is invalid")
    return client_id, client_secret


def google_principal(access_token):
    data = drive._drive_json(access_token, target.ABOUT_URL)
    user = data.get("user") if isinstance(data, dict) else None
    stable_id = user.get("permissionId") if isinstance(user, dict) else None
    if not isinstance(stable_id, str) or not stable_id:
        raise SetupError("Google account identity could not be verified")
    return {"permission_id": stable_id, "email": user.get("emailAddress")}


def setup_default(*, publisher_path, rclone_binary, rclone_config,
                  profile_path=target.PROFILE_PATH,
                  confirm=input, authorize=browser.authorize_with_browser,
                  choose_folder=False):
    """Authorize Google, create app-owned folder layout, and bind after verification.

    Never substitute ambient Google credentials, replace an existing profile,
    or create folders until user has explicitly confirmed this operation.
    """
    if Path(profile_path).exists() or Path(profile_path).is_symlink():
        raise SetupError("Already configured; existing installation requires explicit rebind")
    if Path(rclone_config).exists() or Path(rclone_config).is_symlink():
        raise SetupError("Private rclone config exists; choose safe migration instead")
    client_id, client_secret = publisher_metadata(publisher_path)
    prompt = ("Connect Google, select a Drive folder, and create Backups "
              "and Archive inside it? Type CREATE: " if choose_folder else
              "Connect Google and create Garmin Sync/Backups and "
              "Garmin Sync/Archive in your Drive? Type CREATE: ")
    choice = confirm(prompt)
    if choice != "CREATE":
        raise SetupError("Setup cancelled; no Google Drive folder created")
    # No app-hosted callback, and no browser receives the resulting refresh token.
    if choose_folder:
        tokens, selected_parent_id = authorize(client_id, choose_folder=True)
    else:
        tokens = authorize(client_id)
    oauth.validate_token_response(tokens)
    account = google_principal(tokens["access_token"])
    layout = (drive.create_selected_layout(
        tokens["access_token"], selected_parent_id, confirmed=True)
        if choose_folder else drive.create_default_layout(
            tokens["access_token"], confirmed=True))
    drive.create_private_rclone_config(
        rclone_config, client_id=client_id, client_secret=client_secret,
        token_response=tokens, parent_folder_id=layout["parent_folder_id"])
    try:
        result = drive.bind_created_layout(
            rclone_binary, rclone_config, layout, account["permission_id"],
            profile_path=profile_path)
    except (target.BindingError, OSError) as exc:
        # Preserve files and the private dedicated credentials for forensic
        # repair. Never auto-retry folder creation or substitute another account.
        raise SetupError("Account/folder verification failed; no sync activated") from exc
    # Return only safe metadata; never return or log raw OAuth credentials.
    return {"installation_id": result["installation_id"],
            "account": account["email"],
            "backup_folder_id": layout["backup_folder_id"],
            "archive_folder_id": layout["archive_folder_id"],
            "ready_for_device_enrollment": True}
