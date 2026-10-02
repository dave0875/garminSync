#!/usr/bin/python3
"""Controller regressions and rclone fixtures, using only temporary local backups."""
import os
from pathlib import Path
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
    def test_uri_only_mtp_removal_and_absent_once(self):
        mount = mock.Mock()
        mount.get_root.return_value.get_path.return_value = None
        mount.get_root.return_value.get_uri.return_value = "mtp:" + "//fixture/"
        self.assertTrue(sync.mount_matches_source(mount, "/run/user/1000/gvfs/mtp:host=fixture/Internal Storage/GARMIN"))
        self.assertFalse(sync.mount_matches_source(mount, "/media/user/watch/GARMIN"))
        mount.get_root.return_value.get_uri.return_value = "gphoto2:" + "//fixture/"
        self.assertTrue(sync.mount_matches_source(mount, "/run/user/1000/gvfs/gphoto2:host=fixture/GARMIN"))
        self.assertFalse(sync.mount_matches_source(mount, "/run/user/1000/gvfs/mtp:host=fixture/Internal Storage/GARMIN"))
        with mock.patch.object(sync, "discover", return_value=None):
            self.assertEqual(sync.once(None, "dry-run"), 2)
    def test_state_atomic_preserves_last_success(self):
        with tempfile.TemporaryDirectory() as name:
            path = Path(name) / "state.json"
            state = sync.State(path)
            state.update(last_success="previous", considered=3)
            state.update(status="failed")
            self.assertEqual(sync.State(path).snapshot()["last_success"], "previous")
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
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
    def run_sync(self, mode="copy", runner=None, inspect=False):
        return sync.SyncRun(self.state, runtime=self.base / "runtime", destination=str(self.destination),
                            archive_base=str(self.archive), runner=runner, inspect=inspect).execute(str(self.source), mode)
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
    @unittest.skipUnless(sync.INSPECTOR.exists() and sync.SDK_PYTHON.exists(), "optional SDK absent")
    def test_bad_fit_warning_preserves_raw_bytes(self):
        result = self.run_sync(inspect=True)
        self.assertTrue(result["fit_warnings"])
        warning = next(item for item in result["fit_warnings"] if isinstance(item, dict))
        self.assertFalse(warning["decode_ok"])
        self.assertFalse(warning["integrity_ok"])
        self.assertEqual((self.destination / "Activity/activity.fit").read_bytes(), b"native fixture bytes")
        self.assertEqual(result["verified"], 10)

if __name__ == "__main__": unittest.main(verbosity=2)
