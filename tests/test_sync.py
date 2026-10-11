#!/usr/bin/python3
"""Controller regressions and rclone fixtures, using only temporary local backups."""
import datetime as dt
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock
import garmin_sync as sync

XML = "<Device><Model><Description>FENIX  8 - 51mm, Solar</Description></Model><Id>fixture</Id></Device>"
DEVICE = {"root": "/fixture/GARMIN", "model": "fenix 8", "identity": "fixture"}

class Worker:
    def __init__(self): self.cancelled = False
    def cancel(self): self.cancelled = True

class ControllerTests(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch.object(sync, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = sync.State(Path(self.temp.name) / "state.json")
        self.clock, self.calls = 0, []
        def launch(source, mode, epoch, complete):
            worker = Worker()
            self.calls.append((source, mode, epoch, complete, worker))
            return worker
        self.control = sync.Controller(self.state, launch, clock=lambda: self.clock)
    def finish(self, error=None):
        _, mode, epoch, complete, _ = self.calls[-1]
        complete(epoch, mode, {"considered": 7, "copied": 2, "verified": 7,
                               "archive": "fixture", "fit_warnings": []}, error)
    def test_restart_does_not_trust_stale_detected_or_success_flags(self):
        self.state.update(detected=True, source=DEVICE["root"], status="success", last_success="previous")
        control = sync.Controller(self.state, lambda *_: Worker())
        self.assertFalse(self.state.snapshot()["detected"])
        self.assertEqual(self.state.snapshot()["status"], "waiting")
        self.assertEqual(self.state.snapshot()["last_success"], "previous")
        self.assertFalse(control.successful)
    def test_absent_success_remain_disconnect_reconnect(self):
        self.control.observe(None)
        self.assertFalse(self.calls)
        self.control.observe(DEVICE)
        self.finish()
        self.control.observe(dict(DEVICE))
        self.clock += 10000
        self.control.tick()
        self.assertEqual(len(self.calls), 1)
        self.control.observe(None)
        self.assertFalse(self.state.snapshot()["detected"])
        self.control.observe(DEVICE)
        self.assertEqual(len(self.calls), 2)
    def test_interrupted_stale_completion_and_rapid_reconnect(self):
        self.control.observe(DEVICE)
        epoch = self.control.epoch
        self.control.observe(None)
        self.assertTrue(self.calls[-1][-1].cancelled)
        self.assertFalse(self.state.update(token=epoch, status="success"))
        self.control.observe(DEVICE)
        self.assertEqual(len(self.calls), 1)
        self.finish()
        self.assertEqual(len(self.calls), 2)
        self.assertIsNone(self.state.snapshot()["last_success"])
    def test_failures_exponential_retry_and_forced_failure_after_success(self):
        self.control.observe(DEVICE)
        for delay in (120, 240, 480, 900, 900):
            count = len(self.calls)
            self.finish(sync.CommandFailed("fixture failure"))
            self.assertEqual(self.state.snapshot()["retry_after_seconds"], delay)
            self.clock += delay - 1
            self.control.tick()
            self.assertEqual(len(self.calls), count)
            self.clock += 1
            self.control.tick()
            self.assertEqual(len(self.calls), count + 1)
        self.finish()
        success = self.state.snapshot()["last_success"]
        details = self.state.snapshot()["last_success_details"]
        self.control.request("copy")
        self.finish(sync.CommandFailed("forced failure"))
        self.assertFalse(self.control.successful)
        self.assertEqual(self.state.snapshot()["last_success"], success)
        self.assertEqual(self.state.snapshot()["last_success_details"], details)
        count = len(self.calls)
        self.clock += 120
        self.control.tick()
        self.assertEqual(len(self.calls), count + 1)
    def test_deferred_signals_never_overlap_and_shutdown_cancels(self):
        self.control.observe(DEVICE)
        self.control.request("dry-run")
        self.assertEqual(len(self.calls), 1)
        self.finish()
        self.assertEqual(self.calls[-1][1], "dry-run")
        self.control.request("copy")
        self.finish()
        self.assertEqual(self.calls[-1][1], "copy")
        self.control.stop()
        self.assertTrue(self.calls[-1][-1].cancelled)
        count = len(self.calls)
        self.finish()
        self.assertEqual(len(self.calls), count)


class SyncHealthTests(unittest.TestCase):
    def setUp(self):
        self.at = dt.datetime(2026, 10, 11, 21, 30, tzinfo=dt.timezone.utc)
        self.recent = (self.at - dt.timedelta(hours=1)).isoformat()
        self.stale = (self.at - dt.timedelta(days=3)).isoformat()

    def health(self, **fields):
        state = {"status": "waiting", "detected": False, "last_success": None}
        state.update(fields)
        return sync.sync_health(state, True, current_time=self.at)

    def test_disconnected_healthy_daemon_is_not_verified_backup(self):
        health = self.health(status="waiting", last_success=self.stale)
        self.assertEqual(health["condition"], "waiting_for_device")
        self.assertEqual(health["device_availability"], "not_mounted")
        self.assertEqual(health["backup_freshness"], "stale")
        self.assertEqual(health["last_verified_backup_age_seconds"], 3 * 86400)

    def test_connected_preverification_and_active_copy_not_success(self):
        self.assertEqual(self.health(detected=True, status="connected",
                                     last_success=self.recent)["condition"],
                         "awaiting_verification")
        self.assertEqual(self.health(detected=True, status="copy")["condition"], "syncing")
        self.assertEqual(self.health(detected=True, status="verifying")["condition"], "syncing")

    def test_only_verified_completed_run_reports_verified(self):
        health = self.health(detected=True, status="success", last_success=self.recent)
        self.assertEqual(health["condition"], "verified")
        self.assertEqual(health["backup_freshness"], "recent")
        self.assertEqual(health["last_verified_backup_age_seconds"], 3600)

    def test_retry_failure_blocked_and_service_down(self):
        for phase in ("retry_wait", "failed", "blocked"):
            health = self.health(status=phase, detected=True, last_error="discovery failed")
            self.assertEqual(health["condition"], "degraded")
            self.assertEqual(health["last_error"], "discovery failed")
        self.assertEqual(sync.sync_health({"status": "waiting"}, False)["condition"],
                         "service_unavailable")
        self.assertEqual(sync.sync_health({"status": "unenrolled"}, True, enrolled=False)
                         ["condition"], "unenrolled")

    def test_invalid_historical_timestamp_has_unknown_freshness(self):
        result = self.health(last_success="previous")
        self.assertIsNone(result["last_verified_backup_age_seconds"])
        self.assertEqual(result["backup_freshness"], "unknown")

    def test_status_reports_separate_operational_and_backup_health(self):
        payload = {"service": {"ActiveState": "active", "MainPID": "1"},
                   "state": {"status": "waiting", "detected": False,
                             "last_success": self.stale},
                   "build": {"commit": "test"}, "enrollment": {"identity": "hash"}}
        with mock.patch.object(sync, "readiness", return_value=(True, payload)), \
             mock.patch("builtins.print") as printed:
            self.assertEqual(sync.main(["status"]), 0)
        output = json.loads(printed.call_args.args[0])
        self.assertTrue(output["healthy"])
        self.assertEqual(output["sync_health"]["condition"], "waiting_for_device")


class DiscoveryTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "sys"
        self.root.mkdir()

    def device(self, name, vendor, product):
        node = self.root / name
        node.mkdir()
        (node / "idVendor").write_text(vendor)
        (node / "idProduct").write_text(product)
        return node

    def test_absent_preboot_fenix_and_other_device_are_separate(self):
        self.assertEqual(sync.linux_garmin_usb_probe(self.root), "absent")
        self.device("other", "046d", "c52b")
        self.assertEqual(sync.linux_garmin_usb_probe(self.root), "absent")
        preboot = self.device("preboot", "091e", "0003")
        self.assertEqual(sync.linux_garmin_usb_probe(self.root), "garmin_preboot_usb")
        (preboot / "idProduct").write_text("51b5")
        self.assertEqual(sync.linux_garmin_usb_probe(self.root), "fenix_usb")

    def test_unavailable_probe_does_not_claim_watch_is_absent(self):
        self.assertEqual(sync.linux_garmin_usb_probe(self.root / "missing"), "unavailable")
        with mock.patch.object(sync, "IS_DARWIN", True):
            self.assertEqual(sync.linux_garmin_usb_probe(self.root), "unsupported")

    def test_observation_is_transition_only_and_measures_real_recovery(self):
        state = sync.State(self.root / "state.json")
        seconds = [0.0]
        events = []
        with mock.patch.object(sync, "log", side_effect=lambda event, **fields:
                               events.append((event, fields))):
            tracker = sync.DiscoveryTelemetry(
                state, clock=lambda: seconds[0], timestamp=lambda: "2026-10-11T21:00:00+00:00")
            tracker.observe("absent", False)
            self.assertEqual(state.snapshot()["discovery_state"], "not_mounted")
            tracker.observe("fenix_usb", False)
            seconds[0] = 5.0
            tracker.observe("fenix_usb", False)
            self.assertEqual(len(events), 2)
            self.assertEqual(state.snapshot()["discovery_transitions"], 2)
            self.assertEqual(sync.sync_health({"status": "waiting", "detected": False,
                                              **state.snapshot()}, True)["condition"],
                             "awaiting_mtp_mount")
            seconds[0] = 9.5
            tracker.observe("fenix_usb", True)
            self.assertEqual(state.snapshot()["usb_to_mtp_recovery_seconds"], 9.5)
            self.assertEqual(state.snapshot()["discovery_state"], "mtp_mounted")
            self.assertEqual(len(events), 3)
            tracker.observe("absent", False)
            self.assertEqual(state.snapshot()["discovery_state"], "not_mounted")
            self.assertEqual(state.snapshot()["discovery_transitions"], 4)

class BasicTests(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch.object(sync, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
    def test_discovery_never_stats_unrelated_automounts(self):
        unrelated = Path("/media/fixture/unrelated-volume")
        with mock.patch.object(Path, "iterdir", return_value=iter([unrelated])), \
             mock.patch.object(Path, "is_dir", side_effect=AssertionError("No stat permitted")), \
             mock.patch.object(sync, "model_at", side_effect=AssertionError("No XML probing permitted")):
            self.assertIsNone(sync.discover(uid=9999, username="fixture"))
        self.assertFalse(sync.garmin_hint("unrelated-volume"))
        self.assertTrue(sync.garmin_hint("mtp:host=091e_51b5_fixture"))
        self.assertTrue(sync.garmin_hint("GARMIN"))
    def test_gio_mounts_filter_unrelated_disks_before_discovery(self):
        unrelated, device = mock.Mock(), mock.Mock()
        for mount, path, uri, name in ((unrelated, "/media/user/disk", "file:" + "///media/user/disk", "disk"),
                                      (device, "/run/user/1000/gvfs/mtp:host=091e_fixture", "mtp:" + "//091e_fixture/", "mtp")):
            mount.get_name.return_value = name
            mount.get_root.return_value.get_path.return_value = path
            mount.get_root.return_value.get_uri.return_value = uri
        self.assertEqual(sync.native_mount_paths([unrelated, device]), ["/run/user/1000/gvfs/mtp:host=091e_fixture"])
    def test_mtp_inventory_parser_preserves_native_files_and_exclusions(self):
        tree = """libmtp version: 1.1.21
Device: fenix 8
Storage: Internal Storage
1 GARMIN
  2 GarminDevice.xml
  3 Activity
    4 2026-10-05.fit
  5 Debug
    6 MetricsBackup
      7 metrics.fit
  8 Maps
    9 basemap.fit
  10 Music
    11 song.fit
  12 MAP.IMG
  13 state.bin
OK.
"""
        def record(object_id, size, parent):
            return (f"File ID: {object_id}\n Filename: fixture\n File size {size} "
                    f"(0x00000000) bytes\n Parent ID: {parent}\n"
                    " Storage ID: 0x00020001\n Filetype: Unknown filetype\n")
        files = "".join([
            record(2, 100, 1), record(4, 200, 3), record(7, 300, 6),
            record(9, 400, 8), record(11, 500, 10), record(12, 600, 1),
            record(13, 700, 1),
        ])
        inventory = sync.mtp_selected_inventory(tree, files)
        self.assertEqual(
            [(item["id"], item["path"], item["size"]) for item in inventory],
            [(4, "Activity/2026-10-05.fit", 200),
             (7, "Debug/MetricsBackup/metrics.fit", 300),
             (2, "GarminDevice.xml", 100),
             (13, "state.bin", 700)])
        self.assertEqual(sync.parse_mtp_filetree(tree)[7],
                         "GARMIN/Debug/MetricsBackup/metrics.fit")

    def test_real_fenix_libmtp_whitespace_fixture(self):
        nbsp = "\u00a0"
        tree = "\n".join([
            "Device: fenix 8 - 51mm",
            "Storage: Internal Storage",
            "16777216 GARMIN",
            f"{nbsp}{nbsp}16777226 Debug",
            f"{nbsp}{nbsp}{nbsp}{nbsp}16777348 Uploads",
            f"{nbsp}{nbsp}16777232 Activity",
            f"{nbsp}{nbsp}{nbsp}{nbsp}16777505 2025-12-15-08-09-10.fit",
            f"{nbsp}{nbsp}16777243 Monitor",
            f"{nbsp}{nbsp}{nbsp}{nbsp}16777850 MA500000.FIT",
            f"{nbsp}{nbsp}16777277 GarminDevice.xml",
            "OK.",
        ])
        files = "\n".join([
            "File ID: 16777505",
            f"{nbsp}{nbsp}{nbsp} Filename: 2025-12-15-08-09-10.fit",
            f"{nbsp}{nbsp}{nbsp} File size 2048 (0x0000000000000800) bytes",
            f"{nbsp}{nbsp}{nbsp} Parent ID: 16777232",
            f"{nbsp}{nbsp}{nbsp} Storage ID: 0x00020001",
            "File ID: 16777850",
            f"{nbsp}{nbsp}{nbsp} Filename: MA500000.FIT",
            f"{nbsp}{nbsp}{nbsp} File size 4096 (0x0000000000001000) bytes",
            f"{nbsp}{nbsp}{nbsp} Parent ID: 16777243",
            f"{nbsp}{nbsp}{nbsp} Storage ID: 0x00020001",
            "File ID: 16777277",
            f"{nbsp}{nbsp}{nbsp} Filename: GarminDevice.xml",
            f"{nbsp}{nbsp}{nbsp} File size 65513 (0x000000000000FFE9) bytes",
            f"{nbsp}{nbsp}{nbsp} Parent ID: 16777216",
            f"{nbsp}{nbsp}{nbsp} Storage ID: 0x00020001",
        ])
        inventory = sync.mtp_selected_inventory(tree, files)
        self.assertEqual(
            [(item["id"], item["path"], item["size"]) for item in inventory],
            [(16777505, "Activity/2025-12-15-08-09-10.fit", 2048),
             (16777277, "GarminDevice.xml", 65513),
             (16777850, "Monitor/MA500000.FIT", 4096)])
        self.assertEqual(
            sync.parse_mtp_filetree(tree)[16777505],
            "GARMIN/Activity/2025-12-15-08-09-10.fit")
        self.assertEqual(sync.parse_mtp_files(files)[16777277]["parent_id"], 16777216)

    def test_mtp_download_batches_verify_sizes_before_materializing(self):
        entries = [
            {"id": 20, "path": "Activity/run.fit", "size": 3},
            {"id": 21, "path": "Debug/MetricsBackup/metric.fit", "size": 4},
        ]
        sizes = {item["id"]: item["size"] for item in entries}
        class FakeRunner:
            def __init__(self):
                self.calls = []
            def run(self, args, *_args, **_kwargs):
                self.calls.append(args)
                for index, value in enumerate(args):
                    if value == "--getfile":
                        object_id, target = args[index + 1].split(",", 1)
                        Path(target).write_bytes(b"x" * sizes[int(object_id)])
                return ""
        with tempfile.TemporaryDirectory() as name:
            runner = FakeRunner()
            root = Path(name) / "GARMIN"
            sync.download_mtp_entries(
                entries, root, runner, Path(name) / "stderr.log", max_items=1)
            self.assertEqual((root / "Activity/run.fit").read_bytes(), b"xxx")
            self.assertEqual(
                (root / "Debug/MetricsBackup/metric.fit").read_bytes(), b"xxxx")
            self.assertEqual(len(runner.calls), 2)
            self.assertTrue(all(call[0] == sync.MTP_CONNECT for call in runner.calls))

    def test_mtp_download_batching_collapses_normal_fenix_inventory(self):
        entries = [
            {"id": 16777216 + index, "path": f"Activity/{index:04d}.fit", "size": 100}
            for index in range(824)
        ]
        batches = sync._mtp_download_batches(
            entries, Path("/tmp/garmin-sync/objects"), max_items=1024,
            arg_budget=96 * 1024)
        self.assertEqual(len(batches), 1)
        self.assertEqual(len(batches[0]), 824)

        constrained = sync._mtp_download_batches(
            entries[:10], Path("/tmp/garmin-sync/objects"), max_items=3,
            arg_budget=96 * 1024)
        self.assertEqual([len(batch) for batch in constrained], [3, 3, 3, 1])

    def test_macos_ioreg_detects_fenix_usb_device(self):
        import plistlib
        fixture = [{
            "IORegistryEntryChildren": [
                {"idVendor": 0x1234, "idProduct": 0x5678},
                {"idVendor": 0x091E, "idProduct": 0x51B5,
                 "USB Product Name": "fenix 8 - 51mm"},
            ]
        }]
        self.assertTrue(sync.fenix_present_in_ioreg(plistlib.dumps(fixture)))
        fixture[0]["IORegistryEntryChildren"][1]["idProduct"] = 0x9999
        fixture[0]["IORegistryEntryChildren"][1]["USB Product Name"] = "fenix 8 - 51mm"
        self.assertTrue(sync.fenix_present_in_ioreg(plistlib.dumps(fixture)))
        fixture[0]["IORegistryEntryChildren"][1]["USB Product Name"] = "Other Garmin"
        self.assertFalse(sync.fenix_present_in_ioreg(plistlib.dumps(fixture)))

    def test_macos_watch_syncs_once_per_connection_edge(self):
        sequence = iter([False, True, True, False, True, False])
        class Stop:
            def __init__(self): self.count = 0
            def is_set(self): return self.count >= 6
            def wait(self, _seconds): self.count += 1
            def set(self): self.count = 6
        class Lock:
            def __enter__(self): return self
            def __exit__(self, *_): return False
        state = sync.State(Path(self.temp.name) / "watch-state.json") if hasattr(self, "temp") else None
        if state is None:
            temp = tempfile.TemporaryDirectory()
            self.addCleanup(temp.cleanup)
            state = sync.State(Path(temp.name) / "watch-state.json")
        with mock.patch.object(sync, "IS_DARWIN", True), \
             mock.patch.object(sync, "RunLocks", return_value=Lock()), \
             mock.patch.object(sync, "once", return_value=0) as once:
            rc = sync.macos_watch(
                state, poll_seconds=0, presence=lambda: next(sequence), stop_event=Stop())
        self.assertEqual(rc, 0)
        self.assertEqual(once.call_count, 2)
        self.assertTrue(all(call.args[1] == "copy" for call in once.call_args_list))

    def test_macos_force_and_dry_run_do_not_signal_systemd(self):
        class Lock:
            def __enter__(self): return self
            def __exit__(self, *_): return False
        with mock.patch.object(sync, "IS_DARWIN", True), \
             mock.patch.object(sync, "RunLocks", return_value=Lock()), \
             mock.patch.object(sync, "once", return_value=0) as once, \
             mock.patch.object(subprocess, "call",
                               side_effect=AssertionError("systemd must not be called")):
            self.assertEqual(sync.main(["dry-run"]), 0)
            self.assertEqual(once.call_args.args[1], "dry-run")
            once.reset_mock()
            self.assertEqual(sync.main(["force"]), 0)
            self.assertEqual(once.call_args.args[1], "copy")

    def test_single_instance_and_legacy_lock(self):
        with tempfile.TemporaryDirectory() as name:
            runtime, legacy = Path(name) / "runtime", Path(name) / "legacy"
            with sync.RunLocks(runtime, legacy):
                with self.assertRaises(sync.Busy):
                    with sync.RunLocks(runtime, legacy): pass
            with open(legacy, "r+") as handle:
                sync.fcntl.flock(handle, sync.fcntl.LOCK_EX | sync.fcntl.LOCK_NB)
                with self.assertRaises(sync.Busy):
                    with sync.RunLocks(runtime, legacy): pass
            with sync.RunLocks(runtime, legacy): pass
    def test_model_case_spacing_hash_and_wrong_model(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            xml = root / "GarminDevice.xml"
            xml.write_text(XML)
            self.assertNotEqual(sync.model_at(root)["identity"], "fixture")
            xml.write_text(XML.replace("FENIX  8", "fenix 80"))
            self.assertIsNone(sync.model_at(root))
            xml.write_text(XML.replace("FENIX  8", "fēnix8"))
            self.assertIsNotNone(sync.model_at(root))
            xml.write_text("<bad")
            self.assertIsNone(sync.model_at(root))
            xml.write_text(XML.replace("<Id>fixture</Id>", ""))
            self.assertIsNone(sync.model_at(root))
    def test_uri_only_mtp_removal_and_absent_once(self):
        mount = mock.Mock()
        mount.get_root.return_value.get_path.return_value = None
        mount.get_root.return_value.get_uri.return_value = "mtp:" + "//fixture/"
        self.assertTrue(sync.mount_matches_source(
            mount, "/run/user/1000/gvfs/mtp:host=fixture/Internal Storage/GARMIN"))
        self.assertFalse(sync.mount_matches_source(
            mount, "/run/user/1000/gvfs/mtp:host=phone/Internal Storage/GARMIN"))
        self.assertFalse(sync.mount_matches_source(mount, "/media/user/watch/GARMIN"))
        mount.get_root.return_value.get_uri.return_value = "gphoto2:" + "//fixture/"
        self.assertTrue(sync.mount_matches_source(
            mount, "/run/user/1000/gvfs/gphoto2:host=fixture/GARMIN"))
        self.assertFalse(sync.mount_matches_source(
            mount, "/run/user/1000/gvfs/mtp:host=fixture/Internal Storage/GARMIN"))
        with mock.patch.object(sync, "enrolled_device", return_value={"identity": "a" * 64}), \
             mock.patch.object(sync, "discover", return_value=None):
            self.assertEqual(sync.once(None, "dry-run"), 2)

    def test_discovery_requires_unique_or_enrolled_identity(self):
        with tempfile.TemporaryDirectory() as name:
            first, second = Path(name) / "one", Path(name) / "two"
            for base, identity in ((first, "fixture"), (second, "another")):
                root = base / "GARMIN"
                root.mkdir(parents=True)
                root.joinpath("GarminDevice.xml").write_text(XML.replace("fixture", identity))
            first_device = sync.model_at(first / "GARMIN")
            with self.assertRaises(sync.DiscoveryError):
                sync.discover([str(first), str(second)], uid=9999, username="fixture")
            with self.assertRaises(sync.DiscoveryError):
                sync.discover([str(first), str(second)], uid=9999, username="fixture",
                              expected_identity=first_device["identity"])
            found = sync.discover([str(first)], uid=9999, username="fixture",
                                  expected_identity=first_device["identity"])
            self.assertEqual(found["root"], str(first / "GARMIN"))
            with self.assertRaisesRegex(sync.DiscoveryError, "not the enrolled"):
                sync.discover([str(second)], uid=9999, username="fixture",
                              expected_identity=first_device["identity"])

    def test_enrollment_is_private_and_once_refuses_unenrolled(self):
        with tempfile.TemporaryDirectory() as name:
            pin = Path(name) / "config" / "device.json"
            saved = sync.save_enrollment(
                {"identity": "a" * 64, "model": "fenix 8", "root": "/fixture"}, pin)
            self.assertEqual(saved["identity"], "a" * 64)
            self.assertEqual(sync.enrolled_device(pin)["identity"], "a" * 64)
            self.assertEqual(pin.stat().st_mode & 0o777, 0o600)
        with mock.patch.object(sync, "enrolled_device", return_value=None):
            self.assertEqual(sync.once(None, "verify"), 3)

    def test_verify_obeys_process_lock(self):
        class AlwaysBusy:
            def __enter__(self): raise sync.Busy("fixture")
            def __exit__(self, *_): return False
        with mock.patch.object(sync, "RunLocks", return_value=AlwaysBusy()):
            self.assertEqual(sync.main(["verify"]), 75)
    def test_state_atomic_preserves_last_success(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.json"
            state = sync.State(path)
            state.update(last_success="previous", considered=3)
            state.update(status="failed")
            self.assertEqual(sync.State(path).snapshot()["last_success"], "previous")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
    def test_conflicting_autostarts_detects_renamed_legacy_launcher(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            root.joinpath("unrelated.desktop").write_text(
                "[Desktop Entry]\nExec=/usr/bin/true\n")
            root.joinpath("renamed.desktop").write_text(
                "[Desktop Entry]\nExec=/home/user/.local/bin/garmin_autodev_sync.sh\n")
            conflicts = sync.conflicting_autostarts(root)
            self.assertEqual(conflicts, [str(root / "renamed.desktop")])

    def test_readiness_rejects_stale_pid_and_wrong_build(self):
        state = {"daemon_pid": 123, "status": "waiting", "build": {"commit": "abc"}}
        service = {"ActiveState": "active", "MainPID": "123", "SubState": "running"}
        with mock.patch.object(sync, "service_snapshot", return_value=service), \
             mock.patch.object(sync, "State") as state_type, \
             mock.patch.object(sync, "build_info", return_value={"commit": "abc"}), \
             mock.patch.object(sync, "enrolled_device", return_value=None):
            state_type.return_value.snapshot.return_value = state
            ready, _ = sync.readiness("abc")
            self.assertTrue(ready)
            state_type.return_value.snapshot.return_value = {**state, "daemon_pid": 122}
            ready, _ = sync.readiness("abc")
            self.assertFalse(ready)
            state_type.return_value.snapshot.return_value = state
            with mock.patch.object(sync, "build_info", return_value={"commit": "older"}):
                ready, _ = sync.readiness("abc")
                self.assertFalse(ready)

    def test_status_is_unhealthy_until_device_is_enrolled(self):
        payload = {
            "service": {"ActiveState": "active", "MainPID": "123"},
            "state": {"status": "waiting"},
            "build": {"commit": "abc"},
            "enrollment": None,
        }
        with mock.patch.object(sync, "readiness", return_value=(True, payload)):
            self.assertEqual(sync.main(["status"]), 1)

    def test_runner_cancellation_kills_stubborn_child_group(self):
        with tempfile.TemporaryDirectory() as name:
            runner, results = sync.Runner(), []
            def work():
                try:
                    runner.run(["/usr/bin/python3", "-c",
                                "import signal,time; signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(60)"],
                               Path(name) / "stderr", timeout=10)
                except Exception as exc: results.append(exc)
            thread = threading.Thread(target=work)
            thread.start()
            deadline = time.monotonic() + 3
            while runner.process is None and time.monotonic() < deadline: time.sleep(0.01)
            self.assertIsNotNone(runner.process)
            time.sleep(0.1)
            start = time.monotonic()
            runner.cancel()
            thread.join(timeout=6)
            self.assertFalse(thread.is_alive())
            self.assertLess(time.monotonic() - start, 5)
            self.assertIsInstance(results[0], sync.Cancelled)

@unittest.skipUnless(Path(sync.RCLONE).exists(), "rclone required for local fixtures")
class LocalRcloneTests(unittest.TestCase):
    def setUp(self):
        quiet = mock.patch.object(sync, "log")
        quiet.start()
        self.addCleanup(quiet.stop)
        self.temp = tempfile.TemporaryDirectory(prefix="garmin-test-")
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.source = self.base / "device with spaces" / "GARMIN"
        self.source.mkdir(parents=True)
        self.destination, self.archive = self.base / "backup", self.base / "archives"
        self.state = sync.State(self.base / "state" / "state.json")
        self.write("GarminDevice.xml", XML.encode())
        for path in ("Activity/activity.fit", "Monitor/monitor.fit", "Debug/MetricsBackup/metrics.fit",
                     "SUMMARY/summary.fit", "Location/location.fit", "Sports/sport.fit",
                     "new unknown directory/state.bin", "# leading and trailing space .fit ",
                     "Activity/café ' $(literal).fit"):
            self.write(path, b"native fixture bytes")
        for path in ("MAP.IMG", "license.GmA", "firmware.gCd", "Maps/hidden.fit", "mUsIc/music.FIT",
                     "nested/Audio/sound.fit", "SID/large.sid", "RemoteSW/upgrade.fit", "Activity/video.MP4"):
            self.write(path, b"excluded fixture bytes")
    def write(self, name, content):
        path = self.source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path
    def run_sync(self, mode="copy", runner=None, inspect=False, source_snapshot=False):
        sdk_python = sync.SDK_PYTHON if sync.SDK_PYTHON.exists() else Path(sys.executable)
        inspector = Path(sync.__file__).resolve().parent / "garmin_fit_inspect.py"
        return sync.SyncRun(self.state, runtime=self.base / "runtime", destination=str(self.destination),
                            archive_base=str(self.archive), runner=runner, inspect=inspect,
                            inspector=inspector, sdk_python=sdk_python).execute(
                                str(self.source), mode, source_snapshot=source_snapshot)
    def test_selection_checksum_idempotence_archive_no_deletion(self):
        calls = []
        class RecordingRunner(sync.Runner):
            def run(self, args, *positional, **kwargs):
                calls.append(args)
                return super().run(args, *positional, **kwargs)
        result = self.run_sync(runner=RecordingRunner())
        copies = [args for args in calls if args[1] == "copy"]
        self.assertEqual(len(copies), 2)
        self.assertEqual(copies[0], [*copies[1], "--dry-run"])
        self.assertTrue((Path(self.state.snapshot()["log_path"]) / "preview.jsonl").exists())
        self.assertEqual((result["considered"], result["copied"], result["verified"]), (10, 10, 10))
        for path in ("MAP.IMG", "Maps", "mUsIc", "nested/Audio", "SID", "RemoteSW"):
            self.assertFalse((self.destination / path).exists())
        self.assertTrue((self.destination / "Debug/MetricsBackup/metrics.fit").exists())
        self.assertTrue((self.destination / "# leading and trailing space .fit ").exists())
        self.assertEqual(self.run_sync()["copied"], 0)
        target = self.source / "Activity/activity.fit"
        old = target.stat()
        original = target.read_bytes()
        self.write("Activity/activity.fit", b"X" * len(original))
        os.utime(target, ns=(old.st_atime_ns, old.st_mtime_ns))
        changed = self.run_sync()
        self.assertEqual(changed["copied"], 1)
        self.assertEqual((Path(changed["archive"]) / "Activity/activity.fit").read_bytes(), original)
        self.assertNotEqual(changed["archive"], result["archive"])
        target.unlink()
        (self.destination / "destination-only.fit").write_bytes(b"retain")
        self.run_sync()
        self.assertTrue((self.destination / "Activity/activity.fit").exists())
        self.assertTrue((self.destination / "destination-only.fit").exists())
    def test_verified_snapshot_fast_path_skips_preview_and_traverses_destination(self):
        calls = []
        class RecordingRunner(sync.Runner):
            def run(self, args, *positional, **kwargs):
                calls.append(args)
                return super().run(args, *positional, **kwargs)

        result = self.run_sync(runner=RecordingRunner(), source_snapshot=True)
        copies = [args for args in calls if len(args) > 1 and args[1] == "copy"]
        checks = [args for args in calls if len(args) > 1 and args[1] == "check"]

        self.assertEqual(len(copies), 1)
        self.assertNotIn("--dry-run", copies[0])
        self.assertNotIn("--no-traverse", copies[0])
        self.assertIn("--fast-list", copies[0])
        self.assertEqual(len(checks), 1)
        self.assertIn("--fast-list", checks[0])
        self.assertFalse((Path(self.state.snapshot()["log_path"]) / "preview.jsonl").exists())
        self.assertEqual((result["considered"], result["copied"], result["verified"]), (10, 10, 10))
        self.assertTrue((self.destination / "Debug/MetricsBackup/metrics.fit").exists())

    def test_verified_snapshot_dry_run_is_single_read_only_pass(self):
        calls = []
        class RecordingRunner(sync.Runner):
            def run(self, args, *positional, **kwargs):
                calls.append(args)
                return super().run(args, *positional, **kwargs)

        result = self.run_sync("dry-run", runner=RecordingRunner(), source_snapshot=True)
        copies = [args for args in calls if len(args) > 1 and args[1] == "copy"]
        self.assertEqual(len(copies), 1)
        self.assertIn("--dry-run", copies[0])
        self.assertNotIn("--no-traverse", copies[0])
        self.assertIn("--fast-list", copies[0])
        self.assertEqual(result["verified"], 0)
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.archive.exists())

    def test_dry_run_read_only(self):
        self.run_sync()
        before = {p.relative_to(self.destination): p.read_bytes() for p in self.destination.rglob("*") if p.is_file()}
        self.write("Activity/activity.fit", b"changed")
        result = self.run_sync("dry-run")
        after = {p.relative_to(self.destination): p.read_bytes() for p in self.destination.rglob("*") if p.is_file()}
        self.assertEqual(before, after)
        self.assertEqual(result["verified"], 0)
        self.assertFalse(self.archive.exists())
    def test_failed_preview_prevents_actual_copy(self):
        calls = []
        class FailingPreviewRunner(sync.Runner):
            def run(self, args, *positional, **kwargs):
                calls.append(args)
                if args[1] == "copy" and "--dry-run" in args:
                    raise sync.CommandFailed("fixture preview failure")
                return super().run(args, *positional, **kwargs)
        with self.assertRaisesRegex(sync.CommandFailed, "preview failure"):
            self.run_sync(runner=FailingPreviewRunner())
        copy_calls = [args for args in calls if args[1] == "copy"]
        self.assertEqual(len(copy_calls), 1)
        self.assertIn("--dry-run", copy_calls[0])
        self.assertFalse(self.destination.exists())
        self.assertFalse(self.archive.exists())
        self.assertIsNone(self.state.snapshot()["last_success"])
    def test_newline_cr_filter_fallback_and_verify(self):
        self.write("Activity/line\nbreak.fit", b"newline preserved")
        self.write("Monitor/carriage\rreturn.fit", b"CR preserved")
        result = self.run_sync()
        self.assertEqual(result["considered"], 12)
        self.assertEqual(self.state.snapshot()["selection"], "filters")
        self.assertEqual((self.destination / "Activity/line\nbreak.fit").read_bytes(), b"newline preserved")
        self.assertEqual((self.destination / "Monitor/carriage\rreturn.fit").read_bytes(), b"CR preserved")
        self.assertEqual(self.run_sync("verify")["verified"], 12)
    def test_check_detects_same_size_corruption_and_retains_diagnostics(self):
        self.run_sync()
        path = self.destination / "Activity/activity.fit"
        path.write_bytes(b"Z" * path.stat().st_size)
        with self.assertRaises(sync.CommandFailed): self.run_sync("verify")
        logs = Path(self.state.snapshot()["log_path"])
        self.assertTrue((logs / "rclone.jsonl").exists())
        self.assertTrue((logs / "stderr.log").exists())
    def test_changed_inventory_prevents_success(self):
        outer = self
        class ChangingRunner(sync.Runner):
            def run(self, args, *positional, **kwargs):
                output = super().run(args, *positional, **kwargs)
                if args[1] == "copy" and "--dry-run" not in args:
                    outer.write("Activity/new.fit", b"new during copy")
                return output
        with self.assertRaisesRegex(sync.CommandFailed, "inventory changed"):
            self.run_sync(runner=ChangingRunner())
        self.assertIsNone(self.state.snapshot()["last_success"])
    def test_same_root_device_replacement_prevents_success(self):
        outer = self
        class ReplacingRunner(sync.Runner):
            def run(self, args, *positional, **kwargs):
                output = super().run(args, *positional, **kwargs)
                if args[1] == "check": outer.write("GarminDevice.xml", XML.replace("fixture", "another").encode())
                return output
        with self.assertRaises((sync.CommandFailed, sync.Cancelled)):
            self.run_sync(runner=ReplacingRunner())
        self.assertIsNone(self.state.snapshot()["last_success"])
    def test_bad_fit_warning_preserves_raw_bytes(self):
        result = self.run_sync(inspect=True)
        self.assertTrue(result["fit_warnings"])
        warning = next(item for item in result["fit_warnings"] if isinstance(item, dict))
        self.assertFalse(warning["decode_ok"])
        self.assertFalse(warning["integrity_ok"])
        self.assertEqual((self.destination / "Activity/activity.fit").read_bytes(), b"native fixture bytes")
        self.assertEqual(result["verified"], 10)


class FitInspectorTests(unittest.TestCase):
    @staticmethod
    def crc(data):
        crc = 0
        for byte in data:
            crc ^= byte
            for _ in range(8):
                crc = (crc >> 1) ^ (0xA001 if crc & 1 else 0)
        return crc & 0xFFFF

    def minimal_fit(self):
        # One file_id definition and one file_id data message (type=activity).
        definition = bytes([0x40, 0x00, 0x00, 0x00, 0x00, 0x01,
                            0x00, 0x01, 0x00])
        data = bytes([0x00, 0x04])
        body = definition + data
        header12 = struct.pack("<BBHI4s", 14, 0x20, 100, len(body), b".FIT")
        header = header12 + struct.pack("<H", self.crc(header12))
        complete = header + body
        return complete + struct.pack("<H", self.crc(complete))

    def test_checkout_inspector_decodes_valid_and_rejects_corrupt_fit(self):
        inspector = Path(sync.__file__).resolve().parent / "garmin_fit_inspect.py"
        sdk_python = sync.SDK_PYTHON if sync.SDK_PYTHON.exists() else Path(sys.executable)
        with tempfile.TemporaryDirectory() as name:
            valid = Path(name) / "valid.fit"
            bad = Path(name) / "bad.fit"
            valid.write_bytes(self.minimal_fit())
            bad.write_bytes(b"not a fit file")
            good = subprocess.run([str(sdk_python), str(inspector), str(valid)],
                                  capture_output=True, text=True)
            self.assertEqual(good.returncode, 0, good.stderr + good.stdout)
            decoded = json.loads(good.stdout)[0]
            self.assertTrue(decoded["decode_ok"])
            self.assertTrue(decoded["integrity_ok"])
            broken = subprocess.run([str(sdk_python), str(inspector), str(bad)],
                                    capture_output=True, text=True)
            self.assertEqual(broken.returncode, 1)
            rejected = json.loads(broken.stdout)[0]
            self.assertFalse(rejected["decode_ok"])


if __name__ == "__main__": unittest.main(verbosity=2)
