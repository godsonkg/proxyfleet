#!/usr/bin/env bash
# Prepare a Debian/Ubuntu host as a proxyfleet subscription server.
#
#   sudo PF_DOMAIN=sub.example.com PF_UPLOAD_PUBKEY="ssh-ed25519 AAAA... you@pc" bash server/setup.sh
#
# Creates an SFTP-only, chrooted upload account, a systemd timer that runs
# proxyfleet-publish, and a Caddy site that serves only the three
# subscription files under a random 64-hex path. Safe to run again: the path
# token and existing releases are kept.

set -euo pipefail

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
UPLOAD_USER=proxyfleet
BASE=/srv/proxyfleet
CHROOT=$BASE/sftp/$UPLOAD_USER
CADDYFILE=/etc/caddy/Caddyfile
CADDY_SITE=/etc/caddy/proxyfleet.caddy
SSHD_DROPIN=/etc/ssh/sshd_config.d/80-proxyfleet.conf

die() { echo "error: $*" >&2; exit 1; }

[[ $(id -u) -eq 0 ]] || die "run as root"
: "${PF_DOMAIN:?set PF_DOMAIN to the host name clients will use}"
: "${PF_UPLOAD_PUBKEY:?set PF_UPLOAD_PUBKEY to the SSH public key of the computer that publishes}"
[[ $PF_DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] ||
  die "PF_DOMAIN must be a host name such as sub.example.com"
[[ $PF_UPLOAD_PUBKEY =~ ^(ssh-ed25519|ecdsa-sha2-nistp256|ssh-rsa)\ [A-Za-z0-9+/=]+(\ [^[:cntrl:]]*)?$ ]] ||
  die "PF_UPLOAD_PUBKEY must be a single OpenSSH public key line"
for helper in proxyfleet-publish.sh proxyfleet-rotate.sh; do
  [[ -f $SCRIPT_DIR/$helper ]] || die "$helper not found next to this script"
done
grep -qE '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf' /etc/ssh/sshd_config ||
  die "/etc/ssh/sshd_config does not include sshd_config.d/*.conf; add the SFTP settings by hand"

export DEBIAN_FRONTEND=noninteractive
if ! command -v caddy >/dev/null 2>&1; then
  apt-get update
  if [[ -z $(apt-cache policy caddy | awk '/Candidate:/ && $2 != "(none)" { print $2 }') ]]; then
    die "caddy is not in your apt sources; install it first: https://caddyserver.com/docs/install#debian-ubuntu-raspbian"
  fi
  apt-get install -y --no-install-recommends caddy
fi
getent group caddy >/dev/null || die "caddy group not found; is Caddy installed from a package?"

# --- SFTP-only upload account -------------------------------------------------
if ! id "$UPLOAD_USER" >/dev/null 2>&1; then
  useradd --home-dir "$CHROOT" --no-create-home --shell /usr/sbin/nologin --user-group "$UPLOAD_USER"
fi
# OpenSSH requires every component of the chroot to be root-owned and not writable by others.
install -d -m 0755 -o root -g root "$BASE" "$BASE/sftp" "$CHROOT" "$CHROOT/.ssh"
install -d -m 0700 -o "$UPLOAD_USER" -g "$UPLOAD_USER" "$CHROOT/upload"
printf '%s\n' "$PF_UPLOAD_PUBKEY" >"$CHROOT/.ssh/authorized_keys"
chown root:"$UPLOAD_USER" "$CHROOT/.ssh/authorized_keys"
chmod 0640 "$CHROOT/.ssh/authorized_keys"

cat >"$SSHD_DROPIN" <<EOF
# Managed by proxyfleet setup.sh
Match User $UPLOAD_USER
    ChrootDirectory %h
    ForceCommand internal-sftp -d /upload -u 0077
    PasswordAuthentication no
    PubkeyAuthentication yes
    PermitTTY no
    AllowTcpForwarding no
    AllowAgentForwarding no
    X11Forwarding no
    PermitTunnel no
Match all
EOF
if ! sshd -t; then
  rm -f "$SSHD_DROPIN"
  die "sshd rejected the new settings; nothing was changed"
fi
systemctl reload ssh 2>/dev/null || systemctl reload sshd

# --- Releases, secret path and publisher -------------------------------------
install -d -m 0750 -o root -g caddy "$BASE/releases"
install -d -m 0700 -o root -g root /etc/proxyfleet
if [[ ! -s /etc/proxyfleet/path-token ]]; then
  (umask 077 && openssl rand -hex 32 >/etc/proxyfleet/path-token)
fi
token=$(tr -d '\r\n' </etc/proxyfleet/path-token)
[[ $token =~ ^[0-9a-f]{64}$ ]] || die "/etc/proxyfleet/path-token is not 64 lowercase hex characters"

install -d -m 0755 -o root -g root /usr/local/libexec
install -m 0755 -o root -g root "$SCRIPT_DIR/proxyfleet-publish.sh" /usr/local/libexec/proxyfleet-publish
install -m 0755 -o root -g root "$SCRIPT_DIR/proxyfleet-rotate.sh" /usr/local/libexec/proxyfleet-rotate

cat >/etc/systemd/system/proxyfleet-publish.service <<EOF
[Unit]
Description=Publish a verified proxyfleet subscription batch
After=local-fs.target

[Service]
Type=oneshot
ExecStart=/usr/local/libexec/proxyfleet-publish
PrivateTmp=true
NoNewPrivileges=true
ProtectHome=true
ProtectSystem=strict
ReadWritePaths=$BASE /run/lock
EOF

cat >/etc/systemd/system/proxyfleet-publish.timer <<'EOF'
[Unit]
Description=Check for uploaded proxyfleet batches

[Timer]
OnBootSec=20s
OnUnitActiveSec=20s
AccuracySec=1s
Unit=proxyfleet-publish.service

[Install]
WantedBy=timers.target
EOF

# --- Caddy: our own site file, imported from the main Caddyfile ---------------
cat >"$CADDY_SITE" <<EOF
# Managed by proxyfleet setup.sh
$PF_DOMAIN {
    @subscription path /$token/surge.txt /$token/loon.txt /$token/mihomo.yaml
    handle @subscription {
        root * $BASE/current
        header {
            Cache-Control "private, no-store, max-age=0"
            X-Content-Type-Options "nosniff"
            Referrer-Policy "no-referrer"
        }
        file_server
    }
    handle {
        respond 404
    }
}
EOF
chown root:caddy "$CADDY_SITE"
chmod 0640 "$CADDY_SITE"

if ! grep -qxF "import $CADDY_SITE" "$CADDYFILE" 2>/dev/null; then
  backup=$CADDYFILE.proxyfleet-backup-$(date -u +%Y%m%dT%H%M%SZ)
  [[ -f $CADDYFILE ]] && cp -a "$CADDYFILE" "$backup"
  if [[ -f $CADDYFILE ]] && grep -q '/usr/share/caddy' "$CADDYFILE" && [[ $(grep -cE '^[^#[:space:]].*\{' "$CADDYFILE") -eq 1 ]]; then
    # The packaged welcome page only; replace it.
    printf 'import %s\n' "$CADDY_SITE" >"$CADDYFILE"
  else
    printf '\nimport %s\n' "$CADDY_SITE" >>"$CADDYFILE"
  fi
  if ! caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1; then
    if [[ -f $backup ]]; then cp -a "$backup" "$CADDYFILE"; else rm -f "$CADDYFILE"; fi
    die "Caddy rejected the configuration; your Caddyfile was restored (is $PF_DOMAIN already defined?)"
  fi
  [[ -f $backup ]] && echo "note: previous Caddyfile saved as $backup"
fi
caddy validate --config "$CADDYFILE" --adapter caddyfile >/dev/null 2>&1 || die "Caddy configuration is invalid"

systemctl daemon-reload
systemctl enable --now proxyfleet-publish.timer
systemctl enable caddy
systemctl reload-or-restart caddy

cat <<EOF

proxyfleet server is ready.
  1. Point DNS for $PF_DOMAIN at this host so Caddy can get a certificate.
  2. Read the secret path once and keep it out of chats and logs:
       sudo cat /etc/proxyfleet/path-token
  3. On your computer, set in publish.yaml:
       host: $PF_DOMAIN
       user: $UPLOAD_USER
       base_url: https://$PF_DOMAIN/<the 64 characters from step 2>
EOF
