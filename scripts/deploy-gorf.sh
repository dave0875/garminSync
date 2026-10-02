#!/usr/bin/env bash
# Deploy a tested checkout to the local per-user Garmin service.
# This intentionally performs no rclone operation and never touches the watch.
set -euo pipefail

repo_root="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
target_root="$HOME/.local/bin/garmin-sync"
target_bin="$HOME/.local/bin"
target_unit="$HOME/.config/systemd/user/garmin-sync.service"
retired_root="$HOME/.local/bin/garmin-retired"

if [[ "$(id -un)" != "dbarker" || "$(hostname -s | tr '[:lower:]' '[:upper:]')" != "GORF" ]]; then
  echo "Refusing deployment: this script may only run as dbarker on GORF." >&2
  exit 64
fi
for required in "$repo_root/garmin_sync.py" "$repo_root/garmin_fit_inspect.py" \
  "$repo_root/bin/garmin-syncctl" "$repo_root/bin/garmin_autodev_sync.sh" \
  "$repo_root/systemd/garmin-sync.service" "$target_root/venv/bin/python3"; do
  [[ -f "$required" ]] || { echo "Missing required file: $required" >&2; exit 66; }
done

/usr/bin/python3 -m py_compile "$repo_root/garmin_sync.py" "$repo_root/garmin_fit_inspect.py"
PYTHONPATH="$repo_root" /usr/bin/python3 -m unittest discover -s "$repo_root/tests" -v
"$target_root/venv/bin/python3" -c 'from importlib.metadata import version; assert version("garmin-fit-sdk") == "21.214.0"'

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$retired_root/deploy-$stamp"
mkdir -p -m 700 "$backup"

install_atomic() {
  local source="$1" destination="$2" mode="$3" name
  name="$(basename "$destination")"
  if [[ -e "$destination" ]]; then
    cp -p -- "$destination" "$backup/$name"
  fi
  local temporary
  temporary="$(mktemp "${destination}.new.XXXXXX")"
  install -m "$mode" -- "$source" "$temporary"
  mv -f -- "$temporary" "$destination"
}

install_inspector() {
  local source="$1" destination="$2" name temporary
  name="$(basename "$destination")"
  if [[ -e "$destination" ]]; then
    cp -p -- "$destination" "$backup/$name"
  fi
  temporary="$(mktemp "${destination}.new.XXXXXX")"
  {
    printf '#!%s\n' "$target_root/venv/bin/python3"
    tail -n +2 "$source"
  } > "$temporary"
  chmod 0755 "$temporary"
  mv -f -- "$temporary" "$destination"
}

install_atomic "$repo_root/garmin_sync.py" "$target_root/garmin_sync.py" 0755
install_inspector "$repo_root/garmin_fit_inspect.py" "$target_bin/garmin_fit_inspect.py"
install_atomic "$repo_root/bin/garmin-syncctl" "$target_bin/garmin-syncctl" 0755
install_atomic "$repo_root/bin/garmin_autodev_sync.sh" "$target_bin/garmin_autodev_sync.sh" 0755
install_atomic "$repo_root/systemd/garmin-sync.service" "$target_unit" 0644

printf '%s\n' "source_commit=${GITHUB_SHA:-local}" "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" > "$backup/manifest.txt"
/usr/bin/systemctl --user daemon-reload
/usr/bin/systemctl --user restart garmin-sync.service
/usr/bin/systemctl --user is-active --quiet garmin-sync.service
/usr/bin/python3 "$target_root/garmin_sync.py" status
