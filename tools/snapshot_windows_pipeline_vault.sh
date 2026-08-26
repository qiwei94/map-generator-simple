#!/usr/bin/env bash
# Create an immutable, checksummed milestone snapshot on the Windows F drive.
# Run inside Ubuntu-24.04 WSL2 as mapworker. Existing snapshots are never
# overwritten or deleted by this script.

set -euo pipefail

usage() {
  cat >&2 <<'EOF'
usage: snapshot_windows_pipeline_vault.sh \
  <snapshot-id> <controller-git-bundle> <controller-height-cache-archive> \
  [repo] [vault-root]

defaults:
  repo=/home/mapworker/map-generator-simple
  vault-root=/mnt/f/map-generator-vault
EOF
  exit 2
}

[[ $# -ge 3 && $# -le 5 ]] || usage

snapshot_id="$1"
controller_bundle="$2"
height_archive="$3"
repo="${4:-/home/mapworker/map-generator-simple}"
vault_root="${5:-/mnt/f/map-generator-vault}"

[[ "$snapshot_id" =~ ^[A-Za-z0-9._-]+$ ]] || {
  echo "invalid snapshot id: $snapshot_id" >&2
  exit 2
}

case "$vault_root" in
  /mnt/f/map-generator-vault|/mnt/f/map-generator-vault/*) ;;
  *)
    echo "refusing archive root outside /mnt/f/map-generator-vault" >&2
    exit 2
    ;;
esac

[[ -d "$repo/.git" ]] || {
  echo "not a Git worktree: $repo" >&2
  exit 3
}
[[ -f "$controller_bundle" ]] || {
  echo "controller bundle not found: $controller_bundle" >&2
  exit 3
}
[[ -f "$height_archive" ]] || {
  echo "height cache archive not found: $height_archive" >&2
  exit 3
}
command -v git >/dev/null
command -v rsync >/dev/null
command -v sha256sum >/dev/null

snapshot_root="$vault_root/snapshots/$snapshot_id"
staging_root="$vault_root/staging/$snapshot_id.partial"
[[ ! -e "$snapshot_root" ]] || {
  echo "snapshot already exists: $snapshot_root" >&2
  exit 4
}
[[ ! -e "$staging_root" || "${MAP_GENERATOR_VAULT_RESUME:-0}" == "1" ]] || {
  echo "partial snapshot already exists; inspect it manually: $staging_root" >&2
  echo "set MAP_GENERATOR_VAULT_RESUME=1 to resume it without deleting files" >&2
  exit 4
}

pipeline_cache="$repo/cache/pipeline"
pbf_cache="$repo/pbf_cache"
[[ -d "$pipeline_cache" ]] || {
  echo "pipeline cache not found: $pipeline_cache" >&2
  exit 3
}
[[ -d "$pbf_cache" ]] || {
  echo "PBF cache not found: $pbf_cache" >&2
  exit 3
}

mkdir -p "$vault_root"
required_bytes="$({
  du -sb "$pipeline_cache" "$pbf_cache" "$controller_bundle" \
    "$height_archive"
} | awk '{total += $1} END {print total + 1073741824}')"
available_bytes="$(df -B1 --output=avail "$vault_root" 2>/dev/null | tail -1 | tr -d ' ')"
if [[ "$required_bytes" -gt "$available_bytes" ]]; then
  echo "insufficient archive space: need=$required_bytes available=$available_bytes" >&2
  exit 5
fi

mkdir -p "$staging_root/code" "$staging_root/runtime" \
  "$staging_root/evidence" "$staging_root/manifests"

copy_immutable_input() {
  local source="$1"
  local destination="$2"
  if [[ -e "$destination" ]]; then
    cmp -s "$source" "$destination" || {
      echo "partial snapshot input differs: $destination" >&2
      exit 6
    }
    return
  fi
  cp -p "$source" "$destination"
}

copy_immutable_input \
  "$controller_bundle" "$staging_root/code/$(basename "$controller_bundle")"
copy_immutable_input \
  "$height_archive" "$staging_root/evidence/$(basename "$height_archive")"

# Preserve the exact working data used by the current Windows renderer. The
# live copies stay on the WSL virtual SSD; this copy is disaster recovery.
rsync -a "$pipeline_cache/" "$staging_root/runtime/pipeline_cache/"
rsync -a "$pbf_cache/" "$staging_root/runtime/pbf_cache/"

windows_bundle="$staging_root/code/windows-worktree-all-refs.bundle"
if [[ ! -e "$windows_bundle" ]]; then
  git -C "$repo" bundle create "$windows_bundle" --all
fi
git -C "$repo" bundle verify \
  "$staging_root/code/$(basename "$controller_bundle")" \
  >"$staging_root/manifests/controller-bundle-verify.txt" 2>&1
git -C "$repo" bundle verify "$windows_bundle" \
  >"$staging_root/manifests/windows-bundle-verify.txt" 2>&1

{
  printf 'snapshot_id\t%s\n' "$snapshot_id"
  printf 'created_utc\t%s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf 'node\twindows-wsl2\n'
  printf 'repo\t%s\n' "$repo"
  printf 'windows_branch\t%s\n' "$(git -C "$repo" branch --show-current)"
  printf 'windows_commit\t%s\n' "$(git -C "$repo" rev-parse HEAD)"
  printf 'controller_bundle\t%s\n' "$(basename "$controller_bundle")"
  printf 'height_archive\t%s\n' "$(basename "$height_archive")"
  printf 'pipeline_cache_source\t%s\n' "$pipeline_cache"
  printf 'pbf_cache_source\t%s\n' "$pbf_cache"
} >"$staging_root/METADATA.tsv"

# The pre-existing 167 GB cache is catalogued in place instead of duplicated.
# Size and mtime are sufficient for discovery; immutable milestone files get
# cryptographic hashes below.
find /mnt/f/map_gen_cache -type f -printf '%s\t%TY-%Tm-%TdT%TH:%TM:%TS\t%p\n' \
  | LC_ALL=C sort >"$staging_root/manifests/existing-map-gen-cache.tsv"
du -B1 -d 2 /mnt/f/map_gen_cache \
  | LC_ALL=C sort -n >"$staging_root/manifests/existing-map-gen-cache-sizes.tsv"

(
  cd "$staging_root"
  find . -type f ! -path './manifests/SHA256SUMS' -print0 \
    | LC_ALL=C sort -z \
    | xargs -0 sha256sum >manifests/SHA256SUMS
  sha256sum -c manifests/SHA256SUMS \
    >manifests/SHA256SUMS.verify.txt
)

mkdir -p "$vault_root/snapshots"
mv "$staging_root" "$snapshot_root"
printf '%s\n' "$snapshot_id" >"$vault_root/LATEST"

echo "snapshot=$snapshot_root"
du -sh "$snapshot_root"
echo "checksum_files=$(wc -l < "$snapshot_root/manifests/SHA256SUMS")"
echo "verification=ok"
