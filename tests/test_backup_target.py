"""Offline account/folder isolation tests. Never use personal Drive credentials."""
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock
import urllib.parse
import uuid

import garmin_backup_target as target
import garmin_sync as sync

PARENT = "parentFolder012345678"
BACKUP = "backupFolder123456789"
ARCHIVE = "archiveFolder987654321"
ACCOUNT = "google-permission-123"
BACKUP_NAME = "Watch Backup"
ARCHIVE_NAME = "Retained History"


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.conf = self.root / "rclone.conf"
        self.profile = self.root / "installation.json"
        self.conf.write_text(
            "[userdrive]\ntype=drive\nroot_folder_id=" + PARENT +
            '\ntoken={"access_token":"testing"}\n')
        self.conf.chmod(0o600)
        self.payload = {
            "schema_version": 2,
            "installation_id": str(uuid.uuid4()),
            "rclone_config": str(self.conf),
            "remote": "userdrive",
            "google_permission_id": ACCOUNT,
            "parent_folder_id": PARENT,
            "backup_folder_id": BACKUP,
            "archive_folder_id": ARCHIVE,
            "backup_directory": BACKUP_NAME,
            "archive_directory": ARCHIVE_NAME,
        }
        self.save_profile()

    def save_profile(self):
        self.profile.write_text(json.dumps(self.payload))
        self.profile.chmod(0o600)

    def observed(self, **changes):
        return dict({
            "permission_id": ACCOUNT, "parent_folder_id": PARENT,
            "backup_folder_id": BACKUP, "archive_folder_id": ARCHIVE,
            "email": "fixture@example.org",
        }, **changes)

    def test_requires_private_and_v2_profile(self):
        self.assertEqual(target.load_profile(self.profile)["remote"], "userdrive")
        self.profile.chmod(0o644)
        with self.assertRaises(target.BindingError):
            target.load_profile(self.profile)
        self.profile.chmod(0o600)
        self.payload["schema_version"] = 1
        self.save_profile()
        with self.assertRaisesRegex(target.BindingError, "version"):
            target.load_profile(self.profile)

    def test_rejects_unsafe_child_names(self):
        for value in ("", ".", "../escape", "top/under", "bad\\dir", "x\nwrong", " "):
            with self.subTest(value=value):
                with self.assertRaises(target.BindingError):
                    target.valid_child(value)

    def test_no_folder_id_overlap_or_same_path(self):
        for replacement in (("archive_folder_id", BACKUP),
                            ("archive_directory", BACKUP_NAME.lower())):
            key, bad = replacement
            original = self.payload[key]
            self.payload[key] = bad
            self.save_profile()
            with self.assertRaises(target.BindingError):
                target.load_profile(self.profile)
            self.payload[key] = original
        self.save_profile()

    def test_symlink_credential_and_ambient_gdrive_rejected(self):
        link = self.root / "outside.conf"
        link.symlink_to(self.conf)
        with self.assertRaises(target.BindingError):
            target.private_file(link)
        with mock.patch.dict(os.environ, {"RCLONE_CONFIG": "/tmp/foreign.conf",
                                          "RCLONE_CONFIG_USERDRIVE_TOKEN": "foreign"}):
            result = target.clean_rclone_environment()
        self.assertNotIn("RCLONE_CONFIG", result)
        self.assertNotIn("RCLONE_CONFIG_USERDRIVE_TOKEN", result)

    def test_unsupported_remote_account_mode_rejected(self):
        with self.conf.open("a") as handle:
            handle.write("service_account_file=/tmp/another-person.json\n")
        with self.assertRaises(target.BindingError):
            target.remote_section(self.conf, "userdrive")

    def test_approved_target_uses_same_remote_and_private_snapshot(self):
        with mock.patch.object(target, "inspect_remote", return_value=self.observed()):
            with target.prepared_target("rclone", self.root, self.profile) as bound:
                self.assertEqual(bound["destination"], "userdrive:" + BACKUP_NAME)
                self.assertEqual(bound["archive_base"], "userdrive:" + ARCHIVE_NAME)
                self.assertEqual(bound["destination"].split(":")[0],
                                 bound["archive_base"].split(":")[0])
                snapshot = Path(bound["rclone_options"][1])
                self.assertTrue(snapshot.is_file())
                self.assertNotEqual(snapshot, self.conf)
            self.assertFalse(snapshot.exists())

    def test_parent_root_mismatch_refuses_before_inspection(self):
        self.conf.write_text(
            "[userdrive]\ntype=drive\nroot_folder_id=anotherParent012345678\n"
            'token={"access_token":"testing"}\n')
        self.conf.chmod(0o600)
        with mock.patch.object(target, "inspect_remote") as inspect:
            with self.assertRaisesRegex(target.BindingError, "parent"):
                with target.prepared_target("rclone", self.root, self.profile):
                    self.fail("incorrect Drive root was approved")
            inspect.assert_not_called()

    def test_account_and_each_folder_mismatch_fail_closed(self):
        for change in ({"permission_id": "wrong"}, {"parent_folder_id": "foreign"},
                       {"backup_folder_id": ARCHIVE}, {"archive_folder_id": BACKUP}):
            with self.subTest(change=change), \
                 mock.patch.object(target, "inspect_remote",
                                   return_value=self.observed(**change)):
                with self.assertRaises(target.BindingError):
                    with target.prepared_target("rclone", self.root, self.profile):
                        self.fail("foreign account/folder granted")

    def test_bad_google_api_does_not_authorize_cloud_write(self):
        state = sync.State(self.root / "state.json")
        fake_runner = mock.Mock()
        with mock.patch.object(sync.backup_target, "prepared_target",
                               side_effect=target.BindingError("Google unavailable")):
            with self.assertRaises(target.BindingError):
                sync.SyncRun(state, runner=fake_runner).execute("/fake/GARMIN")
        fake_runner.run.assert_not_called()

    def test_google_metadata_inspection_rejects_duplicate_name_and_wrong_parent(self):
        class Response(io.BytesIO):
            pass

        def api(request, timeout=15):
            url = urllib.parse.urlparse(request.full_url)
            params = urllib.parse.parse_qs(url.query)
            if url.path.endswith("/about"):
                result = {"user": {"permissionId": ACCOUNT,
                                   "emailAddress": "fixture@example.org"}}
            elif url.path.endswith("/files"):
                q = params["q"][0]
                if BACKUP_NAME in q:
                    entries = [{"id": BACKUP, "name": BACKUP_NAME},
                               {"id": "collidingOtherFolder"}]
                else:
                    entries = [{"id": ARCHIVE, "name": ARCHIVE_NAME}]
                result = {"files": entries}
            else:
                folder = url.path.split("/files/", 1)[1]
                name = {BACKUP: BACKUP_NAME, ARCHIVE: ARCHIVE_NAME}.get(folder, "parent")
                result = {"id": folder, "name": name, "trashed": False,
                          "mimeType": target.DRIVE_DIRECTORY,
                          "parents": [PARENT], "capabilities": {"canAddChildren": True}}
            return Response(json.dumps(result).encode())

        with mock.patch.object(target.subprocess, "run",
                               return_value=mock.Mock(returncode=0)), \
             mock.patch.object(target.urllib.request, "urlopen", side_effect=api):
            with self.assertRaisesRegex(target.BindingError, "Ambiguous"):
                target.inspect_remote("rclone", self.conf, "userdrive",
                                      BACKUP_NAME, ARCHIVE_NAME, BACKUP, ARCHIVE)

    def test_google_metadata_inspection_happy_path(self):
        requests = []
        def fake_json(url, token):
            requests.append(url)
            if "/about?" in url:
                return {"user": {"permissionId": ACCOUNT, "emailAddress": "ok@example.org"}}
            if "/files?" in url:
                q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["q"][0]
                return {"files": [{"id": BACKUP if BACKUP_NAME in q else ARCHIVE}]}
            folder = url.split("/files/", 1)[1].split("?", 1)[0]
            return {"id": folder, "name": {BACKUP: BACKUP_NAME,
                     ARCHIVE: ARCHIVE_NAME}.get(folder, "parent"),
                    "mimeType": target.DRIVE_DIRECTORY,
                    "parents": [PARENT], "trashed": False,
                    "capabilities": {"canAddChildren": True}}
        with mock.patch.object(target.subprocess, "run",
                               return_value=mock.Mock(returncode=0)) as cmd, \
             mock.patch.object(target, "_request_json", side_effect=fake_json):
            result = target.inspect_remote("rclone", self.conf, "userdrive",
                                          BACKUP_NAME, ARCHIVE_NAME, BACKUP, ARCHIVE)
        self.assertEqual(result["permission_id"], ACCOUNT)
        self.assertEqual(result["parent_folder_id"], PARENT)
        self.assertEqual(len(requests), 6)
        self.assertEqual(cmd.call_count, 1)

    def test_two_identically_named_remotes_have_independent_installation_binding(self):
        second = self.root / "second"
        second.mkdir(mode=0o700)
        conf = second / "config"
        new_parent = "independentParent123456"
        conf.write_text("[userdrive]\ntype=drive\nroot_folder_id=" + new_parent +
                        '\ntoken={"access_token":"independent"}\n')
        conf.chmod(0o600)
        other = dict(self.payload, installation_id=str(uuid.uuid4()),
                     google_permission_id="other-google-id", rclone_config=str(conf),
                     parent_folder_id=new_parent,
                     backup_folder_id="anotherBackup123456",
                     archive_folder_id="anotherArchive123456")
        profile = second / "installation.json"
        profile.write_text(json.dumps(other))
        profile.chmod(0o600)
        def inspect(_rclone, config, *args):
            if target.remote_section(config, "userdrive")["folder_id"] == new_parent:
                return {"permission_id": "other-google-id", "parent_folder_id": new_parent,
                        "backup_folder_id": other["backup_folder_id"],
                        "archive_folder_id": other["archive_folder_id"]}
            return self.observed()
        with mock.patch.object(target, "inspect_remote", side_effect=inspect):
            with target.prepared_target("rclone", self.root, self.profile) as first:
                with target.prepared_target("rclone", self.root, profile) as other_bound:
                    self.assertNotEqual(first["installation_id"], other_bound["installation_id"])
                    self.assertNotEqual(
                        Path(first["rclone_options"][1]).read_bytes(),
                        Path(other_bound["rclone_options"][1]).read_bytes())


