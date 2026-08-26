#!/usr/bin/env bash
# Restore the verified standalone height database from a pipeline vault archive.
# Existing hot data is preserved before the atomic replacement.

set -euo pipefail

if [[ $# -lt 1 || $# -gt 2 ]]; then
  echo "usage: $0 SNAPSHOT_TAR_GZ [REPO_ROOT]" >&2
  exit 2
fi

archive="$1"
repo_arg="${2:-$(cd "$(dirname "$0")/.." && pwd -P)}"
member="data/height_cache/backups/building_heights_20260826_landmarks.sqlite3"

if [[ ! -f "$archive" ]]; then
  echo "snapshot archive not found: $archive" >&2
  exit 1
fi
if [[ ! -d "$repo_arg" || ! -f "$repo_arg/generate_city_legacy.py" ]]; then
  echo "not a map-generator-simple repository: $repo_arg" >&2
  exit 1
fi

repo_root="$(cd "$repo_arg" && pwd -P)"
target_dir="$repo_root/data/height_cache"
target="$target_dir/building_heights.sqlite3"
mkdir -p "$target_dir/backups"

case "$target" in
  "$repo_root"/data/height_cache/building_heights.sqlite3) ;;
  *) echo "refusing unexpected target: $target" >&2; exit 1 ;;
esac

if ! tar -tzf "$archive" "$member" >/dev/null 2>&1; then
  echo "verified height database is absent from snapshot: $member" >&2
  exit 1
fi

temporary="$(mktemp "$target_dir/.building_heights.restore.XXXXXX.sqlite3")"
cleanup() {
  if [[ -f "$temporary" ]]; then
    rm -f "$temporary"
  fi
}
trap cleanup EXIT
tar -xOzf "$archive" "$member" > "$temporary"

python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  python_bin="$(command -v python3)"
fi

"$python_bin" - "$temporary" <<'PY'
import sqlite3
import sys

path = sys.argv[1]
conn = sqlite3.connect(path)
try:
    integrity = conn.execute("PRAGMA integrity_check").fetchone()[0]
    observations = conn.execute(
        "SELECT COUNT(*) FROM height_observations").fetchone()[0]
    landmarks = conn.execute(
        "SELECT COUNT(*) FROM landmark_heights WHERE status='ok' "
        "AND height_m IS NOT NULL").fetchone()[0]
finally:
    conn.close()
if integrity != "ok":
    raise SystemExit(f"SQLite integrity check failed: {integrity}")
if landmarks <= 0:
    raise SystemExit("snapshot contains no positive landmark heights")
print(f"verified snapshot: integrity=ok observations={observations} "
      f"landmark_heights={landmarks}")
PY

if [[ -f "$target" ]]; then
  stamp="$(date -u +%Y%m%dT%H%M%SZ)"
  backup="$target_dir/backups/building_heights.preinstall-$stamp.sqlite3"
  cp -p "$target" "$backup"
  echo "preserved previous hot database: $backup"
fi

mv "$temporary" "$target"
trap - EXIT

PYTHONPATH="$repo_root" "$python_bin" - "$target" <<'PY'
import json
import sys
from _TEXTURE_STYLE_OF_DEEPSEEK.terrain3d.fetchers.building_height_store import (
    BuildingHeightStore,
    height_store_identity,
)

BuildingHeightStore(sys.argv[1])
print(json.dumps(height_store_identity(sys.argv[1]), ensure_ascii=False,
                 sort_keys=True))
PY
