#!/usr/bin/env bash
set -euo pipefail

label="com.dave0875.garmin-sync"
domain="gui/$(id -u)"
plist="$HOME/Library/LaunchAgents/$label.plist"
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
python="$repo_root/.venv/bin/python"
log_dir="$HOME/Library/Logs/garmin-sync"

action="${1:-install}"

case "$action" in
  status)
    exec /bin/launchctl print "$domain/$label"
    ;;
  uninstall)
    /bin/launchctl bootout "$domain/$label" 2>/dev/null || true
    rm -f "$plist"
    echo "Removed $label"
    exit 0
    ;;
  install)
    ;;
  *)
    echo "Usage: $0 [install|status|uninstall]" >&2
    exit 64
    ;;
esac

if [[ "$(uname -s)" != "Darwin" ]]; then
  echo "This installer is only for macOS." >&2
  exit 64
fi
if [[ ! -x "$python" ]]; then
  echo "Missing project virtualenv: $python" >&2
  echo "Run: python3 -m venv .venv && .venv/bin/pip install -r requirements.txt" >&2
  exit 66
fi
if [[ ! -f "$repo_root/garmin_sync.py" ]]; then
  echo "Missing garmin_sync.py beneath $repo_root" >&2
  exit 66
fi

mkdir -p "$HOME/Library/LaunchAgents" "$log_dir"

"$python" - "$plist" "$repo_root" "$log_dir" <<'PY'
import plistlib
from pathlib import Path
import sys

plist_path = Path(sys.argv[1])
repo_root = Path(sys.argv[2])
log_dir = Path(sys.argv[3])
python = repo_root / ".venv/bin/python"
payload = {
    "Label": "com.dave0875.garmin-sync",
    "ProgramArguments": [str(python), str(repo_root / "garmin_sync.py"), "watch"],
    "WorkingDirectory": str(repo_root),
    "RunAtLoad": True,
    "KeepAlive": True,
    "ProcessType": "Background",
    "ThrottleInterval": 10,
    "EnvironmentVariables": {
        "PATH": "/opt/local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
    },
    "StandardOutPath": str(log_dir / "watch.log"),
    "StandardErrorPath": str(log_dir / "watch.err.log"),
}
with plist_path.open("wb") as handle:
    plistlib.dump(payload, handle, sort_keys=False)
PY

/bin/launchctl bootout "$domain/$label" 2>/dev/null || true
/bin/launchctl bootstrap "$domain" "$plist"
/bin/launchctl enable "$domain/$label"
/bin/launchctl kickstart -k "$domain/$label"

echo "Installed and started $label"
echo "Status: bash scripts/install-macos-launchagent.sh status"
echo "Logs:   $log_dir/watch.log"
