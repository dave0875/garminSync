# Garmin Sync development Google OAuth setup and release gates

**Status: publisher-only prerequisite; not a consumer installation step.**
Consumers must never need to configure a Google Cloud project or know what
`rclone` is. The publisher creates the app identity once. The current draft
onboarding code is **not ready for production Google accounts or GORF cutover**.

## Reproduced GORF UAT (2026-10-25)

From an isolated checkout of draft PR #37, the owner ran:

```bash
python3 -m unittest discover -s tests -p 'test_first_run*.py' -v
```

**Result:** 10 tests passed. The default `My Drive / Garmin Sync` was
rendered correctly. Enter/accept and `C`/Change both reached the
`Publisher Google OAuth application is not configured` guard, as expected.
`Q` canceled with `Setup cancelled; no folders created`. Neither
real OAuth consent nor Drive provisioning occurred. This validates the
local choice UI **only**, not live Google connection or USB backup.

## What the publisher needs to do

1. Sign into [Google Cloud Console](https://console.cloud.google.com/).
   Create/select a **development/testing** Google Cloud project dedicated to
   Garmin Sync, separate from the eventual production app.
2. Enable the **Google Drive API** and **Google Picker API** for the project.
3. Open **Google Auth Platform**: [Branding](https://console.cloud.google.com/auth/branding)
   to set app display name, developer contact and support contact, and
   [Audience](https://console.cloud.google.com/auth/audience) to select
   **External / Testing** with explicit authorized **test users**.
   Use a disposable test Google account, not the existing GORF production
   account or historical Drive folder.
4. In [Data Access](https://console.cloud.google.com/auth/scopes), request
   **only** `https://www.googleapis.com/auth/drive.file` for v1.
   Avoid full `drive` scope. Under the limited scope, files created
   by the app and folders explicitly shared through Google Picker are the
   expected surface; don't presume access to other existing files.
5. In [Clients](https://console.cloud.google.com/auth/clients) choose
   **Create client > Desktop app** (not Web application). Record the
   actual issued client ID ending in `.apps.googleusercontent.com`.
   The desktop app uses browser OAuth authorization code with PKCE and a
   transient loopback redirect on `127.0.0.1`; no hosted callback is needed.
   Keep any downloaded client JSON/private metadata out of Git.
6. Before a real connection test, qualify both authorization-code exchange
   and refresh-token behavior with the actual Desktop client in a **separate
   test OS account or disposable VM**. Google lists Desktop `client_secret`
   as optional for token exchange; implementations may still differ, so test
   whether the issued client requires it. Do not infer successful auth from
   passing offline/mock tests.
7. Confirm token storage, file permissions and account/folder ID pinning
   under an isolated user identity. The current draft uses a 0600 rclone
   configuration; Windows secure storage, GORF migration and production
   keyring/refresh handling remain incomplete.

The code currently reads publisher metadata from
`config/google-publisher.json` in the **source checkout** by default, or
from a path passed via `--publisher-metadata`. Store test metadata **outside
the repository**, for example:

```text
~/.config/garmin-sync-dev/google-publisher.json
{
  "client_id": "<actual issued Desktop client ID>",
  "client_secret": "<issued Desktop secret if required by qualified flow>"
}
```

On Linux/macOS, ensure the enclosing directory is private and the file
permissions are `0600`. The Desktop client's client secret is **not an
end-user password or a reliable confidentiality boundary**. Refresh/access
tokens ARE sensitive and must never be shared, pasted into issues, or
uploaded to GitHub. `.gitignore` excludes source-tree
`config/google-publisher.json` as an additional guard.

**IMPORTANT:** Don't run live `setup-google` in the GORF production Linux
user yet. Changing `--publisher-metadata` alone does **not** isolate the
installation binding. The CLI's default profile path is still
`~/.config/garmin-sync/installation.json`. Complete any real tests using
a dedicated disposable Linux user or VM with a dedicated test Google account
and no access to production Drive credentials or service.

## What qualifies the production publisher app

- A verified publisher-controlled domain; public product homepage, privacy
  policy and support contact matching Google OAuth consent.
- Accurate disclosure of data retention, account revocation and how locally
  stored user OAuth tokens are protected and removed.
- Separate production Desktop client registration(s) for supported platforms,
  production consent testing, and whatever Google verification is required
  for the final scopes and branding. No claim of approval until it arrives.
- Complete `#35` host credential isolation, `#36` cross-user real Drive UAT,
  `#43` packaging, and `#45` Windows WPD/MTP + secured secrets before
  promoting PR #37 or touching GORF production.

References:
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://support.google.com/cloud/answer/15544987
- https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker
- https://rclone.org/drive/#making-your-own-client-id

Related issues: #39, #40, #41, #42, #44, #45.
