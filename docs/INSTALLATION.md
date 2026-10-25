# Transitional installation and explicit Drive-folder binding

> **Draft PR #37 only. Not a production release.** This is an advanced/manual
> migration procedure while the first-party Google OAuth and guided setup flow
> are implemented in issues #39-#43. A new end user should ultimately connect
> Google via the system browser and choose their folder without touching rclone.
> Do not use this draft to migrate GORF until independent real-account tests and
> GORF-specific read-only qualification have been completed.

## What is configurable?

Garmin Sync supports compatible Garmin devices exposing `GarminDevice.xml`
through a supported USB/MTP transport. The model is discovered from the device.
A different device cannot upload unless explicitly enrolled.

Each installation chooses a **parent directory anywhere in its own Google
Drive**. Within that parent, there must be two separate, explicitly approved
folders: one for active backups, one for history/archive. The folders may have
user-chosen names. Their Google IDs, parent ID, and principal ID are pinned.

**Important:** `rclone copy --backup-dir` requires the destination and archive
to share one remote. Do NOT configure independent `backup:` and `archive:`
remotes. An approved configuration instead maps one remote to the parent:

```text
userdrive:                        Google Drive parent_folder_id
  Watch Backup/                  backup_folder_id
  Retained History/              archive_folder_id
    <run-id>/                    overwritten-file versions
```

A later folder rename, account change or recreated directory is blocked until
the user explicitly repairs/rebinds the installation. The program never falls
back to the logged-in user's ambient `gdrive:`.

## Manual transitional setup (test accounts only)

Create the parent, active and archive folders in the intended Drive account.
Use their real IDs from Google Drive folder URLs. Configure exactly one private
rclone Google Drive remote rooted at the selected parent folder's ID:

```bash
install -d -m 700 "$HOME/.config/garmin-sync"
rclone --config "$HOME/.config/garmin-sync/rclone.conf" config
chmod 600 "$HOME/.config/garmin-sync/rclone.conf"
```

Configure its alias as `userdrive`, type `drive`, and set
`root_folder_id = <PARENT_FOLDER_ID>`. For now this requires the operator to
authenticate using rclone. The public consumer release will automate OAuth
and private rclone provisioning (issues #39/#40).

Review the account and child folder identities via read-only API calls:

```bash
python3 garmin_sync.py inspect-target \
  --rclone-config "$HOME/.config/garmin-sync/rclone.conf" \
  --remote userdrive \
  --backup-dir 'Watch Backup' --archive-dir 'Retained History' \
  --backup-folder-id '<BACKUP_FOLDER_ID>' \
  --archive-folder-id '<ARCHIVE_FOLDER_ID>'
```

The inspected principal ID and parent ID must be independently checked against
the operator's intended account and folder before binding. The read-only
inspection also checks direct parent relationships and duplicate child folder
names. Do not copy unknown IDs or rclone credentials from a third party.

Explicitly approve and bind:

```bash
python3 garmin_sync.py bind-target \
  --rclone-config "$HOME/.config/garmin-sync/rclone.conf" \
  --remote userdrive \
  --backup-dir 'Watch Backup' --archive-dir 'Retained History' \
  --backup-folder-id '<BACKUP_FOLDER_ID>' \
  --archive-folder-id '<ARCHIVE_FOLDER_ID>' \
  --parent-folder-id '<PARENT_FOLDER_ID>' \
  --google-account-id '<VERIFIED_PERMISSION_ID>'
```

The private `installation.json` uses schema v2 and is immutable on ordinary
startup. Unsupported older v1/two-remote profiles fail closed and must be
rebound deliberately. No automatic migration or fallback.

Connect one supported Garmin device, then:

```bash
python3 garmin_sync.py enroll
python3 garmin_sync.py once --dry-run
python3 garmin_sync.py once
python3 garmin_sync.py doctor
```

These commands do **not** run unless watch enrollment and destination
authorization requirements are met.

## GORF and legacy archive migration

Current GORF Google Drive:
`Garmin/Fenix8_Backup` and `Garmin/Fenix8_Archive`.
These are **existing sibling folders** with valuable historical data. When
qualifying migration, the new **one** remote should be rooted at the
existing `Garmin` parent ID and use:

- backup directory: `Fenix8_Backup` (same existing ID)
- archive directory: `Fenix8_Archive` (same existing ID)

Do not create replacement folders, move, delete or re-upload existing objects.
The legacy Drive OAuth scopes may see existing files that a new `drive.file`
client cannot see. A scope change therefore requires its own compatibility
assessment. No production Drive mutations during preflight.

The guarded GORF deployer only permits an explicit owner-authorized,
main-branch manual deployment with approved credentials and pre-bound
configuration. It is intentionally **not** triggered by a draft PR.

## Privacy and protection

The enrolled Garmin device is read-only. FIT decoding is diagnostic.
Destination-only files are retained, overwritten destination files are
archived, and final checks prove verified copies before marking success.

Profile and rclone files are private to the local operating-system user.
Do not use production OAuth tokens in CI, GitHub issues, screenshots,
support bundles, or public recordings. For broader release, the architecture
uses a publisher-owned Desktop OAuth client, PKCE, Google Picker and
`drive.file` scope; see [ADR-0001](ADR-0001-consumer-onboarding-google-oauth.md).
