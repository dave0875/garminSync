"""Offline tests for destination account/folder pinning. Never touch live Drive."""
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
import uuid

import garmin_backup_target as target
import garmin_sync as sync


BACKUP = "backupFolder123456789"
ARCHIVE = "archiveFolder987654321"
ACCOUNT = "google-permission-123"


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.conf = self.root / "rclone.conf"
        self.profile = self.root / "installation.json"
        self.conf.write_text(
            "[backup]\ntype=drive\nroot_folder_id=" + BACKUP +
            '\ntoken={"access_token":"one"}\n' +
            "[archive]\ntype=drive\nroot_folder_id=" + ARCHIVE +
            '\ntoken={"access_token":"two"}\n'
        )
        self.conf.chmod(0o600)
        self.payload = {
            "schema_version": 1,
            "installation_id": str(uuid.uuid4()),
            "rclone_config": str(self.conf),
            "backup_remote": "backup",
            "archive_remote": "archive",
            "google_permission_id": ACCOUNT,
            "backup_folder_id": BACKUP,
            "archive_folder_id": ARCHIVE,
        }
        self.save_profile()

    def save_profile(self):
        self.profile.write_text(json.dumps(self.payload))
        self.profile.chmod(0o600)

    def test_profile_requires_owner_private_file_and_valid_identity(self):
        self.assertEqual(target.load_profile(self.profile)["google_permission_id"], ACCOUNT)
        self.profile.chmod(0o644)
        with self.assertRaisesRegex(target.BindingError, "private"):
            target.load_profile(self.profile)
        self.profile.chmod(0o600)
        self.payload["backup_folder_id"] = ARCHIVE
        self.save_profile()
        with self.assertRaises(target.BindingError):
            target.load_profile(self.profile)

    def test_config_cannot_be_symlink_or_inherited_global_remote(self):
        link = self.root / "link.conf"
        link.symlink_to(self.conf)
        with self.assertRaises(target.BindingError):
            target.private_file(link)
        self.conf.write_text("[gdrive]\ntype=drive\ntoken=garbage\n")
        with self.assertRaisesRegex(target.BindingError, "Both configured"):
            target.remote_sections(self.conf, "backup", "archive")

    def test_service_account_cannot_silently_bypass_oauth_binding(self):
        with self.conf.open("a") as f:
            f.write("service_account_file=/tmp/other-identity.json\n")
        with self.assertRaises(target.BindingError):
            target.remote_sections(self.conf, "backup", "archive")

    def test_identity_preflight_and_exact_remote_roots(self):
        def fake_inspect(_rclone, path, backup, archive):
            entries = target.remote_sections(path, backup, archive)
            return {name: {"permission_id": ACCOUNT, "email": "fixture@example.com",
                           "folder_id": data["folder_id"]}
                    for name, data in entries.items()}
        with mock.patch.object(target, "inspect_remotes", side_effect=fake_inspect):
            with target.prepared_target("rclone", self.root, self.profile) as bound:
                self.assertEqual(bound["destination"], "backup:")
                self.assertEqual(bound["archive_base"], "archive:")
                self.assertTrue(Path(bound["rclone_options"][1]).is_file())
            self.assertFalse(Path(bound["rclone_options"][1]).exists())

    def test_changed_account_or_folder_never_yields_transfer_target(self):
        for error in ("account", "folder"):
            def fake_inspect(_rclone, _path, _backup, _archive):
                return {
                    "backup": {"permission_id": "wrong" if error == "account" else ACCOUNT,
                               "folder_id": ARCHIVE if error == "folder" else BACKUP},
                    "archive": {"permission_id": ACCOUNT, "folder_id": ARCHIVE},
                }
            with self.subTest(error=error), \
                 mock.patch.object(target, "inspect_remotes", side_effect=fake_inspect):
                with self.assertRaises(target.BindingError):
                    with target.prepared_target("rclone", self.root, self.profile):
                        self.fail("preflight unexpectedly authorized a transfer")

    def test_live_google_api_inspection_is_read_only_and_checks_both_folders(self):
        operations = []
        class Response(io.BytesIO):
            pass
        def urlopen(request, timeout):
            operations.append((request.full_url, request.get_method(), timeout))
            if "/about?" in request.full_url:
                return Response(json.dumps({"user": {
                    "permissionId": ACCOUNT, "emailAddress": "test@example.com"}}).encode())
            folder = request.full_url.split("/files/", 1)[1].split("?", 1)[0]
            return Response(json.dumps({"id": folder, "trashed": False,
                "mimeType": target.DRIVE_DIRECTORY,
                "capabilities": {"canAddChildren": True}}).encode())
        with mock.patch.object(target.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as command, \
             mock.patch.object(target.urllib.request, "urlopen", side_effect=urlopen):
            result = target.inspect_remotes("rclone", self.conf, "backup", "archive")
        self.assertEqual(result["backup"]["permission_id"], ACCOUNT)
        self.assertEqual(result["archive"]["folder_id"], ARCHIVE)
        self.assertEqual(len(operations), 4)
        self.assertTrue(all(method == "GET" for _, method, _ in operations))
        self.assertEqual(command.call_count, 2)
        self.assertTrue(all("--config" in x.args[0] for x in command.call_args_list))

    def test_binding_refuses_expired_credentials_unreachable_api(self):
        with mock.patch.object(target.subprocess, "run",
                               return_value=mock.Mock(returncode=1)):
            with self.assertRaises(target.BindingError):
                target.inspect_remotes("rclone", self.conf, "backup", "archive")

    def test_no_operational_copy_without_private_bound_profile(self):
        state = sync.State(self.root / "state.json")
        fake_runner = mock.Mock()
        with mock.patch.object(sync.backup_target, "prepared_target",
                               side_effect=target.BindingError("unbound")):
            with self.assertRaises(target.BindingError):
                sync.SyncRun(state, runner=fake_runner).execute("/device/GARMIN")
        fake_runner.run.assert_not_called()

    def test_two_profiles_with_identical_remote_aliases_are_isolated(self):
        second = self.root / "second"
        second.mkdir(mode=0o700)
        second_conf = second / "rclone.conf"
        other_backup = "completelyOtherBackup123"
        other_archive = "completelyOtherArchive456"
        second_conf.write_text("[backup]\\ntype=drive\\nroot_folder_id=" +
                               other_backup + '\\ntoken={"access_token":"third"}\\n' +
                               "[archive]\\ntype=drive\\nroot_folder_id=" +
                               other_archive + '\\ntoken={"access_token":"fourth"}\\n')
        # Replace literal escaped newlines with the actual rclone ini syntax.
        second_conf.write_text(second_conf.read_text().replace("\\\\n", "\\n"))
        second_conf.chmod(0o600)
        other_profile = second / "installation.json"
        other = dict(self.payload, installation_id=str(uuid.uuid4()),
                     rclone_config=str(second_conf), google_permission_id="different-principal",
                     backup_folder_id=other_backup, archive_folder_id=other_archive)
        other_profile.write_text(json.dumps(other))
        other_profile.chmod(0o600)

        def inspect(_binary, file, backup_remote, archive_remote):
            sections = target.remote_sections(file, backup_remote, archive_remote)
            account = ("different-principal"
                       if sections[backup_remote]["folder_id"] == other_backup else ACCOUNT)
            return {name: {"permission_id": account, "folder_id": entry["folder_id"]}
                    for name, entry in sections.items()}

        with mock.patch.object(target, "inspect_remotes", side_effect=inspect):
            with target.prepared_target("rclone", self.root, self.profile) as first:
                first_path = first["rclone_options"][1]
                with target.prepared_target("rclone", self.root, other_profile) as next_one:
                    second_path = next_one["rclone_options"][1]
                    self.assertNotEqual(first_path, second_path)
                    self.assertEqual(first["destination"], next_one["destination"])
                    self.assertEqual(first["installation_id"], self.payload["installation_id"])
                    self.assertEqual(next_one["installation_id"], other["installation_id"])
                    self.assertNotEqual(Path(first_path).read_bytes(), Path(second_path).read_bytes())
            self.assertFalse(Path(first_path).exists())
            self.assertFalse(Path(second_path).exists())

        other["google_permission_id"] = ACCOUNT
        other_profile.write_text(json.dumps(other))
        other_profile.chmod(0o600)
        with mock.patch.object(target, "inspect_remotes", side_effect=inspect):
            with self.assertRaises(target.BindingError):
                with target.prepared_target("rclone", self.root, other_profile):
                    self.fail("Wrong Google identity was accepted")

    def test_rclone_folder_retarget_during_preflight_is_detected(self):
        def redirect(_binary, snapshot, _backup, _archive):
            contents = Path(snapshot).read_text().replace(BACKUP, "substitutedFolder12345")
            Path(snapshot).write_text(contents)
            Path(snapshot).chmod(0o600)
            return {"backup": {"permission_id": ACCOUNT, "folder_id": BACKUP},
                    "archive": {"permission_id": ACCOUNT, "folder_id": ARCHIVE}}
        with mock.patch.object(target, "inspect_remotes", side_effect=redirect):
            with self.assertRaisesRegex(target.BindingError, "changed during preflight"):
                with target.prepared_target("rclone", self.root, self.profile):
                    self.fail("Mutated remote escaped verification")

    def test_api_rejects_non_writable_and_missing_folder(self):
        def refuse(request, timeout):
            if "/about?" in request.full_url:
                return io.BytesIO(json.dumps({"user": {"permissionId": ACCOUNT}}).encode())
            return io.BytesIO(json.dumps({"id": BACKUP, "trashed": False,
                "mimeType": target.DRIVE_DIRECTORY,
                "capabilities": {"canAddChildren": False}}).encode())
        with mock.patch.object(target.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \\
             mock.patch.object(target.urllib.request, "urlopen", side_effect=refuse):
            with self.assertRaisesRegex(target.BindingError, "not a writable folder"):
                target.inspect_remotes("rclone", self.conf, "backup", "archive")

    def test_environment_cannot_override_remote_or_credentials(self):
        with mock.patch.dict(os.environ, {"RCLONE_CONFIG": "/tmp/foreign.conf",
                                          "RCLONE_CONFIG_BACKUP_TOKEN": "bad",
                                          "GARMIN_SYNC_ALLOWED": "ok"}):
            result = target.clean_rclone_environment()
        self.assertNotIn("RCLONE_CONFIG", result)
        self.assertNotIn("RCLONE_CONFIG_BACKUP_TOKEN", result)
        self.assertEqual(result["GARMIN_SYNC_ALLOWED"], "ok")

    def test_binding_never_silently_replaces_existing_installation(self):
        with self.assertRaisesRegex(target.BindingError, "already bound"):
            target.bind_profile("rclone", self.conf, "backup", "archive",
                                ACCOUNT, BACKUP, ARCHIVE, self.profile)


if __name__ == "__main__":
    unittest.main()
