# ADR-0001: First-party Google OAuth and zero-configuration Garmin Sync onboarding

**Status:** Proposed design / implementation gate (2026-10-24)
**Owner:** Garmin Sync
**Applies to:** GitHub issues #33-#36, draft PR #37 and onboarding follow-up issues.
**Decision:** Implement a local-first, browser-assisted setup that provisions its **own** Google Drive authorization and storage connection. Never require the user to configure an rclone `gdrive:` remote, create a Google Cloud project, or type Drive folder IDs.

## 1. User contract and success measure

User journey: **Connect a compatible Garmin -> Connect Google Drive -> Choose a location -> Enable automatic backup**. A first-time user should not be exposed to FIT, OAuth tokens, refresh tokens, rclone, root folder IDs, systemd, LaunchAgent, or shell commands.

- Support any compatible Garmin model exposing a stable identity in `GARMIN/GarminDevice.xml` via an implemented USB/MTP transport. A Garmin vendor/product ID alone is NOT proof that a particular transport supports a device. Detect/enroll the actual identity; no Fenix 8-only naming or paths.
- New users may accept a default parent folder created by Garmin Sync or choose **any user-authorized parent folder**. Under it, Garmin Sync creates separate, app-owned Backups and Archive siblings. The two child folders are pinned independently; archive and backup must not overlap. Choosing *unrelated* backup/archive parent locations is deferred to an advanced separately-qualified archival backend.
- Data remains in the user's own Google Drive. No hosted Garmin Sync service receives device bytes, refresh tokens, or password. Garmin Sync requests a narrow scope, displays the Google account and exact destination, and records explicit consent.
- Start sync only after device, account, folder ownership/access, OAuth scope, permission validation, and a read-only preview pass. Do not claim success until copy and final check pass.
- Every deployment is isolated. A clone of a source repository has **no** valid credential or destination until its own owner authorizes it.
- Existing GORF must retain its exact existing backup/archive objects and paths unless the operator expressly migrates them. No automatic roll-out to GORF.

## 2. Google Cloud identity, OAuth scope, and publishing

**Application publisher** maintains a dedicated Google Cloud project for Garmin Sync, rather than asking each user to register an OAuth client. Enable Google Drive API and, if using a picker, Google Picker API. Register a Desktop OAuth client appropriate for installed Linux/macOS clients and publish its **client ID** as application metadata. Treat all installed clients as public. Never embed a client secret as a security boundary.

Authorization: system-browser OAuth authorization code with **PKCE S256**, unique cryptographic `state`, random loopback port bound only to `127.0.0.1`, precise redirect matching, short-lived one-shot callback, and offline access for background refresh. Use Google-maintained OAuth client libraries where appropriate. Do not implement password collection, embedded webviews, out-of-band copy/paste authorization codes, or device-code shortcuts without policy review.

**Default scope:** `https://www.googleapis.com/auth/drive.file` ONLY for new installations. The scope is non-sensitive under current Google Drive guidance and can manage files created by Garmin Sync or specifically granted to the app through Google Picker. A selected existing folder is not proof that its preexisting child files are readable under `drive.file`; test listing, app permissions, overwrite semantics, archive, and checksum behavior against an isolated Google account.

**Never silently request `https://www.googleapis.com/auth/drive`** (restricted). If existing third-party-created files or legacy re-upload/overwrite cannot be supported with `drive.file`, offer an explicit limited migration path or a separately reviewed privileged compatibility mode. Public release should not default to broad Drive authorization.

**Publication prerequisites before broad public release:**
1. Publisher-controlled, verified domain with an informative public homepage and privacy policy on that domain, linked in-app and in the OAuth consent configuration. Provide accurate support and developer contacts and Google-compliant branding.
2. Consistent product identity in the Cloud project, OAuth consent screen, homepage, binaries, and any Picker integration.
3. Publish complete data-access, storage, deletion/revocation, retention, security, Limited Use, and local-only data-handling disclosures. A repo LICENSE/README is not a substitute for privacy disclosures.
4. Provide a reproducible end-to-end consent/onboarding demo; submit Google brand verification and any additional verification requested for the final scope set. Do not claim verified status until Google approves.
5. Use separate **testing** and **production** OAuth clients/projects, test users and sanitized test recordings; never distribute test credentials, and never rely on the rclone shared Google client ID (its retirement is documented for 2026).
6. Keep project ownership, quota, client configuration and consent status operationally monitored. Have a revocation/rotation and lost-client-ID recovery playbook.

