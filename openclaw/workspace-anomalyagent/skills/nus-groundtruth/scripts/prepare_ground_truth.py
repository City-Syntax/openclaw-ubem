"""
prepare_ground_truth.py
=======================
Parses ground-truth.csv and computes ASHRAE Guideline 14 calibration metrics
(CVRMSE, NMBE) for all GT buildings, using the correct EUI comparison basis.

COMPARISON BASIS
----------------
  Sim  : eui_kwh_m2  = electricity_facility_kwh / floor_area_m2
          (lighting + equipment + own-chiller HVAC electricity, per m²/month)
  GT   : Section 3 "Electricity Consumption" (cols 27-38, Jan-Dec 2024)
          = total electricity kWh from building meter (includes cooling electricity)
          / floor_area_m2  → monthly GT EUI (kWh/m²/month)

  Do NOT use eui_adj_kwh_m2 (adds district-cooling thermal via COP — overcounts).
  Do NOT compare annual totals — use monthly CVRMSE per ASHRAE Guideline 14.

GROUND-TRUTH CSV STRUCTURE (ground-truth.csv)
---------------------------------------------
  Row 0 (header 1): section labels at cols 2, 14, 27
    col 2:  "Electrical Consumption (kWh)"   — elec only (non-cooling)
    col 14: "BTU (Cooling) Consumption (kWh)" — district cooling thermal
    col 27: "Electricity Consumption (kWh)"   — total elec incl cooling ← USE THIS
  Row 1 (header 2): FY24 markers
  Row 2 (header 3): ID, Building Name, Jan-24..Dec-24 [×3 sections, sep by NaN]
  Rows 3-28: one building per row
  Rows 29+:  notes / blank

Section column ranges (0-indexed):
  Section 1 (Electrical):   cols 2-13  (Jan-24 to Dec-24, 12 months)
  Section 2 (BTU Cooling):  cols 14-25 (24-Jan to 24-Dec, 12 months) [separator col 13→nan]
  Section 3 (Electricity):  cols 27-38 (24-Jan to 24-Dec, 12 months) [separator col 26→nan]

ASHRAE Guideline 14 acceptance (monthly basis, must pass both):
    CVRMSE <= 15%   (coefficient of variation of RMSE)
    NMBE   <= ±5%   (normalised mean bias error)
MAPE retained as informational metric only.

Usage:
    NUS_PROJECT_DIR=/Users/ye/nus-energy python3 prepare_ground_truth.py
"""

import csv
import json
import math
import os
from pathlib import Path

