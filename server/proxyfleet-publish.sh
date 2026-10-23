#!/usr/bin/env bash
# Server-side publisher for proxyfleet. Run by a systemd timer as root.
#
# Picks up one complete batch from the SFTP inbox (its .ready marker is
# uploaded last), checks the SHA-256 manifest, the contract and each file's
# structure and node count, then switches the public release with one
# atomic symlink rename. Rejected batches never touch the live release.
# Messages never include file contents or the secret path.

set -euo pipefail

BASE_DIR=${PF_BASE_DIR:-/srv/proxyfleet}
UPLOAD_DIR=${PF_UPLOAD_DIR:-$BASE_DIR/sftp/proxyfleet/upload}
RELEASES_DIR=${PF_RELEASES_DIR:-$BASE_DIR/releases}
CURRENT_LINK=${PF_CURRENT_LINK:-$BASE_DIR/current}
TOKEN_FILE=${PF_TOKEN_FILE:-/etc/proxyfleet/path-token}
NEXT_FILE=${PF_NEXT_TOKEN_FILE:-$TOKEN_FILE.next}
LOCK_FILE=${PF_LOCK_FILE:-/run/lock/proxyfleet-publish.lock}
PUBLISH_GROUP=${PF_PUBLISH_GROUP:-caddy}
KEEP_RELEASES=${PF_KEEP_RELEASES:-5}
FAILED_RETENTION_DAYS=${PF_FAILED_RETENTION_DAYS:-7}
MAX_FILE_BYTES=1048576

[[ $KEEP_RELEASES =~ ^[1-9][0-9]*$ && $KEEP_RELEASES -le 100 ]] || { echo "invalid release retention" >&2; exit 1; }
[[ $FAILED_RETENTION_DAYS =~ ^[1-9][0-9]*$ && $FAILED_RETENTION_DAYS -le 365 ]] || { echo "invalid failed-batch retention" >&2; exit 1; }

exec 9>"$LOCK_FILE"
flock -n 9 || exit 0

[[ -d $UPLOAD_DIR && -d $RELEASES_DIR ]] || exit 0
[[ -f $TOKEN_FILE ]] || { echo "missing token file" >&2; exit 1; }
token=$(tr -d '\r\n' <"$TOKEN_FILE")
[[ $token =~ ^[0-9a-f]{64}$ ]] || { echo "invalid path token" >&2; exit 1; }

# Ownership can only be set by root; the smoke test runs unprivileged.
owner_args=()
if [[ $(id -u) -eq 0 ]]; then
  owner_args=(-o root -g "$PUBLISH_GROUP")
fi

ready=$(find "$UPLOAD_DIR" -maxdepth 1 -type f -name '*.ready' -printf '%f\n' | LC_ALL=C sort | head -n 1 || true)
[[ -n $ready ]] || exit 0

batch=${ready%.ready}
if [[ ! $batch =~ ^[0-9]{8}T[0-9]{6}Z-[0-9a-f]{12}$ ]]; then
  mv -f -- "$UPLOAD_DIR/$ready" "$UPLOAD_DIR/$ready.rejected"
  echo "rejected batch with an invalid name" >&2
  exit 1
fi

processing=$UPLOAD_DIR/$batch.processing
mv -f -- "$UPLOAD_DIR/$ready" "$processing"
reject() {
  trap - ERR
  echo "rejected batch $batch: $1" >&2
  [[ -f $processing ]] && mv -f -- "$processing" "$UPLOAD_DIR/$batch.failed"
  exit 1
}
trap 'reject "unexpected error"' ERR

file_for() {
  case $1 in
    surge) printf 'surge.txt\n' ;;
    loon) printf 'loon.txt\n' ;;
    mihomo) printf 'mihomo.yaml\n' ;;
    *) return 1 ;;
  esac
}

contract=$UPLOAD_DIR/$batch.contract.env
manifest=$UPLOAD_DIR/$batch.sha256
for file in "$contract" "$manifest"; do
  [[ -f $file && ! -L $file ]] || reject "missing or unsafe control file"
  size=$(stat -c %s "$file")
  (( size > 0 && size <= 65536 )) || reject "unsafe control file size"
done

# Contract: KEY=VALUE lines, each key exactly once, nothing else.
awk -F= '
  BEGIN { ok = 1 }
  NF != 2 || $1 !~ /^PF_[A-Z0-9_]+$/ || $2 !~ /^[A-Za-z0-9,]+$/ { ok = 0 }
  { seen[$1]++ }
  END {
    for (key in seen) if (seen[key] != 1) ok = 0
    if (seen["PF_CONTRACT_VERSION"] != 1 || seen["PF_CLIENTS"] != 1 || seen["PF_INVENTORY_SHA256"] != 1) ok = 0
    exit(ok ? 0 : 1)
  }
' "$contract" || reject "invalid contract"
contract_value() { awk -F= -v key="$1" '$1 == key { print $2 }' "$contract"; }

