from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from polymarket_mvp.tg_approver import control_system


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Control the local Polymarket system.")
    parser.add_argument("action", choices=["start", "stop", "restart"], help="Control action to run.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    action_map = {
        "start": "start_system",
        "stop": "stop_system",
        "restart": "restart_system",
    }
    result = control_system(action_map[args.action])
    print(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
