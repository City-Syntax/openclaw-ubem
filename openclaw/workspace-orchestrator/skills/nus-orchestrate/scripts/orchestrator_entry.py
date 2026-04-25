#!/usr/bin/env python3
"""
orchestrator_entry.py — high-level NUS pipeline commands

Usage:
  python3 orchestrator_entry.py run-month 2024-08
  python3 orchestrator_entry.py run-year 2025
"""

import argparse
import subprocess
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
RUN_PIPELINE = SCRIPT_DIR / "run_pipeline.py"


def run_month(month: str) -> int:
    cmd = [sys.executable, str(RUN_PIPELINE), "--month", month]
    print(f"[orchestrator] Running monthly pipeline: {' '.join(cmd)}")
    return subprocess.call(cmd)


def run_year(year: int) -> int:
    codes = []
    for m in range(1, 13):
        month = f"{year}-{m:02d}"
        print(f"\n[orchestrator] ===== {month} =====")
        rc = run_month(month)
        codes.append((month, rc))
        if rc != 0:
            print(f"[orchestrator] ⚠ {month} exited with code {rc}")
    bad = [m for m, rc in codes if rc != 0]
    if bad:
        print(f"\n[orchestrator] Completed with errors in: {', '.join(bad)}")
        return 1
    print("\n[orchestrator] Year run complete with no errors.")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd", required=True)

    m = sub.add_parser("run-month", help="Run full pipeline for a given month (YYYY-MM)")
    m.add_argument("month", help="Month in YYYY-MM format")

    y = sub.add_parser("run-year", help="Run full pipeline for all 12 months")
    y.add_argument("year", type=int, help="Year, e.g. 2025")

    return p.parse_args()


def main() -> None:
    args = parse_args()
    if args.cmd == "run-month":
        sys.exit(run_month(args.month))
    elif args.cmd == "run-year":
        sys.exit(run_year(args.year))


if __name__ == "__main__":
    main()