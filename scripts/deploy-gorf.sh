#!/usr/bin/env bash
# Deploy a tested checkout to the local per-user Garmin service.
# This intentionally never invokes rclone against the watch.
set -euo pipefail

repo_root="$(
  unset CDPATH
  cd -- "$(dirname -- "$0")/.."
  pwd
)"
target_root="$HOME/.local/bin/garmin-sync"
target_bin="$HOME/.local/bin"
target_unit="$HOME/.config/systemd/user/garmin-sync.service"
retired_root="$HOME/.local/bin/garmin-retired"
autostart_dir="$HOME/.config/autostart"

if [[ "$(id -un)" != "dbarker" || "$(hostname -s | tr '[:lower:]' '[:upper:]')" != "GORF" ]]; then
  echo "Refusing deployment: this script may only run as dbarker on GORF." >&2
  exit 64
fi

if [[ -n "${GITHUB_ACTIONS:-}" ]]; then
  [[ "${GITHUB_REF:-}" == "refs/heads/main" ]] || {
    echo "Refusing deployment: GitHub Actions deployments must target main." >&2
    exit 64
  }
  [[ -n "${GITHUB_SHA:-}" && -n "${GITHUB_REPOSITORY:-}" && -n "${GITHUB_TOKEN:-}" ]] || {
    echo "Refusing deployment: required GitHub provenance is unavailable." >&2
    exit 64
  }
  checkout_sha="$(git -C "$repo_root" rev-parse HEAD)"
  [[ "$checkout_sha" == "$GITHUB_SHA" ]] || {
    echo "Refusing deployment: checkout SHA does not match GITHUB_SHA." >&2
    exit 65
  }
  remote_main="$(
    /usr/bin/python3 - <<'PY'
import json
import os
import urllib.request

repo = os.environ["GITHUB_REPOSITORY"]
token = os.environ["GITHUB_TOKEN"]
request = urllib.request.Request(
    f"https://api.github.com/repos/{repo}/git/ref/heads/main",
    headers={
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "garminSync-deployer",
    },
)
with urllib.request.urlopen(request, timeout=15) as response:
    print(json.load(response)["object"]["sha"])
PY
  )"
  [[ "$remote_main" == "$GITHUB_SHA" ]] || {
    echo "Refusing stale deployment: main has advanced beyond $GITHUB_SHA." >&2
    exit 65
  }
fi

for required in "$repo_root/garmin_sync.py" "$repo_root/garmin_backup_target.py" "$repo_root/garmin_fit_inspect.py" \
  "$repo_root/bin/garmin-syncctl" "$repo_root/bin/garmin_autodev_sync.sh" \
  "$repo_root/systemd/garmin-sync.service" "$repo_root/requirements.txt" \
  "$target_root/venv/bin/python3" "$target_root/venv/bin/pip"; do
  [[ -f "$required" ]] || { echo "Missing required file: $required" >&2; exit 66; }
done

/usr/bin/python3 -m py_compile "$repo_root/garmin_sync.py" "$repo_root/garmin_backup_target.py" "$repo_root/garmin_fit_inspect.py"
PYTHONPATH="$repo_root" "$target_root/venv/bin/python3" -m unittest discover -s "$repo_root/tests" -v

expected_sdk="$(sed -n 's/^garmin-fit-sdk==//p' "$repo_root/requirements.txt")"
[[ -n "$expected_sdk" ]] || { echo "Unable to determine Garmin FIT SDK requirement." >&2; exit 66; }
installed_sdk="$("$target_root/venv/bin/python3" -c 'from importlib.metadata import version; print(version("garmin-fit-sdk"))' 2>/dev/null || true)"

candidate_inspector="$(mktemp)"
cleanup() { rm -f -- "$candidate_inspector"; }
trap cleanup EXIT
{
  printf '#!%s\n' "$target_root/venv/bin/python3"
  tail -n +2 "$repo_root/garmin_fit_inspect.py"
} > "$candidate_inspector"
chmod 0755 "$candidate_inspector"

different() {
  [[ ! -e "$2" ]] || ! cmp -s -- "$1" "$2"
}