Authority: 
- https://developers.google.com/workspace/drive/api/guides/api-specific-auth
- https://developers.google.com/identity/protocols/oauth2/native-app
- https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker
- https://support.google.com/cloud/answer/13464321
- https://rclone.org/drive/

## 3. Simple first-run UX (local-first)

Serve a minimal setup UI from the installed local agent on a randomized `127.0.0.1` port. Restrict Host/origin, require unpredictable session tokens and CSRF verification for local mutations, and never expose arbitrary local paths/files to an unauthenticated browser. UI actions communicate with the **local** installation, not a cloud relay. The setup page must never log or render refresh tokens.

Suggested steps:
1. **Welcome and permissions:** Explain exactly what sync reads, what it excludes, and where data will go.
2. **Detect Garmin:** Watch for a recognized device; show model and a non-sensitive identifier. For more than one connected device, ask user to choose rather than auto-enroll. Unsupported transport produces actionable feedback without promises of universal support.
3. **Connect Google:** Native browser redirects to Google; check returned state, audience/client, scopes, account binding, code exchange and refresh token. Show chosen account address; if account switches later, stop and require explicit rebind.
4. **Choose location:** Primary CTA **Create my Garmin Sync folder**. The app creates `Garmin Sync` in the authorized Drive and app-owned `Backups` and `Archive` children. Alternate CTA **Choose another folder**, backed by Google Picker granting this app access to the selected *parent* folder; verify its immutable ID and writable capability, then create app-owned child folders and confirm their IDs. Create **one rclone Drive remote rooted at the authorized parent**, with copy destination `remote:Backups` and `--backup-dir remote:Archive/<run-id>`. Never silently fall back to a different folder if Picker permissions are insufficient.
5. **Confirm:** Human-readable account, target folder, enrolled device and archive policy. Provide editable choice, local privacy notice and copyable non-sensitive diagnostics.
6. **Activate:** Create private per-installation config and rclone backends atomically; run a true read-only preview. Only after explicit user opt-in install/enable the supported background service/LaunchAgent. Show first real copy/check progress and verified completion; offer cancel, retry and safe disconnect guidance.

**Google Picker for desktop apps:** Google documents an authorization URL flow using `drive.file` with `prompt=consent`, `trigger_onepick=true`, `allow_folder_selection=true`, and `response_type=code`. The callback includes `picked_file_ids` and the authorization code. Use the same OAuth Desktop client, PKCE verifier and state validation as normal consent, then bind **one explicitly chosen writable folder ID** after token exchange. Prefer this supported desktop flow over introducing a hosted token-bearing Picker bridge. If the user wants the app to create a new folder, ordinary OAuth consent followed by Drive API folder creation works without Picker. Reference: https://developers.google.com/workspace/drive/picker/guides/desktop-mobile-picker.

Advanced path: CLI `garmin-sync setup --headless` prints a short-lived authenticated **local** enrollment URL or uses supported remote-browser/SSH-port-forward flow. Do not expose callback listeners to public interfaces or ask users to paste Google account passwords/refresh tokens.

Once configured: a small status page/command shows "Waiting for Garmin", "Connected, not yet backed up", "Syncing", "Verified at [date]", and clear actionable states "Google sign-in required" / "Destination changed" / "Device changed". Configured != verified.

## 4. Modules and separation of concerns

```text
Local installer / CLI / 127.0.0.1 setup UI
       |-- Garmin discovery + explicit device enrollment (model agnostic)
       |-- Google OAuth PKCE / secure token manager
       |-- Google Picker / app-owned folder provisioner
       |-- destination account + folder identity binder
       +-- per-user service/LaunchAgent activation
                    |
          Dedicated rclone backend adapter
                    |
   native file snapshot -> guarded copy/archive -> rclone check
                    |
        local-only health report and auditable logs

Google Cloud (publisher only):
       OAuth Desktop client registration + consent brand + Drive/Picker APIs
       No Garmin data uploads / no server-held Google refresh tokens
```

