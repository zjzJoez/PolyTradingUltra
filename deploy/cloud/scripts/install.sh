#!/usr/bin/env bash
# Install all Phase-D systemd units (copy + daemon-reload only).
# Timers are NOT enabled automatically — test each service first, then run
# `bash deploy/cloud/scripts/enable-timers.sh` to turn them on.
#
# Idempotent. Run as user `ubuntu`. Requires sudo for systemctl reloads.
#
# Usage: bash deploy/cloud/scripts/install.sh

set -eu -o pipefail

REPO_ROOT="$(cd "$(dirname "$0")/../../.." && pwd)"
UNIT_SRC="$REPO_ROOT/deploy/cloud/systemd"
UNIT_DST="/etc/systemd/system"

UNITS=(
  mvp-autopilot.service
  mvp-autopilot.timer
  alpha-lab-odds.service
  alpha-lab-odds.timer
  alpha-lab-clob.service
  alpha-lab-clob.timer
  alpha-lab-cycle.service
  alpha-lab-cycle.timer
  alpha-lab-import.service
  alpha-lab-import.timer
  alpha-lab-clv.service
  alpha-lab-clv.timer
  db-backup.service
  db-backup.timer
)

echo "[install] copying $(printf '%s ' "${UNITS[@]}") to $UNIT_DST"
for u in "${UNITS[@]}"; do
  sudo install -m 0644 "$UNIT_SRC/$u" "$UNIT_DST/$u"
done

chmod +x "$REPO_ROOT/deploy/cloud/scripts/backup.sh"

sudo systemctl daemon-reload

echo "[install] verifying unit file syntax"
sudo systemd-analyze verify "${UNITS[@]/#/$UNIT_DST/}" || {
  echo "[install] WARNING: systemd-analyze verify reported issues (see above)"
}

echo "[install] copy + reload complete."
echo "[install] Next step: test each service one-by-one before enabling timers:"
echo "          sudo systemctl start alpha-lab-clob.service && journalctl -u alpha-lab-clob.service -n 50 --no-pager"
echo "          (repeat for each .service, then run enable-timers.sh)"