[[ $(contract_value PF_CONTRACT_VERSION) == 1 ]] || reject "unsupported contract version"
[[ $(contract_value PF_INVENTORY_SHA256) =~ ^[0-9a-f]{64}$ ]] || reject "invalid inventory hash"
IFS=, read -r -a clients <<<"$(contract_value PF_CLIENTS)"
(( ${#clients[@]} >= 1 && ${#clients[@]} <= 3 )) || reject "invalid client list"

declare -A expected_names=()
for client in "${clients[@]}"; do
  name=$(file_for "$client") || reject "unknown client"
  [[ -z ${expected_names[$batch.$name]:-} ]] || reject "duplicate client"
  expected_names[$batch.$name]=$client
done
expected_names[$batch.contract.env]=contract
[[ $(wc -l <"$contract") -eq $((3 + ${#clients[@]})) ]] || reject "contract has unexpected keys"

# Manifest: exactly one "<sha256>  <name>" line per expected file.
[[ $(wc -l <"$manifest") -eq ${#expected_names[@]} ]] || reject "invalid manifest line count"
while read -r digest name extra; do
  [[ -z ${extra:-} && $digest =~ ^[0-9a-f]{64}$ && -n ${expected_names[$name]:-} ]] || reject "invalid manifest entry"
done <"$manifest"
[[ $(awk '{ print $2 }' "$manifest" | LC_ALL=C sort -u | wc -l) -eq ${#expected_names[@]} ]] || reject "duplicate manifest entry"

for name in "${!expected_names[@]}"; do
  file=$UPLOAD_DIR/$name
  [[ -f $file && ! -L $file ]] || reject "missing or unsafe batch file"
  size=$(stat -c %s "$file")
  (( size > 0 && size <= MAX_FILE_BYTES )) || reject "unsafe batch file size"
done
(cd "$UPLOAD_DIR" && sha256sum --quiet -c "$batch.sha256" >/dev/null 2>&1) || reject "SHA-256 mismatch"

for client in "${clients[@]}"; do
  file=$UPLOAD_DIR/$batch.$(file_for "$client")
  expected=$(contract_value "PF_${client^^}_COUNT")
  [[ $expected =~ ^[1-9][0-9]*$ && $expected -le 10000 ]] || reject "invalid $client count in contract"
  if [[ $client == mihomo ]]; then
    [[ $(head -n 1 "$file") == proxies: && $(grep -c '^[^ #]' "$file") -eq 1 ]] || reject "invalid Mihomo structure"
    actual=$(grep -c '^  - name:' "$file" || true)
  else
    grep -q '^\[' "$file" && reject "$client list must not contain sections"
    actual=$(grep -cE '^[^#;[:space:]][^=]*=' "$file" || true)
  fi
  [[ $actual -eq $expected ]] || reject "$client node count $actual does not match contract $expected"
done

release=$RELEASES_DIR/$(date -u +%Y%m%dT%H%M%SZ)-$batch
install -d -m 0750 "${owner_args[@]}" "$release" "$release/$token"
for client in "${clients[@]}"; do
  name=$(file_for "$client")
  install -m 0640 "${owner_args[@]}" "$UPLOAD_DIR/$batch.$name" "$release/$token/$name"
done
install -m 0640 "${owner_args[@]}" "$contract" "$release/contract.env"
# During a path rotation (proxyfleet-rotate), serve the new path as well.
if [[ -f $NEXT_FILE ]]; then
  next=$(tr -d '\r\n' <"$NEXT_FILE")
  [[ $next =~ ^[0-9a-f]{64}$ && $next != "$token" ]] || reject "invalid pending path token"
  ln -s "$token" "$release/$next"
fi

new_link=$BASE_DIR/.current-$batch
ln -s "$release" "$new_link"
mv -Tf "$new_link" "$CURRENT_LINK"

for name in "${!expected_names[@]}"; do rm -f -- "$UPLOAD_DIR/$name"; done
rm -f -- "$manifest" "$processing"
trap - ERR

releases_root=$(readlink -f "$RELEASES_DIR")
current_target=$(readlink -f "$CURRENT_LINK")
mapfile -t release_names < <(find "$RELEASES_DIR" -mindepth 1 -maxdepth 1 -type d -printf '%f\n' | LC_ALL=C sort -r)
for ((index = KEEP_RELEASES; index < ${#release_names[@]}; index++)); do
  old_release=$(readlink -f "$RELEASES_DIR/${release_names[$index]}")
  [[ $old_release == "$releases_root/"* ]] || { echo "unsafe release cleanup target" >&2; exit 1; }
  [[ $old_release == "$current_target" ]] || rm -rf -- "$old_release"
done

find "$UPLOAD_DIR" -maxdepth 1 -type f -mtime "+$FAILED_RETENTION_DAYS" \( \
  -name '*.failed' -o -name '*.rejected' -o -name '*.sha256' -o -name '*.contract.env' -o \
  -name '*.surge.txt' -o -name '*.loon.txt' -o -name '*.mihomo.yaml' \
\) -delete

echo "published batch $batch"
