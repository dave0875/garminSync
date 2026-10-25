# Garmin Sync installation: advanced transitional procedure

> **DRAFT PR #37. NOT YET PRODUCTION-READY.** This manual setup is a temporary
> engineering interface. The consumer setup wizard and Google sign-in are
> tracked in #39–#43 and designed in
> [ADR-0001](ADR-0001-consumer-onboarding-google-oauth.md).
> Never deploy this branch to GORF without completing the non-destructive
> legacy migration review and isolated-account Drive testing.

## Core behavior

Any supported Garmin exposing a valid, stable ID in \`GarminDevice.xml\` can be
enrolled. Fenix 8 and the historical Google Drive folder names are NOT
required. **The owner chooses any existing writable Google Drive parent
folder.** Under that folder, Garmin Sync uses two sibling child folders for
Backups and Archive. Backups and archive must be in the same rclone remote
because \`rclone copy --backup-dir\` requires that property.

Each installation pins the account's stable Drive permission ID, parent ID,
backup child ID, archive child ID, and exact child folder names. A removed,
recreated, renamed, repointed, unwritable or ambiguously resolved child stops
the sync. A path with a matching name but a different Drive ID cannot be used.

## Advanced manual setup while the consumer wizard is incomplete

1. Authorize a **dedicated**, owner-private rclone OAuth configuration, not a
   shared \`~/.config/rclone/rclone.conf\`. Do not copy tokens from another user.

   \`\`\`bash
   install -d -m 700 "$HOME/.config/garmin-sync"
   rclone --config "$HOME/.config/garmin-sync/rclone.conf" config
   chmod 600 "$HOME/.config/garmin-sync/rclone.conf"
   \`\`\`

2. In that configuration create **one** Google Drive remote (for example
   \`my-garmin\`) with \`root_folder_id\` pointing to the selected existing
   **parent folder**. The existing \`Backups\` and \`Archive\` children must
   already exist under that parent. Choose any safe single-component names,
   but the names must differ. Record the three folder IDs via Google Drive's
   folder URLs. The remotes must be OAuth Drive, not service-account or
   shared-drive remotes. Do not use \`gdrive:\` from another profile.

3. Inspect without transferring bytes. This outputs Google account identity
   and three verified Drive folder IDs:

   \`\`\`bash
   python3 garmin_sync.py inspect-target \
     --rclone-config "$HOME/.config/garmin-sync/rclone.conf" \
     --remote my-garmin --backup-path Backups --archive-path Archive
   \`\`\`

4. After reviewing the results, bind the installation explicitly:

   \`\`\`bash
   python3 garmin_sync.py bind-target \
     --rclone-config "$HOME/.config/garmin-sync/rclone.conf" \
     --remote my-garmin --backup-path Backups --archive-path Archive \
     --google-account-id "<VERIFIED_PERMISSION_ID>" \
     --parent-folder-id "<VERIFIED_PARENT_ID>" \
     --backup-folder-id "<VERIFIED_BACKUP_ID>" \
     --archive-folder-id "<VERIFIED_ARCHIVE_ID>"
   \`\`\`

   \`~/.config/garmin-sync/installation.json\` is generated privately with a
   new installation UUID. \`schema_version: 2\` replaces the obsolete
   two-independent-remote v1 schema. There is **no automatic v1 promotion**
   because its archive contract was invalid.

5. Connect the target Garmin, enroll its identity, and first run a preview:

   \`\`\`bash
   python3 garmin_sync.py enroll
   python3 garmin_sync.py once --dry-run
   python3 garmin_sync.py once
   python3 garmin_sync.py verify
   \`\`\`

No account, selected Drive folder, archive folder or watch identity is ever
silently rebound. Missing credentials or a failed account/folder probe blocks
all Drive mutations. A \`once --dry-run\` is a preview, **not** proof of backup.
Only a successful copy plus checksum check qualifies as verified.

## Historic GORF migration

The existing data locations are \`Garmin/Fenix8_Backup\` and
\`Garmin/Fenix8_Archive\`. Their existing parent \`Garmin\` must be the
single \`root_folder_id\`. The child paths must be **exactly**
\`Fenix8_Backup\` and \`Fenix8_Archive\`, pinned to their original IDs.
No folder may be moved, renamed, created as a substitute, cleared or
re-uploaded for migration.

GORF may use legacy Google OAuth scopes. \`drive.file\` is for **new** users;
it does not necessarily grant access to existing foreign-created Drive files.
Keep the legacy credential scope until read-only inventory, account identity,
archive, device data, and rollback are independently qualified. Do not
run this migration through CI or a repo-connected runner with production tokens.
Owner-only deployment is manual and must follow that independent qualification.

The consumer release will replace the manual rclone setup with Google's
browser consent, secure local credential handling, automatic folder creation,
Google Picker and OS-appropriate service activation. **None of those external
publishing requirements is satisfied merely by this manual guide.**
