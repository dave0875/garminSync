"""Mock Windows volume discovery: never scan unrelated fixed drives or files."""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import garmin_platform
import garmin_sync

DEVICE_XML = '<Device><Model><Description>Forerunner 965</Description></Model><Id>unique-device</Id></Device>'


class WindowsDiscoveryTests(unittest.TestCase):
    def test_approved_volume_discovered_and_identity_bound(self):
        with tempfile.TemporaryDirectory() as home:
            mount = Path(home)
            folder = mount / "GARMIN"
            folder.mkdir()
            (folder / "GarminDevice.xml").write_text(DEVICE_XML)
            with mock.patch.object(garmin_sync, "IS_WINDOWS", True), \
                 mock.patch.object(garmin_platform, "windows_volume_infos",
                                   return_value=(("D:\\", "GARMIN", 2),)), \
                 mock.patch.object(garmin_platform, "windows_garmin_mounts",
                                   return_value=(str(mount),)):
                device = garmin_sync.discover()
                self.assertEqual(device["model"], "Forerunner 965")
                self.assertIsNotNone(garmin_sync.discover(
                    expected_identity=device["identity"]))
                with self.assertRaises(garmin_sync.DiscoveryError):
                    garmin_sync.discover(expected_identity="not-the-enrolled-watch")

    def test_no_approved_garmin_volume_means_no_watch(self):
        with mock.patch.object(garmin_sync, "IS_WINDOWS", True), \
             mock.patch.object(garmin_platform, "windows_volume_infos", return_value=()), \
             mock.patch.object(garmin_platform, "windows_garmin_mounts", return_value=()):
            self.assertIsNone(garmin_sync.discover())


if __name__ == "__main__":
    unittest.main()
