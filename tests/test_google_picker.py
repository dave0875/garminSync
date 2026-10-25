"""No-network desktop Picker OAuth callback and authorization tests."""
import unittest
from urllib.parse import parse_qs, urlencode, urlsplit

import garmin_google_picker as picker
import garmin_google_oauth as oauth

CLIENT = "publisher-test.apps.googleusercontent.com"
FOLDER = "selectedParentFolder123456"


class PickerTests(unittest.TestCase):
    def setUp(self):
        self.attempt = picker.folder_picker_attempt(CLIENT, 38881)

    def callback(self, **overrides):
        params = {"state": self.attempt.state, "scope": oauth.DRIVE_SCOPE,
                  "code": "one-time-code", "picked_file_ids": FOLDER}
        params.update(overrides)
        return "/oauth/callback?" + urlencode(params)

    def test_picker_authorization_uses_desktop_drive_file_scope(self):
        params = parse_qs(urlsplit(self.attempt.url).query)
        self.assertEqual(params["scope"], [oauth.DRIVE_SCOPE])
        self.assertEqual(params["trigger_onepick"], ["true"])
        self.assertEqual(params["allow_folder_selection"], ["true"])
        self.assertEqual(params["allow_multiple"], ["false"])
        self.assertEqual(params["code_challenge_method"], ["S256"])
        self.assertEqual(params["state"], [self.attempt.state])

    def test_single_selected_parent_and_code_accepted(self):
        self.assertEqual(picker.consume_folder_picker_callback(
            self.attempt, self.callback()), ("one-time-code", FOLDER))

    def test_rejects_wrong_scope_csrf_and_multiple_folders(self):
        for data in ({"scope": "https://www.googleapis.com/auth/drive"},
                     {"state": "attacker-state"},
                     {"picked_file_ids": FOLDER + ",otherFolderABC123"},
                     {"picked_file_ids": ""},
                     {"picked_file_ids": "../escape"}):
            with self.subTest(data=data), self.assertRaises(oauth.OAuthError):
                picker.consume_folder_picker_callback(self.attempt,
                                                      self.callback(**data))

    def test_rejects_duplicate_selected_folder_field(self):
        path = self.callback() + "&picked_file_ids=" + FOLDER
        with self.assertRaises(oauth.OAuthError):
            picker.consume_folder_picker_callback(self.attempt, path)


if __name__ == "__main__":
    unittest.main()
