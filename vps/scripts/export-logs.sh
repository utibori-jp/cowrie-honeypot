#!/bin/bash
set -euo pipefail

BASE=/home/cowrie/honeypot
LOGDIR=$BASE/var/log/cowrie
TTYDIR=$BASE/var/lib/cowrie/tty
SENTDIR=$LOGDIR/sent
REMOTE=b2:cowrie-log/cowrie/honeypod1

mkdir -p "$SENTDIR"

find "$LOGDIR" -maxdepth 1 -name 'cowrie.json.20*' ! -name '*.gz' -exec gzip -9 {} \;

shopt -s nullglob
for f in "$LOGDIR"/cowrie.json.*.gz; do
  base=$(basename "$f")
  date_part=$(echo "$base" | grep -oE '[0-9]{4}-[0-9]{2}-[0-9]{2}') || true
  [ -n "${date_part:-}" ] || { echo "skip (no date): $base" >&2; continue; }
  y=${date_part:0:4}; m=${date_part:5:2}; d=${date_part:8:2}

  if rclone copy "$f" "$REMOTE/year=$y/month=$m/day=$d/" \
       --no-check-dest --s3-no-head --s3-no-check-bucket \
       --retries 3 --low-level-retries 10; then
    mv "$f" "$SENTDIR/"
    echo "sent: $base"
  else
    echo "FAILED: $base" >&2
  fi
done

# Log Rotation
find "$SENTDIR" -type f -mtime +14 -delete

find "$LOGDIR" -maxdepth 1 -name 'cowrie.log.20*' ! -name '*.gz' -exec gzip -9 {} \;
find "$LOGDIR" -maxdepth 1 -name 'cowrie.log.*.gz' -mtime +90 -delete

[ -d "$TTYDIR" ] && find "$TTYDIR" -type f -mtime +30 -delete

find "$BASE/var/lib/cowrie/downloads" -type f -mtime +30 -delete

echo "$(date -Is) ship-cowrie-logs OK"
