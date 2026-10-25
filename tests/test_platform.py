"""Cross-OS path, USB volume and lock foundation without Garmin hardware."""
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

import garmin_platform as platform


class PlatformTests(unittest.TestCase):
    def test_host_families(self):
        self.assertEqual(platform.host_family("win32"), "windows")
        self.assertEqual(platform.host_family("darwin"), "macos")
        self.assertEqual(platform.host_family("linux"), "linux")
        with self.assertRaises(RuntimeError):
            platform.host_family("freebsd")

    def test_private_local_paths_on_all_three_systems(self):
        home = Path("C:/Users/example") if sys.platform == "win32" else Path("/home/example")
        win = platform.user_paths("win32", home, {"LOCALAPPDATA": "C:/Users/example/AppData/Local"})
        self.assertTrue(str(win["config"]).replace("\\", "/").endswith("AppData/Local/GarminSync"))
        self.assertEqual(win["device"].name, "device.json")
        self.assertEqual(win["state"].name, "state.json")
        self.assertNotIn("systemprofile", str(win["state"]))
        mac = platform.user_paths("darwin", Path("/Users/example"), {})
        self.assertEqual(mac["runtime"], Path("/Users/example/Library/Caches/garmin-sync"))
        self.assertEqual(mac["config"], Path("/Users/example/.config/garmin-sync"))
        linux = platform.user_paths("linux", Path("/home/example"), {})
        self.assertEqual(linux["config"], Path("/home/example/.config/garmin-sync"))
        self.assertEqual(linux["state"], Path("/home/example/.local/state/garmin-sync/state.json"))

    def test_windows_drive_letters_only_of_garmin_label(self):
        result = platform.windows_garmin_mounts([
            ("D:\\", "GARMIN", 2),
            ("E:\\", "fenix 8", 3),
            ("F:\\", "PHOTO", 2),
            ("G:\\", "GARMIN", 5),
            ("H:\\", "GARMIN", 1),
            ("/Users/Shared", "GARMIN", 2),
            ("D:\\", "GARMIN", 2)])
        self.assertEqual(result, ("D:\\", "E:\\"))

    def test_unrelated_devices_do_not_trigger_filesystem_scan(self):
        self.assertEqual(platform.windows_garmin_mounts([
            ("C:\\", "Windows", 3), ("D:\\", "USB MOVIES", 2)]), ())

    def test_native_interprocess_lock_is_exclusive_and_released(self):
        with tempfile.TemporaryDirectory() as name:
            lockfile = Path(name) / "controller.lock"
            with lockfile.open("w+b") as first, lockfile.open("r+b") as second:
                platform.try_lock(first)
                with self.assertRaises((BlockingIOError, OSError)):
                    platform.try_lock(second)
                platform.unlock(first)
                platform.try_lock(second)
                platform.unlock(second)


if __name__ == "__main__":
    unittest.main()
