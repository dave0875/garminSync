"""Offline checks for private Google Drive setup provisioning."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit

import garmin_drive_provision as provision
import garmin_google_oauth as oauth
import garmin_backup_target as target


class ProvisionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.token = {
            "scope": oauth.DRIVE_SCOPE,
            "token_type": "Bearer", "expires_in": 3600,
            "access_token": "offline-test-access",
            "refresh_token": "offline-test-refresh",
        }

    def test_explicit_confirmation_required_before_creating_any_folder(self):
        with mock.patch.object(provision, "_drive_json") as call:
            with self.assertRaisesRegex(provision.SetupError, "consent"):
                provision.create_default_layout("token")
            call.assert_not_called()

    def test_default_layout_creates_three_folders_in_correct_parent(self):
        calls = []
        def fake_api(_token, url, payload=None):
            calls.append((url, payload))
            index = len(calls)
            name = payload["name"]
            folder_id = ("uniqueDriveParent001", "uniqueBackupFolder002",
                         "uniqueArchiveFolder003")[index - 1]
            return {"id": folder_id, "name": name,
                    "mimeType": provision.FOLDER_MIME,
                    "parents": payload.get("parents", [])}
        with mock.patch.object(provision, "_drive_json", side_effect=fake_api):
            value = provision.create_default_layout("token", confirmed=True)
        self.assertEqual(len(calls), 3)
        self.assertNotIn("parents", calls[0][1])
        self.assertEqual(calls[1][1]["parents"], [value["parent_folder_id"]])
        self.assertEqual(calls[2][1]["parents"], [value["parent_folder_id"]])
        self.assertNotEqual(value["backup_folder_id"], value["archive_folder_id"])
        self.assertEqual(value["backup_directory"], "Backups")

    def test_selected_parent_creates_only_children_and_requires_writability(self):
        selected = "userSelectedParent12345"
        requests = []
        def fake_api(_token, url, payload=None):
            requests.append((url, payload))
            if payload is None:
                return {"id": selected, "mimeType": provision.FOLDER_MIME,
                        "capabilities": {"canAddChildren": True}}
            name = payload["name"]
            return {"id": {"Backups": "appBackupFolder0001",
                           "Archive": "appArchiveFolder002"}[name],
                    "name": name, "mimeType": provision.FOLDER_MIME,
                    "parents": [selected]}
        with mock.patch.object(provision, "_drive_json", side_effect=fake_api):
            layout = provision.create_selected_layout(
                "token", selected, confirmed=True)
        self.assertEqual(layout["parent_folder_id"], selected)
        self.assertEqual(len(requests), 3)
        self.assertTrue(all(call[1]["parents"] == [selected]
                            for call in requests[1:]))

        def refused(*_args, **_kwargs):
            return {"id": selected, "mimeType": provision.FOLDER_MIME,
                    "capabilities": {"canAddChildren": False}}
        with mock.patch.object(provision, "_drive_json", side_effect=refused):
            with self.assertRaises(provision.SetupError):
                provision.create_selected_layout("token", selected, confirmed=True)

    def test_unsafe_layout_name_is_rejected_without_api_call(self):
        with mock.patch.object(provision, "_drive_json") as call:
            with self.assertRaises(target.BindingError):
                provision.create_default_layout(
                    "token", confirmed=True, backup_name="../secrets")
            call.assert_not_called()

    def test_private_single_remote_config_generated_without_existing_gdrive(self):
        path = self.root / "config" / "rclone.conf"
        result = provision.create_private_rclone_config(
            path, client_id="test-client.apps.googleusercontent.com",
            token_response=self.token, parent_folder_id="uniqueDriveParent001",
            remote="garmin")
        self.assertEqual(result, path)
        self.assertEqual(path.stat().st_mode & 0o077, 0)
        section = target.remote_section(path, "garmin")
        self.assertEqual(section["folder_id"], "uniqueDriveParent001")
        stored = json.loads(section["token"])
        self.assertEqual(stored["refresh_token"], "offline-test-refresh")
        self.assertEqual(target.remote_section(path, "garmin")["folder_id"],
                         "uniqueDriveParent001")
        self.assertNotIn("[gdrive]", path.read_text())
        with self.assertRaises(provision.SetupError):
            provision.create_private_rclone_config(
                path, client_id="test-client.apps.googleusercontent.com",
                token_response=self.token, parent_folder_id="uniqueDriveParent001")

    def test_widened_scope_and_missing_refresh_token_fail_before_file_created(self):
        for patch in ({"scope": "https://www.googleapis.com/auth/drive"},
                      {"refresh_token": ""}):
            with self.subTest(patch=patch):
                path = self.root / "second.conf"
                with self.assertRaises(oauth.OAuthError):
                    provision.create_private_rclone_config(
                        path, client_id="test-client.apps.googleusercontent.com",
                        token_response=dict(self.token, **patch),
                        parent_folder_id="uniqueDriveParent001")
                self.assertFalse(path.exists())

    def test_binding_uses_all_explicit_ids(self):
        layout = {"backup_directory": "Backups", "archive_directory": "Archive",
                  "parent_folder_id": "uniqueDriveParent001",
                  "backup_folder_id": "uniqueBackupFolder002",
                  "archive_folder_id": "uniqueArchiveFolder003"}
        with mock.patch.object(target, "bind_profile",
                               return_value={"installation_id": "test"}) as bind:
            provision.bind_created_layout(
                "rclone", self.root / "conf", layout, "expected-principal",
                profile_path=self.root / "installation.json")
        self.assertEqual(bind.call_args.args[3], "Backups")
        self.assertEqual(bind.call_args.args[5], "expected-principal")
        self.assertEqual(bind.call_args.args[6], "uniqueDriveParent001")


if __name__ == "__main__":
    unittest.main()
