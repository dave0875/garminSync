"""Mock-only onboarding coordinator regressions. No Google account access."""
import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import garmin_google_oauth as oauth
import garmin_setup_google as setup


CLIENT = "publisher-test.apps.googleusercontent.com"
ACCOUNT = {"permission_id": "google-user-identity", "email": "fixture@example.org"}
LAYOUT = {
    "parent_folder_id": "parentFolder012345678",
    "backup_folder_id": "backupFolder123456789",
    "archive_folder_id": "archiveFolder987654321",
    "backup_directory": "Backups",
    "archive_directory": "Archive",
}
TOKEN = {"scope": oauth.DRIVE_SCOPE, "token_type": "Bearer",
         "access_token": "private-access", "refresh_token": "private-refresh",
         "expires_in": 3600}


class SetupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.root.chmod(0o700)
        self.metadata = self.root / "publisher.json"
        self.metadata.write_text(json.dumps({"client_id": CLIENT}))
        self.profile = self.root / "installation.json"
        self.config = self.root / "rclone.conf"

    def invoke(self, confirm):
        return setup.setup_default(
            publisher_path=self.metadata,
            rclone_binary="rclone", rclone_config=self.config,
            profile_path=self.profile, confirm=lambda _: confirm,
            authorize=lambda _: TOKEN)

    def test_missing_publisher_metadata_rejected_before_google_access(self):
        self.metadata.unlink()
        with mock.patch.object(setup.drive, "create_default_layout") as create:
            with self.assertRaises(setup.SetupError):
                self.invoke("CREATE")
            create.assert_not_called()

    def test_user_must_confirm_before_browser_or_folder_creation(self):
        with mock.patch.object(setup.drive, "create_default_layout") as create:
            with self.assertRaisesRegex(setup.SetupError, "cancelled"):
                self.invoke("NO")
            create.assert_not_called()

    def test_preexisting_config_and_profile_fail_before_google_mutation(self):
        self.config.write_text("private")
        with mock.patch.object(setup.drive, "create_default_layout") as create:
            with self.assertRaisesRegex(setup.SetupError, "exists"):
                self.invoke("CREATE")
            create.assert_not_called()
        self.config.unlink()
        self.profile.write_text("existing")
        with self.assertRaisesRegex(setup.SetupError, "Already configured"):
            self.invoke("CREATE")

    def test_happy_path_binds_only_authorized_account_and_folders(self):
        steps = []
        with mock.patch.object(setup, "google_principal",
                               side_effect=lambda *_: (steps.append("account"), ACCOUNT)[1]), \
             mock.patch.object(setup.drive, "create_default_layout",
                               side_effect=lambda *_args, **_kwargs:
                               (steps.append("folders"), LAYOUT)[1]), \
             mock.patch.object(setup.drive, "create_private_rclone_config",
                               side_effect=lambda *_args, **_kwargs:
                               (steps.append("config"), self.config)[1]), \
             mock.patch.object(setup.drive, "bind_created_layout",
                               side_effect=lambda *_args, **_kwargs:
                               (steps.append("bind"), {"installation_id": "uuid"})[1]) as bind:
            output = self.invoke("CREATE")
        self.assertEqual(steps, ["account", "folders", "config", "bind"])
        self.assertEqual(output["account"], ACCOUNT["email"])
        self.assertTrue(output["ready_for_device_enrollment"])
        self.assertNotIn("private-access", str(output))
        self.assertNotIn("private-refresh", str(output))
        self.assertEqual(bind.call_args.args[3], ACCOUNT["permission_id"])

    def test_wrong_auth_scope_fails_before_any_folder_creation(self):
        with mock.patch.object(setup.drive, "create_default_layout") as create:
            with self.assertRaises(oauth.OAuthError):
                setup.setup_default(
                    publisher_path=self.metadata,
                    rclone_binary="rclone", rclone_config=self.config,
                    profile_path=self.profile, confirm=lambda _: "CREATE",
                    authorize=lambda _: dict(TOKEN,
                                             scope="https://www.googleapis.com/auth/drive"))
            create.assert_not_called()


if __name__ == "__main__":
    unittest.main()
