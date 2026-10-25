"""Host OS adapters. Windows is a qualifying target, not yet production certified.

All cloud defaults are platform-neutral; only per-user *local* runtime paths
and USB discovery vary. Never infer a Garmin from arbitrary user documents.
"""
import contextlib
import os
from pathlib import Path
import sys


def host_family(platform=None):
    platform = sys.platform if platform is None else platform
    if platform.startswith("win"):
        return "windows"
    if platform == "darwin":
        return "macos"
    if platform.startswith("linux"):
        return "linux"
    raise RuntimeError("Unsupported host operating system")


def user_paths(platform=None, home=None, environ=None):
    """Return private runtime/config/state paths without touching the filesystem.

    Preserve established Linux/macOS paths to protect existing deployments.
    On Windows, stay within the user's LocalAppData (never ProgramData/SYSTEM).
    """
    family = host_family(platform)
    home = Path.home() if home is None else Path(home)
    env = os.environ if environ is None else environ
    if family == "windows":
        base = Path(env.get("LOCALAPPDATA") or home / "AppData" / "Local") / "GarminSync"
        return {"config": base, "runtime": base / "runtime",
                "state": base / "state.json", "device": base / "device.json"}
    runtime = (home / "Library" / "Caches" / "garmin-sync"
               if family == "macos" else
               Path("/run/user") / str(os.getuid() if hasattr(os, "getuid") else 0) / "garmin-sync")
    config = home / ".config" / "garmin-sync"
    return {"config": config, "runtime": runtime,
            "state": home / ".local" / "state" / "garmin-sync" / "state.json",
            "device": config / "device.json"}


def windows_garmin_mounts(volume_infos):
    """Only probe native *mounted* volumes labeled Garmin, not unrelated drives.

    MTP-only watches do not receive a drive letter; WPD access is separately
    tracked under #45 and is REQUIRED before Windows can be release-qualified.
    volume_infos is an iterable of (root, label, drive_type) with drive_type
    values matching Windows GetDriveTypeW (2 removable / 3 fixed).
    """
    mounts = []
    for root, label, drive_type in volume_infos:
        if (drive_type in (2, 3) and isinstance(label, str)
                and ("garmin" in label.casefold() or "fenix" in label.casefold())
                and isinstance(root, str) and len(root) == 3
                and root[0].isalpha() and root[1:] == ":\\"):
            mounts.append(root)
    return tuple(sorted(set(mounts), key=str.casefold))


def windows_volume_infos():
    """Use native Win32 for volume labels. Does not access files or MTP storage."""
    if host_family() != "windows":
        return ()
    import ctypes
    kernel = ctypes.windll.kernel32
    roots = []
    bitset = kernel.GetLogicalDrives()
    if not bitset:
        raise OSError("Cannot inspect Windows volume map")
    for i in range(26):
        if not bitset & (1 << i):
            continue
        root = chr(65 + i) + ":\\"
        kind = kernel.GetDriveTypeW(ctypes.c_wchar_p(root))
        if kind not in (2, 3):
            continue
        name = ctypes.create_unicode_buffer(256)
        valid = kernel.GetVolumeInformationW(
            ctypes.c_wchar_p(root), name, len(name),
            None, None, None, None, 0)
        if valid:
            roots.append((root, name.value, kind))
    return tuple(roots)


def try_lock(handle):
    """Obtain exclusive nonblocking one-byte interprocess lock.

    Caller owns handle lifecycle. On Windows, msvcrt.locking operates from
    the current file offset, so lock byte zero consistently.
    """
    if host_family() == "windows":
        import msvcrt
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise BlockingIOError("Another Garmin Sync instance holds the lock") from exc
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)


def unlock(handle):
    if host_family() == "windows":
        import msvcrt
        handle.seek(0)
        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl
        fcntl.flock(handle, fcntl.LOCK_UN)
