#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

./.venv311/bin/python scripts/system_control.py stop

echo
echo "System stop requested."
read -r "?Press Enter to close..."