# ── Config ────────────────────────────────────────────────────────────────────
_PROJECT         = Path(os.environ.get("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
GROUND_TRUTH_CSV = _PROJECT / "ground_truth" / "ground-truth.csv"
PARSED_DIR       = _PROJECT / "outputs"
REGISTRY_PATH    = _PROJECT / "building_registry.json"
CARBON           = 0.4168   # kgCO2e/kWh (EMA Singapore 2023)

# Section 3 column range (Electricity Consumption, total incl. cooling)
GT_SECTION3_COLS = list(range(27, 39))   # 12 columns: Jan-Dec 2024
GT_MONTHS        = [f"2024-{i:02d}" for i in range(1, 13)]

# ── Archetype → GT building mapping ──────────────────────────────────────────
ARCHETYPE_GT = {
    "A1_H_L": ["FOE6",  "FOE9",  "FOE13", "FOE18", "FOS43", "FOS46"],
    "A1_L_L": ["FOE1",  "FOE3",  "FOE5",  "FOE15", "FOE24", "FOE26", "FOS26"],
    "A1_M_H": ["FOS35", "FOS41", "FOS44"],
    "A1_M_L": ["FOE11", "FOE12", "FOE16", "FOE19", "FOE20", "FOE23"],
    "A5":     ["FOE10"],
}
ALL_GT_BUILDINGS   = {b for bs in ARCHETYPE_GT.values() for b in bs}
BUILDING_ARCHETYPE = {b: a for a, bs in ARCHETYPE_GT.items() for b in bs}


# ── Helpers ───────────────────────────────────────────────────────────────────
def parse_kwh(val) -> "float | None":
    v = str(val).strip().replace('"', "").replace(",", "").replace(" ", "")
    if v in ("-", "", "nan", "- "): return None
    try: return float(v)
    except: return None


# ── Ground-truth loader ───────────────────────────────────────────────────────
def load_ground_truth() -> "dict[str, list[float | None]]":
    """
    Parse Section 3 (Electricity Consumption, cols 27-38) from ground-truth.csv.
    Returns {building_id: [jan_kwh, feb_kwh, ..., dec_kwh]}  (12 values, may have None).
    """
    with open(GROUND_TRUTH_CSV, encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    gt = {}
    for row in rows[3:]:          # data starts at row index 3
        if not row or len(row) < 39:
            continue
        bid = str(row[0]).strip()
        if bid in ("/", "PGP?", "", "nan") or not bid:
            continue
        if bid not in ALL_GT_BUILDINGS:
            continue
        monthly = [parse_kwh(row[c]) for c in GT_SECTION3_COLS]
        gt[bid] = monthly
    return gt


# ── Sim loader ────────────────────────────────────────────────────────────────
def load_simulated(building: str) -> "dict[int, float] | None":
    """
    Load parsed monthly CSV → {month_int: eui_kwh_m2}.
    Uses eui_kwh_m2 = electricity_facility_kwh / floor_area_m2.
    Month int: 1=Jan .. 12=Dec.
    """
    path = PARSED_DIR / building / "parsed" / f"{building}_monthly.csv"
    if not path.exists():
        return None
    result = {}
    with open(path) as f:
        for row in csv.DictReader(f):
            if row.get("month_name", "").upper() == "ANNUAL":
                continue
            try:
                m = int(row["month"])
                val = row.get("eui_kwh_m2")
                result[m] = float(val)
            except (KeyError, ValueError, TypeError):
                continue
    return result or None


# ── Metrics ───────────────────────────────────────────────────────────────────
def calc_metrics(building: str,
                 gt_monthly_kwh: "list[float | None]",
                 sim_monthly_eui: "dict[int, float]",
                 floor_area_m2: float) -> "dict | None":
    """
    Compute CVRMSE and NMBE on a monthly EUI basis (kWh/m²/month).

    GT EUI  = gt_monthly_kwh[i] / floor_area_m2   for each month i
    Sim EUI = sim_monthly_eui[month_int]           (already per m²)

    ASHRAE Guideline 14 monthly CVRMSE and NMBE:
      NMBE   = Σ(sim_i - meas_i) / Σmeas_i * 100
      CVRMSE = sqrt(Σ(sim_i - meas_i)² / n) / mean(meas) * 100
    """
    pairs = []
    for i, kwh in enumerate(gt_monthly_kwh):
        month_int = i + 1
        if kwh is None or month_int not in sim_monthly_eui:
            continue
        if floor_area_m2 <= 0:
            continue
        gt_eui  = kwh / floor_area_m2
        sim_eui = sim_monthly_eui[month_int]
        pairs.append((gt_eui, sim_eui))

    if len(pairs) < 6:
        return None

    meas_vals = [p[0] for p in pairs]
    sim_vals  = [p[1] for p in pairs]
    errors    = [s - m for s, m in zip(sim_vals, meas_vals)]
    n         = len(errors)

    mean_meas = sum(meas_vals) / n
    if mean_meas == 0:
        return None

    nmbe   = sum(errors) / sum(meas_vals) * 100
    rmse   = math.sqrt(sum(e ** 2 for e in errors) / n)
    cvrmse = rmse / mean_meas * 100

    # MAPE (informational only — not used for ASHRAE pass/fail)
    mape = (sum(abs(e) / m for e, m in zip(errors, meas_vals) if m != 0) / n * 100)

    return {
        "building":          building,
        "n_months":          n,
        "annual_gt_kwh":     sum(meas_vals) * floor_area_m2,
        "annual_sim_kwh":    sum(sim_vals)  * floor_area_m2,
        "annual_gt_eui":     round(sum(meas_vals), 2),
        "annual_sim_eui":    round(sum(sim_vals),  2),
        "mape_pct":          round(mape,   2),
        "nmbe_pct":          round(nmbe,   2),
        "cvrmse_pct":        round(cvrmse, 2),
        "ashrae_cvrmse_pass": abs(cvrmse) <= 15.0,
        "ashrae_nmbe_pass":   abs(nmbe)   <= 5.0,
        "calibrated":        abs(cvrmse) <= 15.0 and abs(nmbe) <= 5.0,
        "monthly_pairs":     [
            {"month": i + 1, "gt_eui": round(g, 4), "sim_eui": round(s, 4),
             "error_pct": round((s - g) / g * 100, 2) if g else None}
            for i, (g, s) in enumerate(pairs)
        ],
    }


# ── Per-building CSV writer ───────────────────────────────────────────────────
def write_per_building_gt(building: str, monthly_kwh: "list[float | None]"):
    """Write parsed/{building}_ground_truth.csv (month, measured_kwh)."""
    out_dir = PARSED_DIR / building / "parsed"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{building}_ground_truth.csv"
    # Also write to legacy ground_truth/parsed/ for backward compat
    leg_dir  = _PROJECT / "ground_truth" / "parsed"
    leg_dir.mkdir(parents=True, exist_ok=True)
    leg_path = leg_dir / f"{building}_ground_truth.csv"

    rows = []
    for i, kwh in enumerate(monthly_kwh):
        rows.append({"month": f"2024-{i+1:02d}", "measured_kwh": kwh if kwh is not None else ""})

    for path in (out_path, leg_path):
        with open(path, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=["month", "measured_kwh"])
            w.writeheader()
            w.writerows(rows)


# ── Main ──────────────────────────────────────────────────────────────────────
def main():
    if not GROUND_TRUTH_CSV.exists():
        raise FileNotFoundError(f"Ground truth not found: {GROUND_TRUTH_CSV}")
    if not REGISTRY_PATH.exists():
        raise FileNotFoundError(f"Registry not found: {REGISTRY_PATH}")

    registry = json.loads(REGISTRY_PATH.read_text())
    gt_data  = load_ground_truth()

    print(f"Ground-truth loaded: {len(gt_data)} buildings")
    print(f"Comparison basis: sim eui_kwh_m2  vs  GT electricity kWh / floor_area_m2\n")

    all_metrics    = {}
    archetype_aggs = {}

    for archetype, buildings in sorted(ARCHETYPE_GT.items()):
        print(f"── {archetype} ──")
        arch_metrics = []

        for building in buildings:
            gt_monthly = gt_data.get(building)
            if gt_monthly is None:
                print(f"  {building:<10} ⚠️  no GT data")
                continue

            floor_area = registry.get(building, {}).get("floor_area_m2")
            if not floor_area or floor_area <= 0:
                print(f"  {building:<10} ⚠️  no floor area in registry")
                continue

            sim = load_simulated(building)
            if sim is None:
                print(f"  {building:<10} ⚠️  no simulation output")
                write_per_building_gt(building, gt_monthly)
                continue

            m = calc_metrics(building, gt_monthly, sim, floor_area)
            if m is None:
                print(f"  {building:<10} ⚠️  insufficient overlapping months")
                write_per_building_gt(building, gt_monthly)
                continue

            write_per_building_gt(building, gt_monthly)

            # Save per-building JSON
            out_path = PARSED_DIR / building / "parsed" / f"{building}_calibration_metrics.json"
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(m, indent=2))

            status = "✅ PASS" if m["calibrated"] else "❌ CALIBRATE"
            print(f"  {building:<10} CVRMSE={m['cvrmse_pct']:>6.2f}%  "
                  f"NMBE={m['nmbe_pct']:>+7.2f}%  "
                  f"GT EUI={m['annual_gt_eui']:>7.1f}  "
                  f"Sim EUI={m['annual_sim_eui']:>7.1f}  {status}")

            all_metrics[building] = m
            arch_metrics.append(m)

        # Archetype aggregate (simple average across GT buildings with data)
        if arch_metrics:
            agg = {
                "archetype":             archetype,
                "n_gt_buildings":        len(arch_metrics),
                "avg_cvrmse_pct":        round(sum(x["cvrmse_pct"]  for x in arch_metrics) / len(arch_metrics), 2),
                "avg_nmbe_pct":          round(sum(x["nmbe_pct"]    for x in arch_metrics) / len(arch_metrics), 2),
                "avg_mape_pct":          round(sum(x["mape_pct"]    for x in arch_metrics) / len(arch_metrics), 2),
                "ashrae_cvrmse_pass":    all(x["ashrae_cvrmse_pass"] for x in arch_metrics),
                "ashrae_nmbe_pass":      all(x["ashrae_nmbe_pass"]   for x in arch_metrics),
                "representative_building": min(arch_metrics, key=lambda x: x["cvrmse_pct"])["building"],
            }
            archetype_aggs[archetype] = agg

            agg_dir  = _PROJECT / "ground_truth" / archetype
            agg_dir.mkdir(parents=True, exist_ok=True)
            (agg_dir / f"{archetype}_calibration_aggregate.json").write_text(json.dumps(agg, indent=2))

            status = "✅ PASS" if (agg["ashrae_cvrmse_pass"] and agg["ashrae_nmbe_pass"]) else "⚠️  CALIBRATE"
            print(f"\n  → {archetype} aggregate: CVRMSE={agg['avg_cvrmse_pct']:.1f}%  "
                  f"NMBE={agg['avg_nmbe_pct']:+.1f}%  {status}  "
                  f"(representative: {agg['representative_building']})\n")

    # ── Summary ───────────────────────────────────────────────────────────────
    calibrated   = [b for b, m in all_metrics.items() if m["calibrated"]]
    needs_cal    = [b for b, m in all_metrics.items() if not m["calibrated"]]

    print(f"\n{'='*65}")
    print(f"  CAMPUS SUMMARY — {len(all_metrics)} buildings with simulation data")
    print(f"{'='*65}")
    print(f"  {'Building':<10} {'Archetype':<10} {'CVRMSE':>8} {'NMBE':>8}  Status")
    print(f"  {'-'*52}")
    for arch, buildings in sorted(ARCHETYPE_GT.items()):
        for b in sorted(buildings):
            if b in all_metrics:
                m = all_metrics[b]
                status = "PASS" if m["calibrated"] else "CALIBRATE"
                print(f"  {b:<10} {arch:<10} {m['cvrmse_pct']:>7.2f}%  {m['nmbe_pct']:>+7.2f}%  {status}")

    print(f"\n  ✅ Calibrated ({len(calibrated)}): {calibrated}")
    print(f"  ❌ Needs calibration ({len(needs_cal)}): {needs_cal}")
    print(f"{'='*65}\n")


if __name__ == "__main__":
    main()
