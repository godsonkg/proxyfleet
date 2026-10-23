#!/usr/bin/env bash
# Rotate the secret subscription path. Run as root by `proxyfleet rotate-path`
# over SSH, in three phases:
#
#   prepare   serve the current release under both the old and the new path
#   finalize  serve it under the new path only, and make the new path permanent
#   rollback  undo an unfinished prepare
#
# The old and new tokens arrive on stdin (one per line) so they never show up
# in process listings, and they are never printed. rollback needs only the old
# token; the pending one is read from the server. Holds the publisher's lock,
# so a batch is never published halfway through a phase.

set -euo pipefail

BASE_DIR=${PF_BASE_DIR:-/srv/proxyfleet}
RELEASES_DIR=${PF_RELEASES_DIR:-$BASE_DIR/releases}
CURRENT_LINK=${PF_CURRENT_LINK:-$BASE_DIR/current}
TOKEN_FILE=${PF_TOKEN_FILE:-/etc/proxyfleet/path-token}
NEXT_FILE=${PF_NEXT_TOKEN_FILE:-$TOKEN_FILE.next}
LOCK_FILE=${PF_LOCK_FILE:-/run/lock/proxyfleet-publish.lock}
SITE_FILE=${PF_CADDY_SITE:-/etc/caddy/proxyfleet.caddy}
CADDYFILE=${PF_CADDYFILE:-/etc/caddy/Caddyfile}
BACKUP=$SITE_FILE.rotate-backup

fail() { echo "rotate: $*" >&2; exit 1; }

mode=${1:-}
[[ $mode == prepare || $mode == finalize || $mode == rollback ]] || fail "usage: proxyfleet-rotate prepare|finalize|rollback"
read_token() { [[ -f $1 ]] && tr -d '\r\n' <"$1"; }

old='' new=''
read -r old || true
read -r new || true
[[ $old =~ ^[0-9a-f]{64}$ ]] || fail "expected the current 64-hex token on stdin"

exec 9>"$LOCK_FILE"
flock -w 60 9 || fail "the publisher is busy; try again"

if [[ $mode == rollback ]]; then
  new=$(read_token "$NEXT_FILE" || true)
  [[ -z $new || $new =~ ^[0-9a-f]{64}$ ]] || fail "invalid pending token file"
else
  [[ $new =~ ^[0-9a-f]{64}$ ]] || fail "expected the new 64-hex token on stdin"
fi
[[ $old != "$new" ]] || fail "old and new tokens are identical"

# Rewrite only the @subscription matcher line of our own site file.
set_tokens() {
  local paths='' token
  for token in "$@"; do
    paths+=" /$token/surge.txt /$token/loon.txt /$token/mihomo.yaml"
  done
  [[ $(grep -cE '^[[:space:]]*@subscription path ' "$SITE_FILE") -eq 1 ]] || fail "unexpected site file layout"
  sed -i -E "s#^([[:space:]]*@subscription path).*#\\1$paths#" "$SITE_FILE"
}

apply_caddy() {
  caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 || return 1
  systemctl reload caddy
}

# Releases published during a rotation carry a link for the new path too.
remove_new_links() {
  find "$RELEASES_DIR" -mindepth 2 -maxdepth 2 -type l -name "$new" -delete
}

write_token() {
  (umask 077 && printf '%s\n' "$2" >"$1.tmp")
  mv -f "$1.tmp" "$1"
}

case $mode in
  prepare)
    [[ $(read_token "$TOKEN_FILE") == "$old" ]] || fail "the server's current path is not the one in your publish config"
    [[ ! -e $NEXT_FILE ]] || fail "a rotation is already in progress; run with --rollback first"
    current=$(readlink -f "$CURRENT_LINK" 2>/dev/null) || fail "nothing has been published yet"
    [[ -d $current/$old ]] || fail "nothing has been published yet"
    cp -a "$SITE_FILE" "$BACKUP"
    write_token "$NEXT_FILE" "$new"
    ln -sfn "$old" "$current/$new"
    set_tokens "$old" "$new"
    if ! apply_caddy; then
      cp -a "$BACKUP" "$SITE_FILE"
      remove_new_links
      rm -f "$NEXT_FILE" "$BACKUP"
      fail "Caddy rejected the configuration; nothing was changed"
    fi
    echo PF_ROTATE_PREPARED
    ;;
  finalize)
    [[ $(read_token "$TOKEN_FILE") == "$old" && $(read_token "$NEXT_FILE") == "$new" ]] ||
      fail "no matching rotation in progress"
    set_tokens "$new"
    if ! apply_caddy; then
      set_tokens "$old" "$new"
      apply_caddy || true
      fail "Caddy rejected the configuration; both paths still work"
    fi
    write_token "$TOKEN_FILE" "$new"
    rm -f "$NEXT_FILE" "$BACKUP"
    echo PF_ROTATE_FINALIZED
    ;;
  rollback)
    [[ $(read_token "$TOKEN_FILE") == "$old" ]] || fail "the server's current path is not the old one; nothing to roll back"
    if [[ -f $BACKUP ]]; then
      cp -a "$BACKUP" "$SITE_FILE"
    else
      set_tokens "$old"
    fi
    apply_caddy || fail "Caddy rejected the restored configuration; check $SITE_FILE"
    [[ -z $new ]] || remove_new_links
    rm -f "$NEXT_FILE" "$BACKUP"
    echo PF_ROTATE_ROLLED_BACK
    ;;
esac
