#!/usr/bin/env bash
# Applies settings that newer versions need, then builds and (re)starts the containers.
# Run by install.sh and update.sh; safe to run by hand: cd ~/rtmp-recorder && ./deploy.sh
set -uo pipefail
cd "$(dirname "$0")"

step() {  # step <name>: tell the bot's /update progress message which step we're on (update.sh sets PROGRESS_FILE)
  if [ -n "${PROGRESS_FILE:-}" ] && [ -f "$PROGRESS_FILE" ]; then sed -i "s/^stage=.*/stage=$1/" "$PROGRESS_FILE"; fi
}

# Public web address for the trim page. Use the server's own hostname if public DNS points it at this
# server; otherwise a free <ip>.sslip.io name, which always resolves to the IP inside it.
if [ -f .env ] && ! grep -q '^SITE_ADDRESS=' .env; then
  ip=$(curl -fsS -4 -m 10 https://api.ipify.org || true)
  site=""
  for host in "$(hostname -f 2>/dev/null)" "$(getent hosts "$ip" 2>/dev/null | awk '{print $2}')"; do
    case "$host" in *.*) ;; *) continue ;; esac
    if curl -fsS -m 10 "https://dns.google/resolve?name=$host&type=A" 2>/dev/null | grep -q "\"$ip\""; then
      site=$host
      break
    fi
  done
  if [ -z "$site" ] && [ -n "$ip" ]; then site="${ip//./-}.sslip.io"; fi
  echo "SITE_ADDRESS=$site" >> .env
  echo "Trim page address: https://$site"
fi

# Open the streaming and web ports (cloud Ubuntu images allow sudo without a password).
for port in 1935 80 443; do
  sudo -n iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null \
    || sudo -n iptables -I INPUT 1 -p tcp --dport "$port" -j ACCEPT 2>/dev/null || true
done
if command -v netfilter-persistent >/dev/null; then sudo -n netfilter-persistent save >/dev/null 2>&1 || true; fi
if command -v ufw >/dev/null && sudo -n ufw status 2>/dev/null | grep -q "Status: active"; then
  for port in 1935 80 443; do sudo -n ufw allow "$port/tcp" >/dev/null || true; done
fi

if docker info >/dev/null 2>&1; then docker=(docker); else docker=(sudo docker); fi
step installing
"${docker[@]}" compose build || exit 1
step restarting
"${docker[@]}" compose up -d --remove-orphans
