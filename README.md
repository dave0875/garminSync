> **DESIGN-IN-PROGRESS / NOT FOR PRODUCTION.** The current CLI/manual rclone steps are transitional. Consumer onboarding is specified in [ADR-0001](docs/ADR-0001-consumer-onboarding-google-oauth.md) (#39-#43); draft PR #37 must also fix the rclone archive same-remote incompatibility (#38) before use.

# Garmin Sync: enrolled-device preservation to an identity-bound Google Drive

Native, non-destructive backup for **supported Garmin devices**, not just the Fenix 8.
The device must expose `GARMIN/GarminDevice.xml` and its native files through
a supported USB/MTP transport. Enrollment pins its SHA-256 device identity.
This repository does not use Garmin Connect, Garth, or Garmin's cloud.

**Each installation selects its own Google account and any two existing, writable
Drive folders for backups and archives.** Names such as `Fenix8_Backup`,
`Garmin`, or the rclone remote alias `gdrive:` are **not required**.
No Drive mutation occurs without an explicitly approved, private installation
profile and live checks of the approved Google principal and both folder IDs.

- [Per-user installation, Drive binding and migration guide](docs/INSTALLATION.md)
- [GitHub CI/CD workflow](.github/workflows/ci.yml)

## Architecture

```text
Enrolled Garmin with GarminDevice.xml
  Linux: GVFS/GIO        macOS: read-only libmtp snapshot
             \             /
              source inventory + device identity verification
                              |
             installed binding + pinned Google permission ID
                              |
           independently pinned backup and archive folder IDs
                              |
         private rclone credential snapshot + account/folder preflight
                              |
                rclone copy --checksum / backup-dir
                              |
               rclone check + final Garmin verification
```

The Garmin filesystem is authoritative and read-only. Destination-only files
remain intact, overwritten destination objects are archived indefinitely by
default, and FIT decoding is diagnostic only. Maps, firmware/unlock files,
music, videos and image media are excluded; `Activity`, `Monitor`, `Metrics`,
`HRVStatus`, `Sleep`, and `Debug/MetricsBackup` are eligible when present.

## Experimental consumer onboarding (not yet publicly usable)

The draft feature branch now contains an opt-in `setup-google` command which,
**once a real publisher-owned Desktop OAuth client is supplied**, opens Google
in the system browser, authenticates through PKCE/loopback, presents the actual
Google account for confirmation, and creates a private rclone remote and pinned
Drive folder binding. It supports either an app-created default parent or a
user-selected parent through Google Picker. No independent `rclone config`
is required for this experimental path.

```bash
# Development-only. Requires a real publisher-issued config/google-publisher.json:
python3 garmin_sync.py setup-google

# Ask Google Picker to choose any writable parent folder:
python3 garmin_sync.py setup-google --choose-folder
```

**Do not use this against production accounts yet.** No publisher OAuth
client/consent verification has been shipped; the browser-based setup wizard
and native keyring policies remain incomplete. The development CLI deliberately
fails closed without publisher metadata. OAuth secrets are currently written
to a private 0600 rclone configuration for test integration, pending secure
OS credential-store qualification. See #39-#43 and the ADR.

## First-time setup

Install Python 3.12+, rclone, and the OS-specific USB transport dependencies.
Linux requires Gio/GVFS; macOS requires libmtp. Install Python requirements with:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. .venv/bin/python -m unittest discover -s tests -v
```

Before the first `once`/daemon/watch run, complete the instructions in
[docs/INSTALLATION.md](docs/INSTALLATION.md):

1. Choose or create **your own** Google Drive backup and archive folders.
2. Create a private, dedicated rclone config with separate folder-rooted OAuth remotes.
3. Use `inspect-target` to verify the actual Google account and both folder IDs.
4. Use `bind-target` to record the verified IDs with explicit approval.
5. Enroll your connected Garmin device using `garmin_sync.py enroll`.
6. Run `once --dry-run`, then `once`.

```bash
.venv/bin/python garmin_sync.py doctor
.venv/bin/python garmin_sync.py once --dry-run
.venv/bin/python garmin_sync.py once
.venv/bin/python garmin_sync.py verify
```

An unknown device, absent private binding, wrong Google identity, mismatched
folder IDs, or unavailable API fails closed. The program does **not** infer the
destination from the logged-in user's default rclone remote.

## Automatic background sync

On Linux, the existing `garmin-sync.service` observes Gio mount events and
periodic scans. On macOS, the `scripts/install-macos-launchagent.sh`
installer starts a per-user watch process. Neither changes the selected
Drive target; both load the same private bound installation.

Useful operator commands:

```bash
garmin-syncctl status
garmin-syncctl doctor
garmin-syncctl force
garmin-syncctl dry-run
journalctl --user -u garmin-sync.service -n 50 --no-pager
```

For macOS:

```bash
bash scripts/install-macos-launchagent.sh install
bash scripts/install-macos-launchagent.sh status
tail -f ~/Library/Logs/garmin-sync/watch.log
```

## GORF deployment and migration

**The production GORF installation is a legacy Fenix 8 configuration, not a
requirement for other users.** Its original Drive locations are
`Garmin/Fenix8_Backup` and `Garmin/Fenix8_Archive`; they must be preserved.

The guarded GORF deploy script runs only as `dbarker@GORF`, and GitHub
Actions deployment now requires an **explicit owner-triggered manual dispatch**
on the current main SHA with the `DEPLOY_GORF` confirmation. Ordinary merges
and branch pushes do not automatically deploy. The script installs atomically,
verifies application readiness and rolls back on failure. The workflow is
restricted to the original repository's main branch and a specifically
labeled GORF runner. Clones/forks do not deploy to GORF.

The new code explicitly **refuses promotion without a bound private installation
profile**; before merging a release, bind two dedicated remotes rooted at the
**existing** backup/archive folder IDs, verify the account and perform a
read-only qualification. See the migration warning in the installation guide.
Do not rename, relocate, or replace those folders during migration.
An optional runner/privilege isolation hardening remains tracked in #35.

## Verification and operational truth

`status` distinguishes process readiness from device backup health. The
`activity_verified` journal event identifies the most recent verified native
Activity FIT by path, run ID and timestamp. Merely discovering or previewing
a FIT does not prove it was uploaded.

The local regression suite checks upload idempotence, archive-on-overwrite,
checksum failures, disconnected/watch-identity races, FIT selection, Mac MTP
parsing, and account/folder binding. CI has no access to real Garmin hardware
or personal OAuth credentials.

## Known limitations

This version supports OAuth Google Drive remotes with existing folder ID roots.
Service accounts, shared drives, and unsupported Garmin MTP transports require
separate verification before they can be enabled safely. A person who steals
the private OAuth tokens and the installation binding can impersonate the
installation; maintain OS file permissions and separate CI from production
credentials.
