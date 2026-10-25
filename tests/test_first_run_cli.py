"""Mock-only user-choice CLI integration; never opens Google or creates folders."""
import tempfile
from pathlib import Path
import unittest
from unittest import mock

import garmin_sync as sync
import garmin_backup_target as target
import garmin_setup_google as setup


class SetupChoiceTests(unittest.TestCase):
    def test_enter_accepts_suggested_location_without_picker(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(target, "PROFILE_PATH", Path(temp) / "missing.json"), \
             mock.patch("builtins.input", return_value=""), \
             mock.patch.object(setup, "setup_default", return_value={"configured": True}) as run:
            self.assertEqual(sync.main(["setup-google"]), 0)
        self.assertFalse(run.call_args.kwargs["choose_folder"])

    def test_change_uses_picker_and_preserves_user_choice(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(target, "PROFILE_PATH", Path(temp) / "missing.json"), \
             mock.patch("builtins.input", return_value="C"), \
             mock.patch.object(setup, "setup_default", return_value={"configured": True}) as run:
            self.assertEqual(sync.main(["setup-google"]), 0)
        self.assertTrue(run.call_args.kwargs["choose_folder"])

    def test_cancel_refuses_google_authorization(self):
        with tempfile.TemporaryDirectory() as temp, \
             mock.patch.object(target, "PROFILE_PATH", Path(temp) / "missing.json"), \
             mock.patch("builtins.input", return_value="Q"), \
             mock.patch.object(setup, "setup_default") as run:
            self.assertEqual(sync.main(["setup-google"]), 1)
            run.assert_not_called()

    def test_pinned_installation_is_not_automatically_rebound(self):
        with tempfile.TemporaryDirectory() as temp:
            profile = Path(temp) / "installation.json"
            profile.write_text("existing")
            with mock.patch.object(target, "PROFILE_PATH", profile), \
                 mock.patch("builtins.input", side_effect=AssertionError("Prompt forbidden")), \
                 mock.patch.object(setup, "setup_default") as run:
                self.assertEqual(sync.main(["setup-google"]), 1)
                run.assert_not_called()

    def test_choose_and_default_flags_cannot_conflict(self):
        with mock.patch.object(setup, "setup_default") as run:
            self.assertEqual(sync.main(["setup-google", "--use-default", "--choose-folder"]), 1)
            run.assert_not_called()


if __name__ == "__main__":
    unittest.main()
