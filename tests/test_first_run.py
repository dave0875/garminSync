"""First-run default-versus-change tests, independent of host operating system."""
import unittest
from unittest import mock

import garmin_first_run as first


class FirstRunTests(unittest.TestCase):
    def test_default_location_is_human_readable_and_model_neutral(self):
        offer = first.recommend_destination()
        self.assertEqual(offer.parent_display_path, "My Drive / Garmin Sync")
        self.assertEqual(offer.backup_display_path, "My Drive / Garmin Sync / Backups")
        self.assertEqual(offer.archive_display_path, "My Drive / Garmin Sync / Archive")
        self.assertEqual(offer.primary_action, "Use suggested location")
        self.assertEqual(offer.secondary_action, "Change location")
        self.assertNotIn("Fenix", repr(offer))
        self.assertNotIn("gdrive:", repr(offer))

    def test_same_default_on_linux_macos_and_windows(self):
        for platform in ("linux", "darwin", "win32"):
            with self.subTest(platform=platform), mock.patch("sys.platform", platform):
                self.assertEqual(first.recommend_destination().parent_display_path,
                                 "My Drive / Garmin Sync")

    def test_existing_installs_have_no_default_proposal(self):
        self.assertIsNone(first.recommend_destination(True))

    def test_quick_accept_and_change(self):
        for response in ("", "1", "D", "Use", "default"):
            self.assertEqual(first.interpret_destination_choice(response), "default")
        for response in ("2", "C", "Change", "choose"):
            self.assertEqual(first.interpret_destination_choice(response), "picker")
        for response in ("3", "Q", "cancel"):
            self.assertEqual(first.interpret_destination_choice(response), "cancel")

    def test_unrecognized_or_bad_response_does_not_default(self):
        for response in ("maybe", "yes", "foo", None, 0):
            with self.subTest(response=response), self.assertRaises(ValueError):
                first.interpret_destination_choice(response)


if __name__ == "__main__":
    unittest.main()