@unittest.skipUnless(Path(sync.RCLONE).exists(), "rclone unavailable")
class SameRemoteArchiveFixture(unittest.TestCase):
    def test_rclone_backup_dir_preserves_overwritten_file_in_sibling(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            source = root / "watch"
            destination = root / "drive" / "Backups"
            archive = root / "drive" / "Archive" / "run-1"
            (source / "Activity").mkdir(parents=True)
            destination.joinpath("Activity").mkdir(parents=True)
            source.joinpath("Activity", "one.fit").write_bytes(b"new version")
            destination.joinpath("Activity", "one.fit").write_bytes(b"old version")
            config = root / "local-rclone.conf"
            config.write_text("[localuser]\ntype=local\n")
            subprocess.run([sync.RCLONE, "copy", str(source), "localuser:Backups",
                            "--backup-dir", "localuser:Archive/run-1", "--config",
                            str(config), "--checksum"], check=True,
                           env={**target.clean_rclone_environment()},
                           capture_output=True, cwd=root / "drive")
            self.assertEqual(destination.joinpath("Activity", "one.fit").read_bytes(),
                             b"new version")
            self.assertEqual(archive.joinpath("Activity", "one.fit").read_bytes(),
                             b"old version")


if __name__ == "__main__":
    unittest.main()
