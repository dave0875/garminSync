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
        self.assertIsNone(fraction)  # dynamically discovered transfer totals are not final
        progress["phase"] = "inventory"
        self.assertIsNone(ui.progress_summary(progress)[2])

    def test_preview_reports_proposals_not_completed_uploads(self):
        progress = {"phase": "preview", "transferred": 15, "transfers_total": 15,
                    "checked": 350, "checks_total": 900}
        _, counts, fraction = ui.progress_summary(progress)
        self.assertIn("proposed changes", counts)
        self.assertNotIn("files transferred", counts)
        self.assertIsNone(fraction)

    def test_verification_uses_fixed_inventory_and_does_not_signal_done(self):
        progress = {"phase": "verification", "checked": 300, "checks_total": 400,
                    "total_files": 1000}
        self.assertEqual(ui.progress_summary(progress)[2], .3)
        progress["checked"] = 1000
        self.assertEqual(ui.progress_summary(progress)[2], .99)

    def test_session_clock_includes_wait_and_retry_and_freezes_on_done(self):
        clock = ui.SessionClock()
        self.assertEqual(clock.update(True, "attention", 100), 0)
        self.assertEqual(clock.update(True, "syncing", 130), 30)
        self.assertEqual(clock.update(True, "attention", 160), 60)
        self.assertEqual(clock.update(True, "done", 220), 120)
        self.assertEqual(clock.update(True, "done", 250), 120)
        clock.update(False, "waiting", 260)
        self.assertEqual(clock.update(True, "attention", 300), 0)
        self.assertEqual(ui.format_elapsed(3661), "01:01:01")

    def test_clock_restart_recovery_is_labeled(self):
        clock = ui.SessionClock()
        self.assertEqual(clock.update(True, "syncing", 100, 45), 45)
        self.assertTrue(clock.recovered)
        self.assertEqual(clock.update(True, "done", 120), 65)
        self.assertEqual(clock.update(False, "waiting", 130), 65)

    def test_artwork_is_valid_svg_in_all_states(self):
        import xml.etree.ElementTree as ET
        for kind in ("waiting", "attention", "connected", "syncing", "done"):
            for frame in (0, 11, 29):
                root = ET.fromstring(ui.artwork(kind, frame))
                self.assertTrue(root.tag.endswith("svg"))
        self.assertNotEqual(ui.artwork("syncing", 0), ui.artwork("syncing", 1))

    def test_done_timeline_only_completed_after_verification(self):
        self.assertEqual(ui.stage_timeline("done", {"phase": "complete"}), (2, 2, 2, 2, 2))
        self.assertEqual(ui.stage_timeline("syncing", {"phase": "verification"})[-1], 0)

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

    def test_private_error_details_are_not_shown(self):
        state = successful_state()
        state["status"] = "retry_wait"
        state["sync_progress"]["phase"] = "retry_wait"
        state["active_alert"] = {
            "code": "sync_failed",
            "message": "private:/home/dbarker/.config/rclone/rclone.conf token=sensitive",
        }
        message = ui.presentation(state, "fenix_mtp_interface")[2]
        self.assertNotIn("token=", message)
        self.assertNotIn("/home/", message)

    def test_absent_usb_overrides_stale_daemon_mount(self):
        state = successful_state()
        self.assertEqual(ui.presentation(state, "absent")[0], "waiting")

    def test_unplugged_watch_never_reports_done(self):
        state = successful_state()
        state["detected"] = False
        state["mtp_mounted"] = False
        self.assertEqual(ui.presentation(state, "absent")[0], "waiting")


if __name__ == "__main__":
    unittest.main()
