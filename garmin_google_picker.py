"""Google Picker for desktop apps: explicit single-folder OAuth selection.

Google's desktop Picker uses the *same* native OAuth flow with
trigger_onepick=true and returns picked_file_ids alongside the code. This is
not a hosted web Picker or a broad Drive grant.
"""
from dataclasses import replace
import urllib.parse

import garmin_backup_target as binding
import garmin_google_oauth as oauth


def folder_picker_attempt(client_id, port):
    original = oauth.new_authorization(client_id, port)
    parsed = urllib.parse.urlsplit(original.url)
    params = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    params.update({
        "trigger_onepick": ["true"],
        "allow_folder_selection": ["true"],
        "allow_multiple": ["false"],
        "mimetypes": ["application/vnd.google-apps.folder"],
    })
    url = urllib.parse.urlunsplit((
        parsed.scheme, parsed.netloc, parsed.path,
        urllib.parse.urlencode(params, doseq=True), ""))
    return replace(original, url=url)


def consume_folder_picker_callback(attempt, path):
    """Validate OAuth state/scope and precisely one picked folder identifier."""
    if not isinstance(path, str) or len(path) > 4096:
        raise oauth.OAuthError("Invalid Google Picker redirect")
    parsed = urllib.parse.urlsplit(path)
    if parsed.scheme or parsed.netloc or parsed.fragment or parsed.path != oauth.CALLBACK_PATH:
        raise oauth.OAuthError("Unexpected Google Picker callback path")
    try:
        params = urllib.parse.parse_qs(
            parsed.query, strict_parsing=True, keep_blank_values=True,
            max_num_fields=8)
    except ValueError as exc:
        raise oauth.OAuthError("Malformed Google Picker response") from exc
    if set(params) != {"code", "state", "scope", "picked_file_ids"} or any(
            len(vals) != 1 for vals in params.values()):
        raise oauth.OAuthError("Google Picker callback fields are incomplete or duplicated")
    if set(params["scope"][0].split()) != {oauth.DRIVE_SCOPE}:
        raise oauth.OAuthError("Google Picker must grant only drive.file scope")
    folder = params["picked_file_ids"][0]
    if "," in folder:
        raise oauth.OAuthError("Choose exactly one Google Drive folder")
    try:
        binding.valid_folder(folder)
    except binding.BindingError as exc:
        raise oauth.OAuthError("Google Picker selected an invalid folder ID") from exc
    # Reuse the strict, constant-time state validation without accepting
    # Picker-only data as a generic OAuth callback.
    sanitized = oauth.CALLBACK_PATH + "?" + urllib.parse.urlencode({
        "code": params["code"][0], "state": params["state"][0],
        "scope": params["scope"][0]})
    code = oauth.consume_callback(attempt, sanitized)
    return code, folder
