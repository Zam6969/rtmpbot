#!/usr/bin/env bash
# Keeps the recorder up to date with GitHub. Cron runs this every minute (install.sh sets that up).
# - Every 15 minutes it checks GitHub for new code.
# - When an admin uses /update in Discord, it checks right away.
# It never updates while a DJ is live or an upload is running; it just tries again later.
set -uo pipefail
cd "$(dirname "$0")"
exec 9>/tmp/dj-recorder-update.lock
flock -n 9 || exit 0

BRANCH=main
LOG=config/update.log
REQUEST=config/update-requested   # written by the bot; contains the Discord ID of the admin who asked
RESULT=config/update-result       # read by the bot, which DMs it to that admin

forced=0
[ -f "$REQUEST" ] && forced=1
if [ "$forced" = 0 ] && [ $(( 10#$(date +%M) % 15 )) -ne 0 ]; then
  exit 0
fi

finish() {  # finish <message>
  rm -f "$RESULT"
  { cat "$REQUEST" 2>/dev/null || echo; echo "$1"; } > "$RESULT.tmp" && mv "$RESULT.tmp" "$RESULT"
  rm -f "$REQUEST"
  echo "$(date -Is) $1" >> "$LOG"
}

if ! git fetch -q origin "$BRANCH" 2>>"$LOG"; then
  [ "$forced" = 1 ] && finish "Couldn't reach GitHub to check for updates."
  exit 0
fi

old=$(git rev-parse --short HEAD 2>/dev/null || echo none)
new=$(git rev-parse --short "origin/$BRANCH")
if [ "$old" = "$new" ]; then
  [ "$forced" = 1 ] && finish "Already up to date (version $new)."
  exit 0
fi

# The bot keeps config/busy fresh while someone is live or uploading. Ignore it if it's stale (bot crashed).
if [ -n "$(find config/busy -mmin -10 2>/dev/null)" ]; then
  exit 0
fi

changes=$(git log --format='• %s' "HEAD..origin/$BRANCH" 2>/dev/null | head -10)
git reset -q --hard "origin/$BRANCH"
chmod +x update.sh
rm -f config/version && echo "$new" > config/version

if docker compose up -d --build >>"$LOG" 2>&1; then
  finish "Updated $old → $new.
$changes"
else
  finish "Downloaded $new but it failed to start. Check ~/rtmp-recorder/config/update.log on the server."
fi
