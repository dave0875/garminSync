> **DESIGN-IN-PROGRESS / NOT FOR PRODUCTION.** This is a legacy/manual transitional configuration guide, **not** the desired consumer installation. Draft PR #37's separate backup/archive remotes are incompatible with rclone's same-remote `--backup-dir` requirement (blocker #38). Do not attempt GORF cutover using this guide until #38 is fixed and reverified. First-party OAuth, Google Picker, automatic remotes and the local setup wizard are specified in [ADR-0001](ADR-0001-consumer-onboarding-google-oauth.md) and tracked by #39-#43.

# Per-installation Garmin Sync setup (Linux or macOS)

The Garmin model is **not** an installation parameter. Any enrolled Garmin device
exposing a readable `GARMIN/GarminDevice.xml` can be identified by its native device ID.
The MTP transport must still be compatible with the specific Garmin device; vendor
detection alone does not promise transport support for every Garmin product.

**Every installation chooses its own existing backup and archive folders.**
No account, folder name, watch model, or Drive location is inherited from
`dave0875`, from the source repository, or from an ambient `gdrive:` remote.
Cloud writes are disabled unless an installation binding is valid.

## 1. Prepare two Drive folders

Create a backup folder and an archive folder anywhere in **your own Google Drive**.
Give them whatever names you want, for example `Fitness/WatchFiles` and
`Fitness/History`. They must be distinct existing writable folders. Find each
folder's immutable ID in its Drive URL, which looks like
`https://drive.google.com/drive/folders/<FOLDER_ID>`.

The folder locations are independent of the Garmin device model and do not have
to live under a `Garmin` directory.

## 2. Configure a dedicated rclone OAuth file

Do **not** reuse a production `~/.config/rclone/rclone.conf` file or import
somebody else's Google token.

```bash
install -d -m 700 "$HOME/.config/garmin-sync"
rclone --config "$HOME/.config/garmin-sync/rclone.conf" config
chmod 600 "$HOME/.config/garmin-sync/rclone.conf"
```

Create two separate Google Drive remotes, for example `my-backup` and
`my-archive`, authenticated as the **same intended Google account**.
Configure each remote's advanced `root_folder_id` setting with its *own*
approved folder ID. This pins the root of the rclone remote to that exact
folder, not to a textual Drive path.

The first binding version supports OAuth Google Drive `drive` remotes.
Service accounts, Google Workspace impersonation, and shared-drive remotes
are deliberately rejected until dedicated account/permission tests exist.
Use your own OAuth client ID as recommended by current rclone documentation,
rather than depending indefinitely on shared client credentials.

For example, a valid pair of remotes conceptually looks like:

```ini
[my-backup]
type = drive
root_folder_id = <BACKUP_FOLDER_ID>
token = <RCLONE_MANAGED_OAUTH_TOKEN>

[my-archive]
type = drive
root_folder_id = <ARCHIVE_FOLDER_ID>
token = <RCLONE_MANAGED_OAUTH_TOKEN>
```

Do not paste real token values into GitHub issues or configuration examples.
Let `rclone config` provision and refresh each remote's OAuth token.

## 3. Inspect and approve the actual account and folders

Run this read-only inspection, using the same remotes and dedicated config:

```bash
python3 garmin_sync.py inspect-target \
  --rclone-config "$HOME/.config/garmin-sync/rclone.conf" \
  --backup-remote my-backup --archive-remote my-archive
```

This displays each authenticated Google account's `permission_id`, email,
and folder ID. Verify the email and IDs carefully. The permission ID is the
stable account binding; email is for human review.

Explicitly bind the account ID and the two folder IDs observed above:

```bash
python3 garmin_sync.py bind-target \
  --rclone-config "$HOME/.config/garmin-sync/rclone.conf" \
  --backup-remote my-backup --archive-remote my-archive \
  --google-account-id '<VERIFIED_GOOGLE_PERMISSION_ID>' \
  --backup-folder-id '<BACKUP_FOLDER_ID>' \
  --archive-folder-id '<ARCHIVE_FOLDER_ID>'
```

The installer creates `~/.config/garmin-sync/installation.json` privately.
Its `installation_id` is unique per installation. An existing binding is
never replaced implicitly. There is no supported automatic rebind command:
rotation, moving folders, or changing accounts requires a separate controlled
migration with read-only verification. Until then, identity mismatches fail
closed.

## 4. Enroll the Garmin device

Physically connect **only** the target Garmin device and use:

```bash
python3 garmin_sync.py enroll
python3 garmin_sync.py once --dry-run
python3 garmin_sync.py once
```

Enrolled device identity is stored separately as a SHA-256 digest at
`~/.config/garmin-sync/device.json`. It must match the connected Garmin
device for every sync; changing the device requires explicit re-enrollment.
The `GARMIN` source is treated as read-only and is never modified.

## 5. What the binding enforces

Each actual sync creates a temporary private snapshot of the dedicated rclone
config. It confirms both expected folder IDs in the remote configuration,
checks OAuth identities using Google Drive's `about.get` API, verifies both
folders exist and are writable, and passes the **same temporary credential
snapshot** to rclone for copying, archiving, and checking.

The process rejects an unknown Garmin device, unbound installation,
unexpected Google account, changed/recreated folder, unavailable Drive API,
expired/revoked credentials, wrong archive folder, unexpected rclone settings,
and insecure config permissions. It never silently selects another remote.

The backup uses `rclone copy --checksum`, not `sync`/delete.
Destination-only objects survive and overwritten versions go to the selected
archive root. FIT inspection remains diagnostic and cannot reject raw files.

### Trust boundaries and release status

A person with access to both the full private installation profile and its
OAuth tokens can impersonate that installation. Prevent that through OS-level
account isolation, encrypted backups, credential rotation, and access control.
Do not run arbitrary pull-request code or untrusted build jobs under the same
user identity as production Drive credentials.

**GORF migration:** the historic `gdrive:Garmin/Fenix8_Backup` and
`gdrive:Garmin/Fenix8_Archive` folders are existing operator data and are
not removed or renamed by this feature. Before promoting this branch, obtain
those folders' actual IDs, configure two new dedicated root-folder remotes,
inspect the account, and explicitly bind the installation. The GORF deployer
now refuses a new runtime when the installation is not bound. Keep this branch
out of the production `main` deployment until this read-only migration
has been qualified against the real account and existing folders.

On macOS, use `scripts/install-macos-launchagent.sh` after enrollment;
on Linux, install the per-user systemd service through the approved deployment
procedure. Neither should run as root.
