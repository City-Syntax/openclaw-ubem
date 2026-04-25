"""
notify_dry_run.py — Dry-run Slack notification preview for a building.

Only two message types are posted to Slack:
  1. Calibration approval request  (--iteration N)
  2. Final intervention results     (default, always shown)

Usage:
    NUS_PROJECT_DIR=/Users/ye/nus-energy python3 notify_dry_run.py --building FOE1
    NUS_PROJECT_DIR=/Users/ye/nus-energy python3 notify_dry_run.py --building FOE1 --calibrated --cvrmse 7.2 --nmbe -1.8
    NUS_PROJECT_DIR=/Users/ye/nus-energy python3 notify_dry_run.py --building FOE6 --iteration 2
"""

import argparse
import json
import os
import sys
from pathlib import Path

NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))

# ── Load carbon scenarios ─────────────────────────────────────────────────

def load_carbon_scenarios(building):
    p = NUS_PROJECT_DIR / "outputs" / building / "carbon" / f"{building}_carbon_scenarios.json"
    if not p.exists():
        return None, []
    try:
        data = json.loads(p.read_text())
        return data, data.get("scenarios", [])
    except Exception:
        return None, []

# ── Message builders ──────────────────────────────────────────────────────

def msg_calibration_approval(building, iteration):
    """Type 1: calibration approval request — minimal, no values/diffs."""
    return (
        f"{building} 🔧 Recalibration needed (iteration {iteration})\n"
        f"Reply \"approve\" or \"reject\"."
    )

def msg_intervention_results(building, scenarios, calibrated=False):
    """Type 2: final intervention results — concise summary."""
    if not scenarios:
        return f"{building} ⚡ Intervention scenarios complete\nNo scenario data found."

    # Find best scenario by reduction_pct
    best = max(scenarios, key=lambda s: s.get("reduction_pct") or 0)
    best_name = best.get("name") or best.get("id") or "—"
    best_pct = best.get("reduction_pct") or 0
    best_co2 = best.get("co2_saved_tco2e") or best.get("carbon_saved_tco2e")

    icon = "✅" if calibrated else "⚠️"
    lines = [f"{building} {icon} Intervention scenarios complete"]

    # Per-tier summary
    tier_map = {"shallow": "🟢 Shallow", "medium": "🟡 Medium", "deep": "🔴 Deep"}
    for s in scenarios:
        sid = (s.get("id") or "").lower()
        label = tier_map.get(sid, s.get("name") or sid)
        pct = s.get("reduction_pct")
        if pct is not None:
            lines.append(f"{label}: {pct:.0f}% reduction")

    # Best call-out
    best_label = best.get("label") or best_name
    co2_str = f" — {best_co2:.0f} tCO₂e/yr saved" if best_co2 else ""
    lines.append(f"Best: {best_label}{co2_str}")

    if not calibrated:
        lines.append("⚠ Uncalibrated — treat savings as indicative")

    return "\n".join(lines)

# ── Print helper ──────────────────────────────────────────────────────────

def print_msg(channel, label, text):
    width = 62
    print(f"\n{'─'*width}")
    print(f"  → {channel}   [{label}]")
    print(f"{'─'*width}")
    for line in text.splitlines():
        print(f"  {line}")
    print(f"{'─'*width}")

# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Dry-run Slack notification preview (2 types only)")
    parser.add_argument("--building",   required=True)
    parser.add_argument("--calibrated", action="store_true", default=False)
    parser.add_argument("--cvrmse",     type=float, default=None)
    parser.add_argument("--nmbe",       type=float, default=None)
    parser.add_argument("--iteration",  type=int,   default=None,
                        help="Show calibration approval request for this iteration")
    args = parser.parse_args()

    b = args.building
    print(f"\nDry-run notification preview — {b}")
    print(f"(Only 2 Slack message types: calibration approval + final intervention results)\n")

    _, scenarios = load_carbon_scenarios(b)

    # Always show: final intervention results
    print_msg(
        "#openclaw-alerts",
        "Signal 📣 — final intervention results",
        msg_intervention_results(b, scenarios, calibrated=args.calibrated)
    )

    # Optional: calibration approval request
    if args.iteration is not None:
        print_msg(
            "#openclaw-alerts",
            "Signal 📣 — calibration approval request",
            msg_calibration_approval(b, args.iteration)
        )

    print()

if __name__ == "__main__":
    main()
