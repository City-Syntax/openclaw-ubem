#!/usr/bin/env python3
"""
query.py — NUS Energy Query Script
Oracle (QueryAgent) uses this to answer data questions from the facilities team.

Usage:
  python3 query.py --summary
  python3 query.py --building FOE13 --metric mape
  python3 query.py --ranking mape
  python3 query.py --campus-carbon
  python3 query.py --bca-gap
"""

import argparse
import json
import os
import sys
from pathlib import Path

import pandas as pd

# ── Config ─────────────────────────────────────────────────────────────────────
NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
OUTPUTS_DIR     = NUS_PROJECT_DIR / "outputs"
GROUND_TRUTH_DIR = NUS_PROJECT_DIR / "ground_truth" / "parsed"
REGISTRY_FILE   = NUS_PROJECT_DIR / "building_registry.json"

CARBON_FACTOR   = 0.4168   # kgCO2e/kWh (EMA Singapore 2023)
TARIFF_SGD      = 0.28     # SGD/kWh (SP Group commercial ~2024)
MAPE_THRESHOLD  = 15.0     # %
MAPE_CRITICAL   = 25.0     # %

CALIBRATED_BUILDINGS = ["FOE6", "FOE9", "FOE13", "FOE18", "FOS43", "FOS46"]

BCA_BENCHMARKS = {
    "Platinum":  85,
    "Gold Plus": 100,
    "Gold":      115,
    "Certified": 130,
}

# ── Helpers ────────────────────────────────────────────────────────────────────

def load_registry() -> dict:
    if not REGISTRY_FILE.exists():
        return {}
    with open(REGISTRY_FILE) as f:
        return json.load(f)


def load_monthly_csv(building: str) -> "pd.DataFrame | None":
    p = OUTPUTS_DIR / building / "parsed" / f"{building}_monthly.csv"
    if not p.exists():
        return None
    return pd.read_csv(p)


def load_ground_truth(building: str) -> "pd.DataFrame | None":
    """Try to find ground truth CSV for a building."""
    candidates = list(GROUND_TRUTH_DIR.glob(f"{building}*.csv"))
    if not candidates:
        return None
    return pd.read_csv(candidates[0])


def mape_status_icon(mape: float) -> str:
    if mape < MAPE_THRESHOLD:
        return "✅"
    elif mape < MAPE_CRITICAL:
        return "⚠️"
    else:
        return "🔴"


def compute_mape(simulated: pd.Series, measured: pd.Series) -> float:
    """Compute Mean Absolute Percentage Error."""
    mask = measured != 0
    return float(((simulated[mask] - measured[mask]).abs() / measured[mask].abs()).mean() * 100)


def align_sim_gt(sim: pd.DataFrame, gt: pd.DataFrame):
    """
    Align simulated and ground truth DataFrames on calendar month (1-12).
    Sim has integer 'month' column (1-12).
    GT has 'month' in YYYY-MM format; extract month number, average across years.
    Returns (sim_kwh Series, gt_kwh Series) aligned by month.
    """
    sim_col = next((c for c in sim.columns if "electricity_facility" in c.lower()), None)
    if sim_col is None:
        sim_col = next((c for c in sim.columns if "kwh" in c.lower() or "electricity" in c.lower()), None)
    gt_col = next((c for c in gt.columns if "measured_kwh" in c.lower()), None)
    if gt_col is None:
        gt_col = next((c for c in gt.columns if "kwh" in c.lower()), None)
    if sim_col is None or gt_col is None:
        return None, None

    # Sim: use 'month' column (1-12) as index
    sim_indexed = sim.set_index("month")[[sim_col]].copy() if "month" in sim.columns else sim[[sim_col]].copy()
    sim_indexed.index = sim_indexed.index.astype(int)

    # GT: extract month number, average across years
    gt2 = gt.copy()
    gt2["month_num"] = pd.to_datetime(gt2["month"]).dt.month
    gt_indexed = gt2.groupby("month_num")[gt_col].mean()

    # Inner join on month number
    merged = sim_indexed.join(gt_indexed, how="inner")
    merged.columns = ["simulated_kwh", "measured_kwh"]
    merged = merged.dropna()
    return merged["simulated_kwh"], merged["measured_kwh"]


# ── Commands ───────────────────────────────────────────────────────────────────

