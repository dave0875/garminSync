# Three-platform Garmin Sync release contract

**Status: pre-release. Windows support is under development and is NOT yet
production ready.** Never treat a green GitHub Actions import/smoke test as
proof of real Garmin USB/MTP access or secure unattended OAuth operation.

## The first-run experience is the same on every computer

The default is a **Google Drive cloud location**, not a local Downloads,
Documents, OneDrive, iCloud or system drive. Suggest:

```text
Your Google account: verified-user@example.com
Suggested destination: My Drive / Garmin Sync

[Use suggested location]   [Change location]
```

- Press Enter/click **Use suggested location** to accept. Click **Change
  location** to choose any folder made available through Google's supported
  Picker OAuth flow.
- After Google authorization, display the actual verified account, chosen
  destination and archive policy; **the user must confirm before creating any
  Drive folder**.
- Only one cloud folder choice is needed. The application creates/manages
  `Backups` and `Archive` under the approved parent. Archiving retains only
  versions displaced by an overwrite; it is not a second full copy each run.
- Different Google accounts have different folders even if their displayed
  paths look the same. Identity checks use immutable IDs, not visual names.
- Existing enrolled installations are never given a new "default" nor
  silently rebound or moved. The GORF legacy parent/child IDs are preserved.
- The default is **model-neutral**: no `Fenix8` path names or required
  `gdrive:` remote.

## Operating system architecture

| Capability | Linux | macOS | Windows |
|---|---|---|---|
| Default/Change Drive location | UI/CLI integration in progress | Same | Same |
| Google OAuth flow | Desktop browser/PKCE prototype | Same | PKCE smoke-tested; secure storage pending |
| Local settings (new installs) | `~/.config/garmin-sync` | Current legacy path until migration | `%LOCALAPPDATA%/GarminSync` |
| Garmin file access | Gio/GVFS USB MTP | libmtp read-only staging | Read-only drive-letter discovery added; **WPD/MTP missing** |
| Automatic sync | Per-user systemd | Per-user LaunchAgent | Per-user Task Scheduler **not implemented** |
| Interprocess lock | fcntl | fcntl | msvcrt adapter smoke-tested |
| Release status | Legacy GORF service exists; new onboarding not released | Existing manual/LaunchAgent runtime, new onboarding not released | **Not production ready** |

**Windows caution:** Many modern Garmin watches appear in Explorer as
*Portable Devices*, not ordinary drive letters. Win32 volume enumeration does
not expose those device files. A Windows WPD (Windows Portable Devices)
streaming adapter must enumerate Garmin MTP device objects, read a
GarminDevice.xml, stage files read-only, verify sizes/inventory and reject
device changes. Do not replace this with a broad scan of user Documents,
OneDrive or fixed drives. Avoid a dependence on a fragile interactive
Explorer copy and avoid WinFsp just to ingest USB device files.

**Credentials caution:** Windows POSIX-style `chmod 600` does not establish
a private DACL. Windows configuration reads currently deliberately block
before using tokens until a verified per-user DPAPI/Windows Credential
Manager and ACL adapter is available. A runnable Windows Python import is
not sufficient to call the application supported.

## Deployment acceptance matrix

1. CI on `ubuntu-latest`, `macos-latest` and `windows-latest` must
   compile and run the portable recommendation/OAuth/credential-free tests.
2. Linux full integration tests must remain green and GORF unchanged until
   explicit, reversible migration. macOS must pass libmtp hardware UAT.
3. Windows needs native WPD/MTP and mass-storage hardware fixtures, file
   checksums, a read-only transfer test using an actual Garmin, an explicitly
   non-administrator Task Scheduler entry and safe uninstall/rollback.
4. Google authorization and Drive folder Picker must operate with test
   publisher credentials on all three operating systems, and must create
   folders only after user confirmation. Token storage and refresh must be
   OS-protected, not inherited from an ambient `gdrive:` remote.
5. Two test Google accounts and two separate Garmin identities must
   demonstrate that neither installation can upload into the other's Drive.
6. Only when hardware, OAuth, storage, installer, and recovery gates pass
   may Windows be declared supported. Until then `doctor` must truthfully
   report missing WPD/credential support and uploads fail closed.

References:
- https://learn.microsoft.com/en-us/windows/win32/wpd_sdk/portal
- https://learn.microsoft.com/en-us/windows/win32/taskschd/task-scheduler-start-page
- https://rclone.org/install/#windows-installation
- https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker

Issues: #33, #34, #35, #36, #38, #40, #41, #42, #43, #44, #45.
Draft implementation: PR #37.
