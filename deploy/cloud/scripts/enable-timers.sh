#!/usr/bin/env bash
# Enable all Phase-D timers AFTER each corresponding .service has been
# successfully test-started (via `systemctl start <unit>.service` + journalctl).
#
# Usage: bash deploy/cloud/scripts/enable-timers.sh

set -eu -o pipefail

TIMERS=(
  mvp-autopilot.timer
  alpha-lab-odds.timer
  alpha-lab-clob.timer
  alpha-lab-cycle.timer
  alpha-lab-import.timer
  alpha-lab-clv.timer
  db-backup.timer
)

echo "[enable-timers] enabling + starting: ${TIMERS[*]}"
sudo systemctl enable --now "${TIMERS[@]}"

echo "[enable-timers] done. Current schedule:"
systemctl list-timers --all --no-pager | grep -E 'mvp-|alpha-lab-|db-backup' || true
