#!/bin/zsh
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

./.venv311/bin/python -m polymarket_mvp.control_plane --port 8788