def cmd_building_status(building: str, metric: str = "all"):
    """Print status for a single building."""
    sim = load_monthly_csv(building)
    if sim is None:
        print(f"{building}: No simulation output found. Run Forge first.")
        return

    # Try MAPE if ground truth exists
    gt = load_ground_truth(building)
    if gt is not None and metric in ("mape", "all"):
        try:
            sim_s, gt_s = align_sim_gt(sim, gt)
            if sim_s is not None and len(sim_s) > 0:
                mape = compute_mape(sim_s, gt_s)
                icon = mape_status_icon(mape)
                print(f"{building} {icon} MAPE {mape:.1f}%")
                elec_col = next((c for c in sim.columns if "electricity_facility" in c), None) or \
                           next((c for c in sim.columns if "kwh" in c.lower()), None)
                total_sim = sim[elec_col].sum() if elec_col else sim_s.sum()
                print(f"  Annual simulated: {total_sim:,.0f} kWh")
                carbon = total_sim * CARBON_FACTOR / 1000
                cost   = total_sim * TARIFF_SGD
                print(f"  Carbon: {carbon:.0f} tCO2e/year")
                print(f"  Cost:   SGD {cost:,.0f}/year")
                return
        except Exception as e:
            print(f"{building}: Error computing MAPE — {e}")
            return

    # No ground truth — just report simulation totals
    energy_col = [c for c in sim.columns if "kwh" in c.lower() or "energy" in c.lower() or "electricity" in c.lower()]
    if energy_col:
        total = sim[energy_col[0]].sum()
        carbon = total * CARBON_FACTOR / 1000
        cost   = total * TARIFF_SGD
        print(f"{building} 📊 (no ground truth for MAPE)")
        print(f"  Annual simulated: {total:,.0f} kWh")
        print(f"  Carbon: {carbon:.0f} tCO2e/year")
        print(f"  Cost:   SGD {cost:,.0f}/year")
    else:
        print(f"{building}: Simulation output found but energy column not identified.")
        print(f"  Columns: {list(sim.columns)}")


def cmd_ranking(metric: str = "mape"):
    """Print ranking for all simulated buildings by the given metric."""
    registry = load_registry()
    all_buildings = [k for k in registry.keys() if not k.startswith("_")] if registry else CALIBRATED_BUILDINGS

    if metric == "eui":
        rows = []
        for b in all_buildings:
            sim = load_monthly_csv(b)
            if sim is None:
                continue
            energy_col = [c for c in sim.columns if "kwh" in c.lower() or "energy" in c.lower() or "electricity" in c.lower()]
            if not energy_col:
                continue
            total_kwh = sim[energy_col[0]].sum()
            meta = registry.get(b, {})
            area = meta.get("floor_area_m2") or meta.get("gfa_m2") or meta.get("area_m2")
            if not area:
                rows.append((b, None, "no floor area"))
                continue
            eui = total_kwh / area
            tier = next((t for t, thresh in BCA_BENCHMARKS.items() if eui <= thresh), None)
            icon = "✅" if tier else "🔴"
            rows.append((b, eui, icon))

        available = [(b, v, i) for b, v, i in rows if v is not None]
        unavailable = [(b, v, i) for b, v, i in rows if v is None]
        available.sort(key=lambda x: x[1])

        print(f"EUI ranking ({len(available)} buildings with simulation + floor area):")
        for rank, (b, eui, icon) in enumerate(available, 1):
            tier = next((t for t, thresh in BCA_BENCHMARKS.items() if eui <= thresh), "below Certified")
            print(f"  {rank}. {b:<8} {icon}  {eui:.0f} kWh/m²/yr  ({tier})")
        for b, _, reason in unavailable:
            print(f"  —  {b:<8} ❓  {reason}")
        return

    # Default: MAPE ranking (calibrated buildings only)
    results = []
    for b in CALIBRATED_BUILDINGS:
        sim = load_monthly_csv(b)
        gt  = load_ground_truth(b)
        if sim is None or gt is None:
            results.append((b, None, "no data"))
            continue
        try:
            sim_s, gt_s = align_sim_gt(sim, gt)
            if sim_s is not None and len(sim_s) > 0:
                mape = compute_mape(sim_s, gt_s)
                results.append((b, mape, mape_status_icon(mape)))
            else:
                results.append((b, None, "column not found"))
        except Exception as e:
            results.append((b, None, f"error: {e}"))

    available = [(b, m, i) for b, m, i in results if m is not None]
    unavailable = [(b, m, i) for b, m, i in results if m is None]
    available.sort(key=lambda x: x[1])

    print(f"MAPE ranking ({len(CALIBRATED_BUILDINGS)} calibrated buildings):")
    for rank, (b, mape, icon) in enumerate(available, 1):
        print(f"  {rank}. {b:<8} {icon}  MAPE {mape:.1f}%")
    for b, _, reason in unavailable:
        print(f"  —  {b:<8} ❓  {reason}")


