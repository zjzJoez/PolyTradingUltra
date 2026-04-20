#!/usr/bin/env bash
# Nightly SQLite backup.
#
# 1. Takes a consistent snapshot via `sqlite3 .backup` (safe vs live writers).
# 2. Gzips it and drops it into ~/backups/, keeping the most recent 14 files.
# 3. If ~/.aws/credentials exists AND $POLY_BACKUP_S3_BUCKET is set in the
#    environment (or /etc/default/polymarket-backup), also uploads to S3.
#
# To enable S3:
#   - create bucket in eu-west-1, e.g. polymarket-db-backups-<random>
#   - create IAM user with write-only access to that bucket
#   - save credentials in ~/.aws/credentials (user=ubuntu)
#   - echo 'POLY_BACKUP_S3_BUCKET=polymarket-db-backups-<random>' | sudo tee /etc/default/polymarket-backup

set -eu -o pipefail

DB_PATH="${POLY_BACKUP_DB_PATH:-/home/ubuntu/polymarket-mvp/var/polymarket_mvp.sqlite3}"
BACKUP_DIR="${POLY_BACKUP_DIR:-/home/ubuntu/backups}"
RETAIN_DAYS="${POLY_BACKUP_RETAIN:-14}"

# Load optional /etc/default overrides (for $POLY_BACKUP_S3_BUCKET).
if [ -f /etc/default/polymarket-backup ]; then
  # shellcheck disable=SC1091
  . /etc/default/polymarket-backup
fi

mkdir -p "$BACKUP_DIR"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$BACKUP_DIR/polymarket_mvp-$ts.sqlite3"

# Consistent online backup — safer than `cp` against a live DB.
sqlite3 "$DB_PATH" ".backup '$out'"
gzip -9 "$out"
out_gz="$out.gz"

echo "[backup] wrote $out_gz ($(du -h "$out_gz" | cut -f1))"

# Rotate: keep newest $RETAIN_DAYS files.
# shellcheck disable=SC2012
ls -1t "$BACKUP_DIR"/polymarket_mvp-*.sqlite3.gz 2>/dev/null | tail -n "+$((RETAIN_DAYS + 1))" | xargs -r rm -v

# Optional S3 mirror.
if [ -n "${POLY_BACKUP_S3_BUCKET:-}" ] && [ -f "$HOME/.aws/credentials" ]; then
  if command -v aws >/dev/null 2>&1; then
    aws s3 cp "$out_gz" "s3://$POLY_BACKUP_S3_BUCKET/$(basename "$out_gz")" --only-show-errors
    echo "[backup] uploaded to s3://$POLY_BACKUP_S3_BUCKET/"
  else
    echo "[backup] awscli not installed; skipping S3 upload" >&2
  fi
else
  echo "[backup] S3 not configured (POLY_BACKUP_S3_BUCKET unset or ~/.aws/credentials missing); local-only"
fi
