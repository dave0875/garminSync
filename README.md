# Garmin Fenix 8 native sync

Native, read-only preservation of a physically connected Garmin Fenix 8 to `gdrive:Garmin/Fenix8_Backup`.

```text
Fenix 8 native filesystem -> GVFS/GIO discovery -> rclone copy/check -> Google Drive
                                           \
                                            -> optional official garmin-fit-sdk diagnostics
```

The device filesystem is authoritative. `rclone copy` is non-destructive: destination-only objects remain, changed destination objects are placed beneath `gdrive:Garmin/Fenix8_Archive`, and the watch is never written to. FIT decoding is post-copy diagnostics; it cannot reject a legitimate native artifact.

The backup is bound to an explicitly enrolled Fenix identity. The raw Garmin device ID is never stored; its SHA-256 identity is written to `~/.config/garmin-sync/device.json` with private permissions. An unenrolled, different, or ambiguous Fenix cannot write into the authoritative backup.

## Deliberate exclusions

Maps, unlock/firmware assets, music, video, and image media are excluded. All other native files are considered, including `Debug/MetricsBackup`, `Activity`, `Monitor`, `Metrics`, `HRVStatus`, and `Sleep` whenever present. No Garth, Garmin Connect API, browser authentication, token, cookie, or cloud JSON source is used or included in this project.

## Layout

- `garmin_sync.py` — discovery, locking, raw transport, verification, state.
- `garmin_fit_inspect.py` — read-only official FIT SDK diagnostics.
- `systemd/` and `bin/` — installed user-service and operator entry points.
- `tests/` — controller and local-rclone regression fixtures.
- `scripts/deploy-gorf.sh` — guarded, atomic per-user deployment for GORF.

Local device data, runtime state, forensic evidence, credentials, and the pre-cutover rollback archive are intentionally excluded from Git.

## Local development and CI

Use Python 3.12+ and rclone:

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
PYTHONPATH=. python3 -m unittest discover -s tests -v
```

CI installs and verifies the tooling used by its static guards, proves the production source contains no cloud-client references, smoke-tests GIO, exercises the checkout's official FIT SDK inspector with valid and corrupt FIT inputs, and runs the local-rclone regression suite. It does not access a watch, Drive, or credentials.

## GORF deployment

Deployments occur only from the current `main` revision, after CI, on a runner labeled `self-hosted`, `Linux`, `X64`, `gorf`, and `garmin-sync`. The deploy script is guarded to run only as `dbarker` on host `GORF`, verifies that the checkout SHA is still the current GitHub `main` SHA, and refuses stale workflow runs.

The deployer compares the candidate runtime with the installed runtime. Documentation-only and runner-only changes do not restart `garmin-sync.service`. A real runtime/dependency change is snapshotted beneath `~/.local/bin/garmin-retired/deploy-<UTC>/`, installed atomically, stamped in `~/.local/bin/garmin-sync/build.json`, and accepted only after application-level PID/build readiness matches the deployed commit. The one-time migration also archives and removes the retired `~/.config/autostart/garmin-sync.desktop` launcher so systemd is the sole automatic owner. Deployment never invokes rclone against the watch.

The existing GORF runners are registered to other repositories and cannot serve this repository simultaneously. Register a separate runner rather than reconfiguring one of them:

```bash
mkdir -p ~/actions-runner-garmin-sync && cd ~/actions-runner-garmin-sync
# Download the matching runner release, then obtain a one-time registration token
# from github.com/dave0875/garminSync/settings/actions/runners/new.
./config.sh --url https://github.com/dave0875/garminSync --token TOKEN \
  --name garmin-sync-gorf --labels gorf,garmin-sync --unattended
install -Dm644 /path/to/garminSync/systemd/actions-runner-garmin-sync.service \
  ~/.config/systemd/user/actions-runner-garmin-sync.service
systemctl --user daemon-reload
systemctl --user enable --now actions-runner-garmin-sync.service
```

## First enrollment and replacement

After the hardening release is installed, the service intentionally remains in `unenrolled` state until the attached Fenix is explicitly pinned:

```bash
garmin-syncctl enroll
garmin-syncctl status
```

Enrollment requires exactly one qualifying Fenix 8. Replacing the enrolled watch is an explicit maintenance action:

```bash
systemctl --user stop garmin-sync.service
garmin-syncctl enroll --replace
systemctl --user start garmin-sync.service
```

## Archive policy

`gdrive:Garmin/Fenix8_Archive` is preservation storage for overwritten destination objects. The default policy is **preserve indefinitely; no automatic deletion**. `garmin-syncctl doctor` reports the number of top-level archive run directories so growth is visible and can be reviewed manually before any future retention policy is introduced.

## Operator commands

```bash
garmin-syncctl status
garmin-syncctl doctor
journalctl --user -u garmin-sync.service -n 50 --no-pager
garmin-syncctl force
garmin-syncctl dry-run
garmin_fit_inspect.py /path/to/native.fit

# A full direct verification owns the same process lock as the daemon.
systemctl --user stop garmin-sync.service
garmin-syncctl verify
systemctl --user start garmin-sync.service
```

`status` reports the live systemd PID, daemon PID, enrolled-device state, and deployed build metadata; it exits non-zero when the application is not healthy. `readiness` is an internal deployment probe that verifies process/build initialization even when a newly installed service is intentionally waiting for its first enrollment.
