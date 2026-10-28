#!/usr/bin/env bash
# Installs (or repairs) the DJ stream recorder in ~/rtmp-recorder and turns on automatic updates.
# Run on the server with:
#   bash <(curl -fsSL https://raw.githubusercontent.com/Zam6969/rtmpbot/main/install.sh)
set -euo pipefail
REPO="https://github.com/Zam6969/rtmpbot.git"
BRANCH="main"
DIR="$HOME/rtmp-recorder"

echo "==> Waiting for any running Ubuntu updates to finish"
while sudo fuser /var/lib/dpkg/lock-frontend /var/lib/dpkg/lock >/dev/null 2>&1; do
  echo "    still updating..."
  sleep 15
done

echo "==> Installing Docker and git (if needed)"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
fi
command -v git >/dev/null || sudo apt-get install -y git
sudo usermod -aG docker "$(id -un)" || true

echo "==> Getting the latest code from GitHub"
mkdir -p "$DIR"
cd "$DIR"
[ -d .git ] || git init -q
git remote get-url origin >/dev/null 2>&1 && git remote set-url origin "$REPO" || git remote add origin "$REPO"
git fetch -q origin "$BRANCH"
# Your settings (.env) and data (config/) are not part of the repository, so this leaves them alone.
git reset -q --hard "origin/$BRANCH"
git branch -q -M "$BRANCH" 2>/dev/null || true
git branch -q --set-upstream-to="origin/$BRANCH" 2>/dev/null || true
chmod +x update.sh
mkdir -p config
rm -f config/version 2>/dev/null || sudo rm -f config/version
git rev-parse --short HEAD > config/version

echo "==> Opening port 1935 (RTMP) in the firewall"
if command -v ufw >/dev/null && sudo ufw status | grep -q "Status: active"; then
  sudo ufw allow 1935/tcp
fi
if ! sudo iptables -C INPUT -p tcp --dport 1935 -j ACCEPT 2>/dev/null; then
  sudo iptables -I INPUT 1 -p tcp --dport 1935 -j ACCEPT
fi
if command -v netfilter-persistent >/dev/null; then sudo netfilter-persistent save || true; fi

if [ ! -f .env ]; then
  PUBLIC_IP=$(curl -fsS -4 https://api.ipify.org || hostname -I | awk '{print $1}')
  echo
  echo "==> Settings (you can change these later in $DIR/.env)"
  read -rp "Discord bot token: " DISCORD_TOKEN
  read -rp "Discord server ID: " GUILD_ID
  cat > .env <<ENV
DISCORD_TOKEN=$DISCORD_TOKEN
GUILD_ID=$GUILD_ID
RTMP_PUBLIC_URL=rtmp://$PUBLIC_IP:1935/live
MIN_RECORDING_SECONDS=120
TITLE_WAIT_MINUTES=30
ENV
  chmod 600 .env
fi

echo "==> Turning on automatic updates (checks GitHub every 15 minutes, or right away on /update)"
( crontab -l 2>/dev/null | grep -v 'rtmp-recorder/update.sh' || true
  echo "* * * * * $DIR/update.sh >/dev/null 2>&1" ) | crontab -

echo "==> Building and starting (first time takes a few minutes)"
sudo docker compose up -d --build
sleep 5
sudo docker compose ps

echo
echo "Done (version $(cat config/version))."
echo "Bot logs:  cd $DIR && sudo docker compose logs -f bot"
echo "Admins: /youtube connect to link YouTube, /update to update right away."
echo "DJs: /register to sign up."
