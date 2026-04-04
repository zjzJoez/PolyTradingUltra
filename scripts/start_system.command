#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

./.venv311/bin/python scripts/system_control.py start

echo
echo "Dashboard: http://127.0.0.1:8787/ops"
echo "System start requested."
read -r "?Press Enter to close..."
