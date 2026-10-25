"""Offline account, sibling-folder and same-rclone-remote tests; no real Drive."""
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

PARENT = "selectedParentFolder123"
BACKUP = "pinnedBackupFolder123"
ARCHIVE = "pinnedArchiveFolder456"
ACCOUNT = "google-permission-123"


class TargetTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.root.chmod(0o700)
        self.config = self.root / "rclone.conf"
        self.profile = self.root / "installation.json"
        self.config.write_text("[userdrive]\ntype=drive\nroot_folder_id=" + PARENT +
                               '\ntoken={"access_token":"testtoken"}\n')
        self.config.chmod(0o600)
        self.binding = {"schema_version": 2, "installation_id": str(uuid.uuid4()),
                        "rclone_config": str(self.config), "remote": "userdrive",
                        "google_permission_id": ACCOUNT, "parent_folder_id": PARENT,
                        "backup_folder_id": BACKUP, "archive_folder_id": ARCHIVE,
                        "backup_path": "Backups", "archive_path": "Archive"}
        self.save_profile()

    def save_profile(self):
        self.profile.write_text(json.dumps(self.binding))
        self.profile.chmod(0o600)

    @staticmethod
    def listing(backup_id=BACKUP, archive_id=ARCHIVE):
        return json.dumps([{"Path": "Backups", "IsDir": True, "ID": backup_id},
                           {"Path": "Archive", "IsDir": True, "ID": archive_id}]).encode()

    def mock_google(self, account=ACCOUNT, backup_name="Backups",
                    archive_name="Archive", parent_for_child=PARENT,
                    writable=True, listing=None):
        calls = []
        def urlopen(request, timeout):
            calls.append((request.full_url, request.get_method()))
            if "/about?" in request.full_url:
                data = {"user": {"permissionId": account, "emailAddress": "test@example.com"}}
            else:
                folder_id = request.full_url.split("/files/")[1].split("?")[0]
                names = {PARENT: "Selected", BACKUP: backup_name, ARCHIVE: archive_name}
                data = {"id": folder_id, "name": names.get(folder_id, "unknown"),
                        "mimeType": target.DRIVE_DIRECTORY, "trashed": False,
                        "parents": [] if folder_id == PARENT else [parent_for_child],
                        "capabilities": {"canAddChildren": writable}}
            return io.BytesIO(json.dumps(data).encode())
        proc = mock.patch.object(target.subprocess, "run", return_value=mock.Mock(
            returncode=0, stdout=self.listing() if listing is None else listing))
        http = mock.patch.object(target.urllib.request, "urlopen", side_effect=urlopen)
        return proc, http, calls

    def test_private_profile_valid_and_no_two_remote_schema(self):
        self.assertEqual(target.load_profile(self.profile)["remote"], "userdrive")
        self.binding["schema_version"] = 1
        self.save_profile()
        with self.assertRaisesRegex(target.BindingError, "v2"):
            target.load_profile(self.profile)
        self.binding["schema_version"] = 2
        self.save_profile()
        self.profile.chmod(0o644)
        with self.assertRaisesRegex(target.BindingError, "private"):
            target.load_profile(self.profile)

    def test_refuse_same_paths_ids_and_path_escape(self):
        for key, value in [
            ("archive_path", "Backups"), ("archive_path", "../elsewhere"),
            ("archive_path", "folder/child"), ("archive_path", " Backups"),
            ("archive_folder_id", BACKUP), ("parent_folder_id", ARCHIVE)]:
            before = dict(self.binding)
            with self.subTest(key=key,value=value):
                self.binding[key] = value
                self.save_profile()
                with self.assertRaises(target.BindingError):
                    target.load_profile(self.profile)
            self.binding = before

    def test_no_global_remote_symlink_or_service_account(self):
        symlink = self.root / "alias"
        symlink.symlink_to(self.config)
        with self.assertRaises(target.BindingError):
            target.private_file(symlink)
        with self.config.open("a") as f:
            f.write("service_account_file=/tmp/unapproved.json\n")
        with self.assertRaisesRegex(target.BindingError, "Unsupported"):
            target.remote_section(self.config, "userdrive")

    def test_verified_paths_share_one_rooted_remote(self):
        proc, http, calls = self.mock_google()
        with proc as process, http:
            with target.prepared_target("rclone", self.root, self.profile) as config:
                self.assertEqual(config["destination"], "userdrive:Backups")
                self.assertEqual(config["archive_base"], "userdrive:Archive")
                self.assertEqual(config["rclone_options"][0], "--config")
                self.assertTrue(Path(config["rclone_options"][1]).exists())
                self.assertTrue(all("lsjson" in args.args[0] for args in process.call_args_list))
                snapshot = config["rclone_options"][1]
        self.assertFalse(Path(snapshot).exists())
        self.assertEqual(len(calls), 4)
        self.assertTrue(all(method == "GET" for _,method in calls))

    def test_bad_account_fails_closed_before_copy(self):
        proc,http,_ = self.mock_google(account="other-account")
        with proc,http, self.assertRaises(target.BindingError):
            with target.prepared_target("rclone", self.root, self.profile):
                self.fail("Wrong principal must not yield target")

    def test_rclone_child_id_mismatch_fails_closed(self):
        proc,http,_ = self.mock_google(listing=self.listing(backup_id="otherChildFolder123"))
        with proc,http, self.assertRaises(target.BindingError):
            with target.prepared_target("rclone", self.root, self.profile):
                self.fail("Wrong child ID accepted")

    def test_child_missing_id_fails_closed(self):
        proc,http,_=self.mock_google(listing=json.dumps([
            {"Path":"Backups", "IsDir": True},
            {"Path":"Archive", "IsDir": True, "ID":ARCHIVE}]).encode())
        with proc,http,self.assertRaisesRegex(target.BindingError,"Cannot prove"):
            target.inspect_target("rclone",self.config,"userdrive","Backups","Archive")

    def test_child_moved_or_renamed_fails_closed(self):
        for values in ({"parent_for_child":"differentParent123"},
                       {"backup_name":"Replaced"}):
            proc,http,_=self.mock_google(**values)
            with self.subTest(values=values),proc,http,self.assertRaises(target.BindingError):
                target.inspect_target("rclone",self.config,"userdrive","Backups","Archive")

    def test_unwritable_folder_fails_closed(self):
        proc,http,_=self.mock_google(writable=False)
        with proc,http,self.assertRaisesRegex(target.BindingError,"writable"):
            target.inspect_target("rclone",self.config,"userdrive","Backups","Archive")

    def test_rclone_root_retarget_fails_closed(self):
        self.config.write_text("[userdrive]\ntype=drive\nroot_folder_id=differentParent123\n"
                               'token={"access_token":"testtoken"}\n')
        proc,http,_=self.mock_google()
        with proc,http,self.assertRaises(target.BindingError):
            with target.prepared_target("rclone",self.root,self.profile):
                self.fail("Repointed root accepted")

    def test_failed_remote_or_identity_api_blocks(self):
        with mock.patch.object(target.subprocess, "run",return_value=mock.Mock(returncode=1)):
            with self.assertRaises(target.BindingError):
                target.inspect_target("rclone",self.config,"userdrive","Backups","Archive")
        proc,http,_=self.mock_google()
        with proc,http:
            with mock.patch.object(target, "_request_json",
                                   side_effect=target.BindingError("API offline")):
                with self.assertRaises(target.BindingError):
                    target.inspect_target("rclone",self.config,"userdrive","Backups","Archive")

    def test_two_users_same_alias_different_accounts_and_folders(self):
        other=self.root/"other"
        other.mkdir(mode=0o700)
        cfg=other/"rclone.conf"
        cfg.write_text("[userdrive]\ntype=drive\nroot_folder_id=anotherParent98765\n"
                       'token={"access_token":"different"}\n')
        cfg.chmod(0o600)
        profile=other/"installation.json"
        second=dict(self.binding,installation_id=str(uuid.uuid4()),
                    rclone_config=str(cfg), google_permission_id="account-B",
                    parent_folder_id="anotherParent98765",
                    backup_folder_id="otherBackup123456",archive_folder_id="otherArchive123456")
        profile.write_text(json.dumps(second))
        profile.chmod(0o600)
        # Isolated inspectors represent real read-only Google probes.
        def inspect(_binary,snapshot,remote,backup,archive):
            p=target.remote_section(snapshot,remote)["parent_id"]
            is_other=p==second["parent_folder_id"]
            return {"permission_id":"account-B" if is_other else ACCOUNT,
                    "parent_folder_id":p,
                    "backup_folder_id":second["backup_folder_id"] if is_other else BACKUP,
                    "archive_folder_id":second["archive_folder_id"] if is_other else ARCHIVE,
                    "backup_path":backup,"archive_path":archive}
        with mock.patch.object(target,"inspect_target",side_effect=inspect):
            with target.prepared_target("rclone",self.root,self.profile) as first:
                with target.prepared_target("rclone",self.root,profile) as second_target:
                    self.assertEqual(first["destination"],second_target["destination"])
                    self.assertNotEqual(first["installation_id"],second_target["installation_id"])
                    self.assertNotEqual(
                        Path(first["rclone_options"][1]).read_bytes(),
                        Path(second_target["rclone_options"][1]).read_bytes())
            second["google_permission_id"]=ACCOUNT
            profile.write_text(json.dumps(second))
            profile.chmod(0o600)
            with self.assertRaises(target.BindingError):
                with target.prepared_target("rclone",self.root,profile):
                    self.fail("Wrong principal should fail")

    def test_no_operational_copy_when_unbound(self):
        runner=mock.Mock()
        with mock.patch.object(target,"prepared_target",side_effect=target.BindingError("unbound")):
            with self.assertRaises(target.BindingError):
                sync.SyncRun(None,runner=runner).execute("/device/GARMIN")
        runner.run.assert_not_called()

    def test_bind_requires_positive_identity_then_prevents_rebind(self):
        self.profile.unlink()
        proc,http,_=self.mock_google()
        with proc,http:
            result=target.bind_profile("rclone",self.config,"userdrive","Backups","Archive",
                                       ACCOUNT,PARENT,BACKUP,ARCHIVE,path=self.profile)
        self.assertEqual(result["parent_folder_id"],PARENT)
        self.assertEqual(target.load_profile(self.profile)["archive_path"],"Archive")
        with self.assertRaisesRegex(target.BindingError,"already bound"):
            target.bind_profile("rclone",self.config,"userdrive","Backups","Archive",
                                ACCOUNT,PARENT,BACKUP,ARCHIVE,path=self.profile)

    def test_ambient_rclone_override_is_removed(self):
        with mock.patch.dict(os.environ,{"RCLONE_CONFIG":"/tmp/unapproved",
                                         "RCLONE_CONFIG_USERDRIVE_ROOT_FOLDER_ID":"other",
                                         "OTHER":"safe"}):
            clean=target.clean_rclone_environment()
        self.assertNotIn("RCLONE_CONFIG",clean)
        self.assertNotIn("RCLONE_CONFIG_USERDRIVE_ROOT_FOLDER_ID",clean)
        self.assertEqual(clean["OTHER"],"safe")

if __name__=="__main__":
    unittest.main()
