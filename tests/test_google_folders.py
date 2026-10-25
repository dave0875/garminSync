"""Offline tests proving folder provisioning requires approval and stays in Drive."""
import io
import json
import unittest
from urllib.parse import parse_qs, urlsplit

import garmin_google_folders as folders

MIME = folders.DRIVE_DIRECTORY


class FolderTests(unittest.TestCase):
    def setUp(self):
        self.posts = []
        self.identities = iter(("containerFolder123456", "backupFolder123456789",
                                "archiveFolder12345678"))

    def fake_api(self, request, timeout=15):
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(urlsplit(request.full_url).path, "/drive/v3/files")
        body = json.loads(request.data)
        self.posts.append(body)
        self.assertEqual(body["mimeType"], MIME)
        self.assertEqual(len(body["parents"]), 1)
        return io.BytesIO(json.dumps({
            "id": next(self.identities), "name": body["name"],
            "mimeType": MIME, "parents": body["parents"],
        }).encode())

    def test_requires_explicit_user_approval(self):
        with self.assertRaises(folders.FolderProvisionError):
            folders.create_consumer_destination("access", user_confirmed=False,
                                                http=self.fake_api)
        self.assertEqual(self.posts, [])

    def test_new_installation_creates_distinct_siblings(self):
        r = folders.create_consumer_destination(
            "access", user_confirmed=True, http=self.fake_api)
        self.assertEqual(r.parent_folder_id, "containerFolder123456")
        self.assertEqual(r.backup_directory, "Backups")
        self.assertEqual(r.archive_directory, "Archive")
        self.assertEqual(self.posts[0]["parents"], ["root"])
        self.assertEqual(self.posts[1]["parents"], [r.parent_folder_id])
        self.assertEqual(self.posts[2]["parents"], [r.parent_folder_id])
        self.assertEqual(len({r.parent_folder_id, r.backup_folder_id,
                              r.archive_folder_id}), 3)
        self.assertTrue(self.posts[0]["name"].startswith("Garmin Sync "))

    def test_picker_selected_parent_creates_subfolder_in_user_location(self):
        user_selected = "chosenParent123456"
        r = folders.create_consumer_destination("access", user_confirmed=True,
                                               selected_parent_id=user_selected,
                                               http=self.fake_api)
        self.assertEqual(self.posts[0]["parents"], [user_selected])
        self.assertEqual(self.posts[1]["parents"], [r.parent_folder_id])

    def test_invalid_selected_parent_stops_before_writes(self):
        with self.assertRaises(ValueError):
            folders.create_consumer_destination(
                "access",user_confirmed=True,selected_parent_id="../../secret",http=self.fake_api)
        self.assertEqual(self.posts, [])

    def test_invalid_metadata_stops_and_does_not_create_children(self):
        def corrupt(request, timeout=15):
            self.posts.append(json.loads(request.data))
            return io.BytesIO(json.dumps({"id":"containerFolder123456",
                                         "name":"Unexpected",
                                         "mimeType":MIME,"parents":["root"]}).encode())
        with self.assertRaises(folders.FolderProvisionError):
            folders.create_consumer_destination("access",user_confirmed=True,http=corrupt)
        self.assertEqual(len(self.posts),1)

    def test_no_access_token_prevents_any_write(self):
        with self.assertRaises(folders.FolderProvisionError):
            folders.create_consumer_destination("",user_confirmed=True,http=self.fake_api)
        self.assertEqual(self.posts,[])


if __name__ == "__main__":
    unittest.main()