- `auth_google` owns OAuth, refresh, revoke and identity resolution. Store refresh token in OS credential store where available (macOS Keychain; Linux Secret Service, or encrypted/private storage with explicit limitations), never in a GitHub repo. Be explicit if a headless machine cannot unlock a user keyring unattended. Ownership/permissions remain per installation.
- `folder_selection` wraps Picker/create-folder and must verify the selected folder was explicitly granted to this app. It returns **Drive IDs**, not trusted human names or browser-supplied access tokens.
- `drive_binding` pins Google stable principal ID, OAuth client ID / approved scopes, canonical backup/archive folder IDs, config version and unique installation/device identifiers. Every mutation/retry verifies the credentials correspond to the binding; token refresh must not widen access or silently switch accounts. Use one credential snapshot/handle for validation and transfer to prevent accidental mixups.
- `rclone_adapter` is internal and receives exactly an authorized **common parent remote** plus a narrowly-scoped access token/refresh capability. Generate one private rclone Drive remote rooted at the selected parent ID, with distinct pinned backup and archive child IDs/paths inside it. `rclone copy --backup-dir` **requires the archive and destination to be on the same rclone remote**. This is a hard compatibility invariant and a release blocker for the two-root implementation currently in draft PR #37. Do **not** inherit ambient `gdrive:` or `RCLONE_*` values, and do **not** require users to run `rclone config`. Test expiry/refresh, upgrades, and token ownership; avoid writing credentials to logs.
- `sync_engine` continues existing source-read-only, archive-before-overwrite, checksum verification, retry/backoff, and FIT-byte preservation behavior.
- `installer` handles OS-specific dependencies with signed/verified downloads or trusted package managers. Don't run remote installer scripts as root, request privileged permissions only when required, or automatically modify a shared production runner.

These modules are boundaries, not necessarily individual Python files.

## 5. Configuration / setup state contract

Store only stable, non-secret identifiers in a private, versioned profile. An example (illustrative, not a running configuration):

```json
{
  "schema_version": 2,
  "installation_id": "<random UUID>",
  "device_identity_sha256": "<enrolled SHA-256>",
  "device_model": "<detected model>",
  "auth": {
    "provider": "google",
    "oauth_client_id": "<publisher desktop client ID>",
    "scope": "https://www.googleapis.com/auth/drive.file",
    "credential_reference": "<OS credential-store reference>",
    "google_permission_id": "<stable account identifier>"
  },
  "drive": {
    "parent_folder_id": "<user-selected or app-created parent ID>",
    "backup_folder_id": "<app-created pinned Backup child ID>",
    "archive_folder_id": "<app-created distinct Archive child ID>",
    "backup_relative_path": "Backups",
    "archive_relative_path": "Archive",
    "user_visible_path_hint": "Garmin Sync/Backups"
  },
  "automatic_sync": {"enabled": false}
}
```

State transitions:
`unconfigured -> device_enrolled -> authorization_pending -> authorized -> folder_selected -> bound -> preview_passed -> enabled -> syncing -> verified`.

Invalid/expired/revoked token -> `reauth_required`; different account/device/folder -> `rebind_required`; any unverified preflight -> `blocked`. Blocked states perform zero Drive mutations. No automatic relink, new folder creation, or fallback to another remote on error. A deliberate Google account change follows a previewed `rebind` with rollback.

## 6. Existing users and GORF migration

Current draft PR #37 has an explicit manual rclone/Drive binding. Treat this as a **transitional advanced/manual interface**, not the desired first-run consumer UX. Do not merge/advertise a public release as zero-setup until the wizard provisions the same correct destination credentials end-to-end.