runtime_changed=0
for pair in \
  "$repo_root/garmin_sync.py|$target_root/garmin_sync.py" \
  "$repo_root/garmin_backup_target.py|$target_root/garmin_backup_target.py" \
  "$candidate_inspector|$target_bin/garmin_fit_inspect.py" \
  "$repo_root/bin/garmin-syncctl|$target_bin/garmin-syncctl" \
  "$repo_root/bin/garmin_autodev_sync.sh|$target_bin/garmin_autodev_sync.sh" \
  "$repo_root/systemd/garmin-sync.service|$target_unit"; do
  source="${pair%%|*}"
  destination="${pair#*|}"
  if different "$source" "$destination"; then
    runtime_changed=1
  fi
done

sdk_changed=0
[[ "$installed_sdk" == "$expected_sdk" ]] || sdk_changed=1
legacy_autostarts=()
if [[ -d "$autostart_dir" ]]; then
  for candidate in "$autostart_dir"/*.desktop; do
    [[ -e "$candidate" ]] || continue
    if grep -Eiq 'garmin_autodev_sync[.]sh|garmin-sync/garmin_sync[.]py[[:space:]]+daemon' "$candidate"; then
      legacy_autostarts+=("$candidate")
    fi
  done
fi
legacy_changed=0
(( ${#legacy_autostarts[@]} == 0 )) || legacy_changed=1

if (( runtime_changed == 0 && sdk_changed == 0 && legacy_changed == 0 )); then
  echo "No installed Garmin sync runtime change; leaving garmin-sync.service undisturbed."
  /usr/bin/systemctl --user is-active --quiet garmin-sync.service
  /usr/bin/python3 "$target_root/garmin_sync.py" readiness
  exit 0
fi

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup="$retired_root/deploy-$stamp"
mkdir -p -- "$backup"
chmod 0700 "$backup"

backup_if_present() {
  local source="$1" name
  name="$(basename "$source")"
  if [[ -e "$source" ]]; then
    cp -p -- "$source" "$backup/$name"
  fi
}

for installed in "$target_root/garmin_sync.py" "$target_root/garmin_backup_target.py" "$target_bin/garmin_fit_inspect.py" \
  "$target_bin/garmin-syncctl" "$target_bin/garmin_autodev_sync.sh" "$target_unit" \
  "$target_root/build.json"; do
  backup_if_present "$installed"
done
for legacy in "${legacy_autostarts[@]}"; do
  cp -p -- "$legacy" "$backup/legacy-$(basename "$legacy")"
done

printf '%s\n' \
  "source_commit=${GITHUB_SHA:-$(git -C "$repo_root" rev-parse HEAD)}" \
  "deployed_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)" \
  "previous_fit_sdk=${installed_sdk:-missing}" \
  "target_fit_sdk=$expected_sdk" > "$backup/manifest.txt"

# Never upgrade a working GORF service to an unbound destination.
# The operator must pre-enroll the *existing* Drive folder IDs beforehand.
if [[ ! -f "$HOME/.config/garmin-sync/installation.json" ]]; then
  echo "Refusing deployment: migrate and bind the existing GORF Google Drive folders first." >&2
  exit 77
fi
PYTHONPATH="$repo_root" /usr/bin/python3 - <<'PY'
import garmin_backup_target as target
with target.prepared_target("rclone", target.PROFILE_PATH.parent):
    pass
PY

rollback_armed=0
restore_one() {
  local backup_name="$1" destination="$2"
  if [[ -e "$backup/$backup_name" ]]; then
    cp -p -- "$backup/$backup_name" "$destination"
  else
    rm -f -- "$destination"
  fi
}

rollback() {
  local code="${1:-1}"
  if (( rollback_armed )); then
    rollback_armed=0
    trap - ERR INT TERM
    set +e
    echo "Deployment failed; restoring the previously installed Garmin runtime." >&2
    restore_one "garmin_sync.py" "$target_root/garmin_sync.py"
    restore_one "garmin_backup_target.py" "$target_root/garmin_backup_target.py"
    restore_one "garmin_fit_inspect.py" "$target_bin/garmin_fit_inspect.py"
    restore_one "garmin-syncctl" "$target_bin/garmin-syncctl"
    restore_one "garmin_autodev_sync.sh" "$target_bin/garmin_autodev_sync.sh"
    restore_one "garmin-sync.service" "$target_unit"
    restore_one "build.json" "$target_root/build.json"
    if [[ -n "${installed_sdk:-}" && "$installed_sdk" != "$expected_sdk" ]]; then
      "$target_root/venv/bin/pip" install --disable-pip-version-check \
        "garmin-fit-sdk==$installed_sdk" >/dev/null 2>&1
    fi
    for saved in "$backup"/legacy-*.desktop; do
      [[ -e "$saved" ]] || continue
      mkdir -p -- "$autostart_dir"
      chmod 0700 "$autostart_dir"
      cp -p -- "$saved" "$autostart_dir/${saved##*/legacy-}"
    done
    /usr/bin/systemctl --user daemon-reload
    /usr/bin/systemctl --user start garmin-sync.service
  fi
  return "$code"
}
trap 'code=$?; rollback "$code"; exit "$code"' ERR
trap 'rollback 130; exit 130' INT TERM
rollback_armed=1

