from __future__ import annotations

import argparse

from .poly_executor import main as poly_executor_main


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Backward-compatible mock executor wrapper.")
    parser.add_argument("--proposal-file", required=True, help="Path to proposal JSON.")
    parser.add_argument("--output", help="Optional file path for execution summary.")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    argv = ["--proposal-file", args.proposal_file, "--mode", "mock"]
    if args.output:
        argv.extend(["--output", args.output])

    import sys

    original = sys.argv
    try:
        sys.argv = [original[0], *argv]
        return poly_executor_main()
    finally:
        sys.argv = original


if __name__ == "__main__":
    raise SystemExit(main())
