"""Offline desktop companion state-machine and no-false-Done regressions."""
import importlib.util
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import garmin_sync_ui as ui


def successful_state():
    return {
        "status": "success",
        "detected": True,
        "source": "/not-displayed/GARMIN",
        "mtp_mounted": True,
        "connection_token": 4,
        "verified_connection_token": 4,
        "sync_progress": {
            "phase": "complete", "mode": "copy", "run_id": "run4",
            "verified": 917, "copied": 48, "updated_at": "2026-11-25T20:00:00+00:00",
        },
        "last_success_details": {
            "mode": "copy", "verified": 917, "copied": 48,
            "verified_activity": {"run_id": "run4"},
        },
    }


class CompanionStateTests(unittest.TestCase):
    def test_usb_transient_still_prompts_without_mount(self):
        state = {"status": "waiting", "detected": False, "mtp_mounted": False}
        kind, title, message, _, retry = ui.presentation(state, "garmin_transient")
        self.assertEqual(kind, "attention")
        self.assertIn("MTP", title)
        self.assertIn("reconnect", message)
        self.assertFalse(retry)

    def test_mtp_visible_but_not_mounted(self):
        kind, title, message, *_ = ui.presentation(
            {"detected": False, "mtp_mounted": False}, "fenix_mtp_interface")
        self.assertEqual(kind, "attention")
        self.assertIn("mount", message)

    def test_good_current_verified_copy_only(self):
        state = successful_state()
        self.assertTrue(ui.verified_this_connection(state, "fenix_mtp_interface"))
        kind, title, message, fraction, retry = ui.presentation(state, "fenix_mtp_interface")
        self.assertEqual(kind, "done")
        self.assertIn("Done", title)
        self.assertIn("917 files verified", message)
        self.assertEqual(fraction, 1.0)
        self.assertFalse(retry)

    def test_historical_success_does_not_promote_new_connection(self):
        state = successful_state()
        state["connection_token"] = 5
        self.assertFalse(ui.verified_this_connection(state, "fenix_mtp_interface"))
        self.assertNotEqual(ui.presentation(state, "fenix_mtp_interface")[0], "done")

    def test_unmounted_or_transient_never_done(self):
        state = successful_state()
        self.assertFalse(ui.verified_this_connection(state, "garmin_transient"))
        self.assertFalse(ui.verified_this_connection(state, "absent"))
        state["mtp_mounted"] = False
        self.assertFalse(ui.verified_this_connection(state, "fenix_mtp_interface"))

    def test_dry_run_never_done(self):
        state = successful_state()
        state["sync_progress"]["mode"] = "dry-run"
        self.assertFalse(ui.verified_this_connection(state, "fenix_mtp_interface"))

    def test_stale_or_incomplete_run_never_done(self):
        state = successful_state()
        state["last_success_details"]["verified_activity"]["run_id"] = "prior-run"
        self.assertFalse(ui.verified_this_connection(state, "fenix_mtp_interface"))
        state["last_success_details"]["verified_activity"]["run_id"] = "run4"
        state["sync_progress"]["verified"] = 0
        self.assertFalse(ui.verified_this_connection(state, "fenix_mtp_interface"))
        state["sync_progress"]["verified"] = 917
        state["status"] = "retry_wait"
        self.assertFalse(ui.verified_this_connection(state, "fenix_mtp_interface"))

    def test_retry_and_interruption_visible(self):
        state = successful_state()
        state["status"] = "retry_wait"
        state["sync_progress"]["phase"] = "retry_wait"
        state["active_alert"] = {"message": "Backup failed; retry scheduled."}
        view = ui.presentation(state, "fenix_mtp_interface")
        self.assertEqual(view[0], "attention")
        self.assertTrue(view[-1])
        self.assertIn("retry", view[2])

    def test_transfer_fraction_uses_only_measured_stage(self):
        progress = {
            "phase": "copy", "phase_index": 4, "phase_count": 9,
            "transferred": 2, "transfers_total": 8,
            "checked": 10, "checks_total": 25,
        }
        heading, counts, fraction = ui.progress_summary(progress)
        self.assertIn("Step 4/9", heading)
        self.assertIn("2/8", counts)
        self.assertEqual(fraction, .25)
        progress["phase"] = "inventory"
        self.assertIsNone(ui.progress_summary(progress)[2])

    def test_detects_both_usb_modes_without_watch_access(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            watch = root / "1-1"
            watch.mkdir()
            (watch / "idVendor").write_text("091e")
            (watch / "idProduct").write_text("0003")
            self.assertEqual(ui.usb_presence(root), "garmin_transient")
            (watch / "idProduct").write_text("51b5")
            self.assertEqual(ui.usb_presence(root), "fenix_mtp_interface")
            (watch / "idVendor").write_text("ffff")
            self.assertEqual(ui.usb_presence(root), "absent")

    def test_unplugged_watch_never_reports_done(self):
        state = successful_state()
        state["detected"] = False
        state["mtp_mounted"] = False
        self.assertEqual(ui.presentation(state, "absent")[0], "waiting")


if __name__ == "__main__":
    unittest.main()
