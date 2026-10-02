# Garmin Fenix 8 native sync

Native, read-only preservation of a physically connected Garmin Fenix 8 to `gdrive:Garmin/Fenix8_Backup`.

```text
Fenix 8 native filesystem -> GVFS/GIO discovery -> rclone copy/check -> Google Drive
                                           \
                                            -> optional official garmin-fit-sdk diagnostics
```

The device filesystem is authoritative. `rclone copy` is non-destructive: destination-only objects remain, changed destination objects are placed beneath `gdrive:Garmin/Fenix8_Archive`, and the watch is never written to. FIT decoding is post-copy diagnostics; it cannot reject a legitimate native artifact.

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

CI runs static checks, proves the production source contains no cloud-client references, and runs the local-rclone regression suite. It does not access a watch, Drive, or credentials.

## GORF deployment

Deployments occur only from `main`, after CI, on a runner labeled `self-hosted`, `Linux`, `X64`, `gorf`, and `garmin-sync`. The deploy script is guarded to run only as `dbarker` on host `GORF`; it runs tests, snapshots changed installed files to `~/.local/bin/garmin-retired/deploy-<UTC>/`, atomically replaces the native service files, reloads systemd, and restarts the service. It never invokes rclone or writes to the watch.

The existing GORF runners are registered to other repositories and cannot serve this repository simultaneously. Register a separate runner rather than reconfiguring one of them:

```bash
mkdir -p ~/actions-runner-garmin-sync && cd ~/actions-runner-garmin-sync
# Download the matching runner release, then obtain a one-time registration token
# from github.com/dave0875/garminSync/settings/actions/runners/new.
./config.sh --url https://github.com/dave0875/garminSync --token TOKEN \
  --name garmin-sync-gorf --labels gorf,garmin-sync --unattended
./svc.sh install
./svc.sh start
```

The repository must use a GitHub account token with repository creation and push access. The workstation's current `gh` token is expired, so authenticate with `gh auth login -h github.com` before creating or pushing the remote.

## Operator commands

```bash
garmin-syncctl status
journalctl --user -u garmin-sync.service -n 50 --no-pager
garmin-syncctl force
garmin-syncctl dry-run
garmin_fit_inspect.py /path/to/native.fit
systemctl --user restart garmin-sync.service
systemctl --user stop garmin-sync.service
garmin-syncctl verify
```

