"""Platform-independent first-run Drive location recommendation.

Never creates a Drive folder, stores OAuth credentials, or selects a remote.
UI and CLI consume this read-only plan before launching OAuth/Picker.
"""
from dataclasses import dataclass


DEFAULT_PARENT_NAME = "Garmin Sync"
DEFAULT_BACKUP_NAME = "Backups"
DEFAULT_ARCHIVE_NAME = "Archive"


@dataclass(frozen=True)
class DestinationSuggestion:
    title: str
    parent_display_path: str
    backup_display_path: str
    archive_display_path: str
    primary_action: str
    secondary_action: str


def recommend_destination(approved_profile_exists=False):
    """Same *Google Drive* default on Windows, macOS, and Linux.

    A configured installation has no default proposal: its pinned destination
    remains authoritative and must not be silently replaced.
    """
    if approved_profile_exists:
        return None
    parent = "My Drive / " + DEFAULT_PARENT_NAME
    return DestinationSuggestion(
        title="Where should Garmin Sync save your data?",
        parent_display_path=parent,
        backup_display_path=parent + " / " + DEFAULT_BACKUP_NAME,
        archive_display_path=parent + " / " + DEFAULT_ARCHIVE_NAME,
        primary_action="Use suggested location",
        secondary_action="Change location",
    )


def interpret_destination_choice(choice):
    """Return 'default', 'picker', or 'cancel'. Never silently accept errors.

    Enter/empty input is an affirmative user action only if the prompt has
    clearly presented the default and its acceptance semantics.
    """
    if not isinstance(choice, str):
        raise ValueError("Invalid destination choice")
    normalized = choice.strip().casefold()
    if normalized in ("", "use", "default", "d", "1"):
        return "default"
    if normalized in ("change", "choose", "picker", "c", "2"):
        return "picker"
    if normalized in ("cancel", "quit", "q", "3"):
        return "cancel"
    raise ValueError("Choose the suggested location, change, or cancel")