Production GORF may already contain files created by another OAuth client under `Garmin/Fenix8_Backup` and `Garmin/Fenix8_Archive`. Both must resolve under one approved common parent rclone remote rooted at the historical `Garmin` folder. Never replace the existing sibling folders, rename them, or treat their names as security identities. `drive.file` may not see those historical objects simply by naming their folder IDs. Migration must:
- Inventory the real account, folder IDs, existing checksums/file counts and permissions using a READ-ONLY process, without exposing private data to GitHub/CI.
- Reuse historical destinations without moving/replacing folders or modifying archive objects.
- Determine whether `drive.file` can safely access the historical files. **Do not** force scope downgrade, re-upload, or copy data to a new location to hide a permission problem. Keep legacy OAuth mode only as an explicit, documented advanced choice until a safe migration exists.
- Stage dedicated credentials, new profile, service ownership and rollback independently; stop before the first mutation and ask for explicit approval.
- Never let a PR/CI runner with repository write privileges inherit production Google credentials; opt-in owner-only GORF release remains required.

## 7. Qualification gates

No public release / GORF promotion without:
- Unit tests for PKCE/state/nonce, invalid or swapped OAuth callback, refused consent, no refresh token, revoked tokens, scope mismatches and account switching.
- Two isolated Google test accounts with identically named remotes/folders: files never cross tenants, even after retries and offline periods.
- A fresh account can complete first-run end-to-end **without a preexisting rclone config**.
- `drive.file` integration tests cover app-created folders, Google Picker-selected existing folders, app-created children, files created by other OAuth clients, existing archive objects, **same-remote `--backup-dir` archival**, checksums and token refresh.
- Install/uninstall/re-install tests on clean Linux and macOS profiles, including headless behavior and cleanup/rollback.
- Static security assertions: no default `gdrive:`, no Fenix-only device gate, no cloud credentials in artifacts/logs, no release before user-selected destination confirmed.
- Verified publisher-owned Google app branding/scope policy, support URL, privacy policy and production OAuth consent approval appropriate to the final scopes.
- Separate read-only GORF migration qualification and explicit operator approval.

## 8. Archive/destination compatibility invariant

Rclone's documented `--backup-dir` option requires the **same remote** as the copy destination and non-overlapping paths. A configuration such as `backup_remote:` and `archive_remote:` each rooted at unrelated folder IDs is not a valid drop-in replacement. This was discovered while reviewing draft PR #37; the code must be corrected before merge even if offline tests pass.

**Default v1 target contract:** one parent-folder-rooted Drive remote (`garmin-user:`), `garmin-user:Backups` and `garmin-user:Archive/<run-uuid>`. Pin the parent and both child Google folder IDs and revalidate their parent relationships; verify that rclone's path traversal resolves to those same child IDs before any write. Names are user-facing only; IDs are the security anchors. If an operator renames a child or resolves a name collision, block and require explicit repair. Only create folders after Google consent and the user's selected-parent confirmation.

**GORF legacy contract:** a single remote rooted at the historical `Garmin` parent, with `Fenix8_Backup` and `Fenix8_Archive` as existing sibling paths. Preserve both unchanged. The legacy raw-drive scope requires an explicit separate migration review.

**Advanced future mode:** unrelated backup and archive locations require a transactional archival algorithm using Drive IDs and an idempotent durable journal, not `rclone --backup-dir`. It is not in v1.

## 9. Implementation order / ownership

1. Keep #33 and #34 identity-bound storage foundations.
2. Add OAuth publisher, public-client PKCE and local token handling before building the first-run browser UI.
3. Add first-run flow and default app-owned folder creation; integrate Google Picker existing-folder choice and prove `drive.file` semantics using test accounts.
4. Add installation activation, local status UX, OS-secret-store adapter and headless fallback.
5. Complete #35 runner/privilege isolation; extend #36 with cross-account end-to-end tests.
6. Obtain Google publishing approval and qualification evidence. Only then consider public release or explicit, reversible GORF cutover.

**Out of scope for the first release:** cloud-hosted user accounts, Google password handling, Garmin Connect cloud, syncing to unrelated consumer cloud providers, automatic account rebinding, and a promise that every Garmin USB mode is compatible.