# Stop only when a runtime/dependency migration is actually necessary. This
# prevents README/runner-only pushes from interrupting an active watch copy.
/usr/bin/systemctl --user stop garmin-sync.service || true

"$target_root/venv/bin/pip" install --disable-pip-version-check -r "$repo_root/requirements.txt"
"$target_root/venv/bin/python3" -c "from importlib.metadata import version; assert version('garmin-fit-sdk') == '$expected_sdk'"

install_atomic() {
  local source="$1" destination="$2" mode="$3" temporary
  mkdir -p -- "$(dirname "$destination")"
  temporary="$(mktemp "${destination}.new.XXXXXX")"
  install -m "$mode" -- "$source" "$temporary"
  mv -f -- "$temporary" "$destination"
}

install_atomic "$repo_root/garmin_sync.py" "$target_root/garmin_sync.py" 0755
install_atomic "$repo_root/garmin_backup_target.py" "$target_root/garmin_backup_target.py" 0644
install_atomic "$candidate_inspector" "$target_bin/garmin_fit_inspect.py" 0755
install_atomic "$repo_root/bin/garmin-syncctl" "$target_bin/garmin-syncctl" 0755
install_atomic "$repo_root/bin/garmin_autodev_sync.sh" "$target_bin/garmin_autodev_sync.sh" 0755
install_atomic "$repo_root/systemd/garmin-sync.service" "$target_unit" 0644

for legacy in "${legacy_autostarts[@]}"; do
  rm -f -- "$legacy"
done

commit="${GITHUB_SHA:-$(git -C "$repo_root" rev-parse HEAD)}"
deployed_at="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
build_tmp="$(mktemp "$target_root/build.json.new.XXXXXX")"
BUILD_COMMIT="$commit" BUILD_DEPLOYED_AT="$deployed_at" BUILD_SDK="$expected_sdk" \
  /usr/bin/python3 - "$build_tmp" <<'PY'
import json
import os
import sys

with open(sys.argv[1], "w") as handle:
    json.dump({
        "commit": os.environ["BUILD_COMMIT"],
        "deployed_at": os.environ["BUILD_DEPLOYED_AT"],
        "fit_sdk": os.environ["BUILD_SDK"],
    }, handle, indent=2)
    handle.write("\n")
PY
chmod 0600 "$build_tmp"
mv -f -- "$build_tmp" "$target_root/build.json"

/usr/bin/systemctl --user daemon-reload
/usr/bin/systemctl --user start garmin-sync.service

ready=0
for _ in {1..30}; do
  if GARMIN_SYNC_EXPECT_COMMIT="$commit" /usr/bin/python3 "$target_root/garmin_sync.py" readiness >/dev/null; then
    ready=1
    break
  fi
  sleep 0.5
done
if (( ready == 0 )); then
  echo "Deployment failed application-level readiness for $commit." >&2
  GARMIN_SYNC_EXPECT_COMMIT="$commit" /usr/bin/python3 "$target_root/garmin_sync.py" readiness || true
  rollback 1
  exit 1
fi

GARMIN_SYNC_EXPECT_COMMIT="$commit" /usr/bin/python3 "$target_root/garmin_sync.py" readiness
rollback_armed=0
trap - ERR INT TERM