def cmd_summary():
    """Campus-wide summary across all buildings with simulation output."""
    registry = load_registry()
    all_buildings = [k for k in registry.keys() if not k.startswith("_")] if registry else []

    found, totals = [], []
    for b in all_buildings:
        sim = load_monthly_csv(b)
        if sim is None:
            continue
        energy_col = [c for c in sim.columns if "kwh" in c.lower() or "energy" in c.lower() or "electricity" in c.lower()]
        if energy_col:
            found.append(b)
            totals.append(sim[energy_col[0]].sum())

    if not found:
        print("No simulation outputs found. Run Forge first.")
        return

    total_kwh = sum(totals)
    total_carbon = total_kwh * CARBON_FACTOR / 1000
    total_cost   = total_kwh * TARIFF_SGD

    print(f"📊 NUS Campus Energy Summary ({len(found)}/{len(all_buildings)} buildings simulated)")
    print(f"  Total annual energy: {total_kwh:,.0f} kWh")
    print(f"  Total carbon:        {total_carbon:,.0f} tCO2e/year")
    print(f"  Total cost:          SGD {total_cost:,.0f}/year")
    print()
    print(f"  Buildings simulated: {', '.join(found)}")
    print(f"  Buildings missing:   {', '.join(b for b in all_buildings if b not in found) or 'none'}")


def cmd_bca_gap():
    """Show gap to each BCA Green Mark tier for simulated buildings."""
    registry = load_registry()
    all_buildings = [k for k in registry.keys() if not k.startswith("_")] if registry else []

    rows = []
    for b in all_buildings:
        sim = load_monthly_csv(b)
        if sim is None:
            continue
        energy_col = [c for c in sim.columns if "kwh" in c.lower() or "energy" in c.lower() or "electricity" in c.lower()]
        if not energy_col:
            continue
        total_kwh = sim[energy_col[0]].sum()
        # Try to get floor area from registry
        meta = registry.get(b, {})
        area = meta.get("floor_area_m2") or meta.get("gfa_m2") or meta.get("area_m2")
        if not area:
            rows.append((b, total_kwh, None))
            continue
        eui = total_kwh / area
        rows.append((b, total_kwh, eui))

    print("BCA Green Mark gap analysis:")
    for b, kwh, eui in rows:
        if eui is None:
            print(f"  {b:<8} — floor area missing in registry, cannot compute EUI")
            continue
        tier = next((t for t, thresh in BCA_BENCHMARKS.items() if eui <= thresh), None)
        if tier:
            print(f"  {b:<8} EUI {eui:.0f} kWh/m²/yr ✅ meets BCA {tier}")
        else:
            gap = eui - BCA_BENCHMARKS["Certified"]
            print(f"  {b:<8} EUI {eui:.0f} kWh/m²/yr 🔴 {gap:.0f} above BCA Certified")


def cmd_campus_carbon():
    """Total campus carbon footprint."""
    registry = load_registry()
    all_buildings = [k for k in registry.keys() if not k.startswith("_")] if registry else []

    total_kwh = 0
    count = 0
    for b in all_buildings:
        sim = load_monthly_csv(b)
        if sim is None:
            continue
        energy_col = [c for c in sim.columns if "kwh" in c.lower() or "energy" in c.lower() or "electricity" in c.lower()]
        if energy_col:
            total_kwh += sim[energy_col[0]].sum()
            count += 1

    if count == 0:
        print("No simulation data found. Run Forge first.")
        return

    carbon = total_kwh * CARBON_FACTOR / 1000
    cost   = total_kwh * TARIFF_SGD
    print(f"🌍 NUS Campus Carbon Footprint ({count} buildings)")
    print(f"  Annual energy:  {total_kwh:,.0f} kWh")
    print(f"  Carbon:         {carbon:,.0f} tCO2e/year")
    print(f"  Cost:           SGD {cost:,.0f}/year")
    print(f"  Carbon factor:  0.4168 kgCO2e/kWh (EMA Singapore 2023)")


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NUS Energy Query Tool")
    parser.add_argument("--building",      help="Building code (e.g. FOE13)")
    parser.add_argument("--metric",        default="all", help="Metric: mape, energy, all")
    parser.add_argument("--ranking",       metavar="METRIC", nargs="?", const="mape",
                        help="Show ranking by metric (default: mape)")
    parser.add_argument("--summary",       action="store_true", help="Campus-wide summary")
    parser.add_argument("--bca-gap",       action="store_true", help="BCA Green Mark gap analysis")
    parser.add_argument("--campus-carbon", action="store_true", help="Campus carbon footprint")

    args = parser.parse_args()

    if args.summary:
        cmd_summary()
    elif args.bca_gap:
        cmd_bca_gap()
    elif args.campus_carbon:
        cmd_campus_carbon()
    elif args.ranking is not None:
        cmd_ranking(args.ranking)
    elif args.building:
        cmd_building_status(args.building, args.metric)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
