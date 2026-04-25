#!/usr/bin/env python3
"""
intervene.py — NUS Carbon Intervention Simulator
=================================================
Given a folder of IDFs and a target % reduction, this script:
  1. Reads baseline simulation outputs (already in outputs/)
  2. Proposes specific parameter changes per building (lighting, equipment, cooling setpoint)
  3. Patches copies of the IDFs into outputs/<building>/intervention/
  4. Re-runs EnergyPlus on the patched IDFs
  5. Compares baseline vs intervention energy → reports actual savings

Usage:
  python3 intervene.py --folder A1_H_L --target-pct 5
  python3 intervene.py --folder A1_H_L --target-pct 5 --buildings FOE6,FOE13
  python3 intervene.py --folder A1_H_L --target-pct 5 --dry-run

Parameters adjusted (in order of comfort/cost risk):
  1. Cooling_Setpoint_C  +1°C  → ~3-6% savings (ASHRAE: 1°C = ~3% cooling energy)
  2. Lighting_W_per_m2   -15%  → ~2-4% savings (LED retrofit assumption)
  3. Equipment_W_per_m2  -10%  → ~1-3% savings (smart strip/scheduling)
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from datetime import datetime

# ── Config ──────────────────────────────────────────────────────────────────
NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
IDF_BASE_DIR    = NUS_PROJECT_DIR / "idfs"
OUTPUTS_DIR     = NUS_PROJECT_DIR / "outputs"

# Ground-truth buildings per archetype folder (calibrated, metered validation)
GT_BUILDINGS = {
    "A1_H_L": ["FOE6", "FOE13", "FOE18", "FOS43", "FOS46"],
    "A1_L_L": ["FOE1", "FOE3", "FOE5", "FOE15", "FOE24", "FOE26", "FOS26"],
    "A1_M_H": ["FOS35", "FOS41", "FOS44"],
    "A1_M_L": ["FOE11", "FOE12", "FOE16", "FOE19", "FOE20", "FOE23"],
    "A5":     ["FOE10"],
}
SIMULATE_SCRIPT = Path(os.getenv(
    "SIMULATE_SCRIPT",
    "/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/scripts/simulate.py"
))
ENERGYPLUS_DIR  = Path(os.getenv("ENERGYPLUS_DIR", "/Applications/EnergyPlus-23-1-0"))
EPW_PATH        = NUS_PROJECT_DIR / "weather" / "SGP_Singapore.486980_IWEC.epw"

# Intervention parameters and their deltas
# Each entry: (param_name, delta_type, delta_value, description)
#   delta_type: "add" (absolute) or "scale" (multiply by factor)
INTERVENTIONS = [
    ("Cooling_Setpoint_C", "add",   +1.0,  "Raise cooling setpoint +1°C"),
    ("Lighting_W_per_m2",  "scale", 0.85,  "LED retrofit −15% lighting load"),
    ("Equipment_W_per_m2", "scale", 0.90,  "Smart scheduling −10% plug load"),
]

CARBON_FACTOR = 0.4168  # kgCO2e/kWh
TARIFF_SGD    = 0.28    # SGD/kWh


# ── IDF patching (inline, no dep on patch_idf.py) ───────────────────────────

def _read_idf(path: Path) -> list[str]:
    with open(path, encoding="utf-8", errors="replace") as f:
        return f.readlines()

def _write_idf(path: Path, lines: list[str]):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        f.writelines(lines)

def _get_value(lines, param):
    """Extract current value for a parameter from IDF lines."""
    if param == "Cooling_Setpoint_C":
        for line in lines:
            if "!- CoolingSetpoint" in line:
                m = re.search(r"([\d.]+)\s*;", line)
                if m:
                    return float(m.group(1))
    elif param == "Infiltration_ACH":
        for line in lines:
            if "InfiltrationAch" in line and "!-" in line:
                m = re.match(r"\s*([\d.]+)\s*,", line)
                if m:
                    return float(m.group(1))
    elif param == "Lighting_W_per_m2":
        # Field 6 in Lights block: LightingPowerDensity [W/m2]
        in_obj, fc = False, 0
        for line in lines:
            s = line.strip()
            if re.match(r"^Lights\s*,", s):
                in_obj, fc = True, 0
                continue
            if in_obj and s and not s.startswith("!"):
                fc += 1
                if fc == 6:
                    m = re.search(r"([\d.]+)", line)
                    if m:
                        return float(m.group(1))
                    break
                if s.endswith(";"):
                    in_obj = False
    elif param == "Equipment_W_per_m2":
        # Field 6 in ElectricEquipment block: EquipmentPowerDensity [W/m2]
        in_obj, fc = False, 0
        for line in lines:
            s = line.strip()
            if re.match(r"^ElectricEquipment\s*,", s):
                in_obj, fc = True, 0
                continue
            if in_obj and s and not s.startswith("!"):
                fc += 1
                if fc == 6:
                    m = re.search(r"([\d.]+)", line)
                    if m:
                        return float(m.group(1))
                    break
                if s.endswith(";"):
                    in_obj = False
    return None

def _patch_cooling(lines, value):
    result, changes = [], 0
    for line in lines:
        if "!- CoolingSetpoint" in line:
            new = re.sub(r"^\s*[\d.]+\s*;", f"  {value:.1f};", line)
            result.append(new)
            changes += (new != line)
        else:
            result.append(line)
    return result, changes

def _patch_infiltration(lines, value):
    result, changes = [], 0
    for line in lines:
        if "InfiltrationAch" in line and "!-" in line:
            new = re.sub(r"^\s*[\d.]+,", f"  {value:.3f},", line)
            result.append(new)
            changes += (new != line)
        else:
            result.append(line)
    return result, changes

def _patch_lights(lines, value):
    """Patch LightingPowerDensity [W/m2] — field 6 in NUS IDF format."""
    result, changes = [], 0
    in_obj, fc = False, 0
    for line in lines:
        s = line.strip()
        if re.match(r"^Lights\s*,", s):
            in_obj, fc = True, 0
            result.append(line)
            continue
        if in_obj and s and not s.startswith("!"):
            fc += 1
            if fc == 6:  # LightingPowerDensity [W/m2]
                new = re.sub(r"[\d.]+(\s*[,;])", lambda m: f"{value:.2f}{m.group(1)}", line, count=1)
                result.append(new)
                changes += (new != line)
                if s.endswith(";"): in_obj = False
                continue
            if s.endswith(";"): in_obj = False
        result.append(line)
    return result, changes

def _patch_equipment(lines, value):
    """Patch EquipmentPowerDensity [W/m2] — field 6 in NUS IDF format."""
    result, changes = [], 0
    in_obj, fc = False, 0
    for line in lines:
        s = line.strip()
        if re.match(r"^ElectricEquipment\s*,", s):
            in_obj, fc = True, 0
            result.append(line)
            continue
        if in_obj and s and not s.startswith("!"):
            fc += 1
            if fc == 6:  # EquipmentPowerDensity [W/m2]
                new = re.sub(r"[\d.]+(\s*[,;])", lambda m: f"{value:.2f}{m.group(1)}", line, count=1)
                result.append(new)
                changes += (new != line)
                if s.endswith(";"): in_obj = False
                continue
            if s.endswith(";"): in_obj = False
        result.append(line)
    return result, changes

PATCH_FNS = {
    "Cooling_Setpoint_C": _patch_cooling,
    "Infiltration_ACH":   _patch_infiltration,
    "Lighting_W_per_m2":  _patch_lights,
    "Equipment_W_per_m2": _patch_equipment,
}

BOUNDS = {
    "Cooling_Setpoint_C": (22.0, 26.0),
    "Infiltration_ACH":   (0.2, 6.0),
    "Lighting_W_per_m2":  (5.0, 20.0),
    "Equipment_W_per_m2": (3.0, 60.0),
}


def compute_new_value(param, current_val, delta_type, delta_val):
    if delta_type == "add":
        new = current_val + delta_val
    else:  # scale
        new = current_val * delta_val
    lo, hi = BOUNDS.get(param, (-1e9, 1e9))
    return max(lo, min(hi, new))


# ── Simulation helpers ───────────────────────────────────────────────────────

def run_simulation(idf_path: Path, output_subdir: str) -> dict:
    """Run simulate.py on a single IDF, output to outputs/<building>/<output_subdir>/."""
    building = idf_path.stem
    out_dir = OUTPUTS_DIR / building / output_subdir
    out_dir.mkdir(parents=True, exist_ok=True)

    env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}
    cmd = [
        sys.executable, str(SIMULATE_SCRIPT),
        "--idf", str(idf_path),
        "--output", str(OUTPUTS_DIR),  # simulate.py flag is --output, not --output-dir
    ]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300, env=env)
    return {
        "returncode": result.returncode,
        "stdout": result.stdout[-3000:] if result.stdout else "",
        "stderr": result.stderr[-1000:] if result.stderr else "",
    }


def read_annual_kwh(building: str, subdir: str = "parsed"):
    """Read annual kWh from monthly CSV."""
    p = OUTPUTS_DIR / building / subdir / f"{building}_monthly.csv"
    if not p.exists():
        return None
    import csv
    total = 0.0
    with open(p) as f:
        reader = csv.DictReader(f)
        for row in reader:
            # find the energy column
            for k, v in row.items():
                if "kwh" in k.lower() or "electricity" in k.lower():
                    try:
                        total += float(v)
                    except (ValueError, TypeError):
                        pass
                    break
    return total if total > 0 else None


# ── Main logic ───────────────────────────────────────────────────────────────

def process_building(building: str, src_idf: Path, target_pct: float, dry_run: bool) -> dict:
    """
    Patch one building's IDF, simulate, and return a result dict.
    """
    lines = _read_idf(src_idf)

    # Read current parameter values
    current_vals = {}
    proposed_vals = {}
    applied_changes = []

    for param, delta_type, delta_val, description in INTERVENTIONS:
        cur = _get_value(lines, param)
        if cur is None:
            continue
        new_val = compute_new_value(param, cur, delta_type, delta_val)
        current_vals[param] = cur
        proposed_vals[param] = new_val
        applied_changes.append({
            "param": param,
            "description": description,
            "from": round(cur, 3),
            "to": round(new_val, 3),
        })

    if not applied_changes:
        return {"building": building, "error": "No patchable parameters found in IDF"}

    if dry_run:
        return {
            "building": building,
            "dry_run": True,
            "changes": applied_changes,
        }

    # Write patched IDF to a temp location (never modify originals)
    patched_dir = OUTPUTS_DIR / building / "intervention" / "idfs"
    patched_dir.mkdir(parents=True, exist_ok=True)
    patched_idf = patched_dir / src_idf.name

    # Apply all patches in sequence
    for param, new_val in proposed_vals.items():
        fn = PATCH_FNS.get(param)
        if fn:
            lines, _ = fn(lines, new_val)
    _write_idf(patched_idf, lines)

    # Run baseline sim if parsed output missing
    baseline_kwh = read_annual_kwh(building, "parsed")
    if baseline_kwh is None:
        return {"building": building, "error": "No baseline simulation output — run Forge first"}

    # Run intervention simulation — overwrite outputs/<building>/parsed temporarily
    # Actually: run with a separate output root to avoid clobbering baseline
    # We simulate using the patched IDF and read from parsed dir after
    # simulate.py always writes to outputs/<building>/parsed/<building>_monthly.csv
    # So we back up, simulate, save result, restore.
    parsed_dir   = OUTPUTS_DIR / building / "parsed"
    baseline_csv = parsed_dir / f"{building}_monthly.csv"
    backup_csv   = parsed_dir / f"{building}_monthly_baseline.csv"

    # Backup baseline
    if baseline_csv.exists() and not backup_csv.exists():
        shutil.copy2(baseline_csv, backup_csv)

    # Run intervention sim
    sim_result = run_simulation(patched_idf, "intervention")
    intervention_kwh = read_annual_kwh(building, "parsed")

    # Restore baseline CSV
    if backup_csv.exists():
        shutil.copy2(backup_csv, baseline_csv)
        backup_csv.unlink()

    if intervention_kwh is None:
        return {
            "building": building,
            "error": f"Intervention simulation failed: {sim_result.get('stderr', '')[:200]}",
            "changes": applied_changes,
        }

    saving_kwh    = baseline_kwh - intervention_kwh
    saving_pct    = (saving_kwh / baseline_kwh * 100) if baseline_kwh > 0 else 0
    saving_tco2e  = saving_kwh * CARBON_FACTOR / 1000
    saving_sgd    = saving_kwh * TARIFF_SGD

    return {
        "building":        building,
        "baseline_kwh":    round(baseline_kwh),
        "intervention_kwh": round(intervention_kwh),
        "saving_kwh":      round(saving_kwh),
        "saving_pct":      round(saving_pct, 1),
        "saving_tco2e":    round(saving_tco2e, 1),
        "saving_sgd":      round(saving_sgd),
        "changes":         applied_changes,
        "meets_target":    saving_pct >= target_pct,
    }


def format_slack_report(results: list[dict], target_pct: float, folder: str) -> str:
    lines = [f"⚡ *Intervention Report — {folder} (target: {target_pct}% carbon reduction)*\n"]

    total_baseline   = sum(r.get("baseline_kwh", 0) for r in results if "saving_kwh" in r)
    total_saving_kwh = sum(r.get("saving_kwh", 0)   for r in results if "saving_kwh" in r)
    total_tco2e      = sum(r.get("saving_tco2e", 0) for r in results if "saving_tco2e" in r)
    total_sgd        = sum(r.get("saving_sgd", 0)   for r in results if "saving_sgd" in r)

    if total_baseline > 0:
        overall_pct = total_saving_kwh / total_baseline * 100
        lines.append(
            f"*Portfolio: {overall_pct:.1f}% reduction | "
            f"{total_tco2e:.0f} tCO₂e/yr saved | SGD {total_sgd:,}/yr*\n"
        )

    lines.append("")
    for r in results:
        b = r["building"]
        if "error" in r:
            lines.append(f"• *{b}* — ❌ {r['error']}")
            continue
        if r.get("dry_run"):
            lines.append(f"• *{b}* — [dry run]")
            for c in r.get("changes", []):
                lines.append(f"  └ {c['description']}: {c['from']} → {c['to']}")
            continue
        icon   = "✅" if r["meets_target"] else "⚠️"
        lines.append(
            f"{icon} *{b}* — {r['saving_pct']}% reduction "
            f"({r['saving_kwh']:,} kWh | {r['saving_tco2e']} tCO₂e | SGD {r['saving_sgd']:,})"
        )
        for c in r.get("changes", []):
            lines.append(f"  └ {c['description']}: {c['from']} → {c['to']}")

    lines.append("")
    lines.append("_Patched IDFs saved to `outputs/<building>/intervention/idfs/`. Originals unchanged._")
    return "\n".join(lines)


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NUS Carbon Intervention Simulator")
    parser.add_argument("--folder",      required=True, help="Archetype folder, e.g. A1_H_L")
    parser.add_argument("--target-pct",  type=float, default=5.0, help="Target % reduction (default 5)")
    parser.add_argument("--buildings",   help="Comma-separated building IDs to process (default: all in folder)")
    parser.add_argument("--dry-run",     action="store_true", help="Show proposed changes without simulating")
    parser.add_argument("--output-json", help="Write results to this JSON file")
    args = parser.parse_args()

    folder_path = IDF_BASE_DIR / args.folder
    if not folder_path.exists():
        print(f"ERROR: Folder not found: {folder_path}", file=sys.stderr)
        sys.exit(1)

    all_idfs = {p.stem: p for p in sorted(folder_path.glob("*.idf"))}
    if args.buildings:
        selected = [b.strip().upper() for b in args.buildings.split(",")]
    else:
        # Default to GT buildings only — these have metered validation
        selected = GT_BUILDINGS.get(args.folder, [])
        if not selected:
            print(f"WARNING: No GT buildings defined for folder {args.folder}, running all IDFs", file=sys.stderr)
            selected = list(all_idfs.keys())
        else:
            print(f"Using GT buildings for {args.folder}: {selected}", file=sys.stderr)

    all_idfs = {k: v for k, v in all_idfs.items() if k in selected}
    missing  = [b for b in selected if b not in all_idfs]
    if missing:
        print(f"WARNING: Buildings not found in folder: {missing}", file=sys.stderr)

    if not all_idfs:
        print("ERROR: No IDFs to process.", file=sys.stderr)
        sys.exit(1)

    print(f"Processing {len(all_idfs)} buildings in {args.folder} (target: {args.target_pct}%)...")
    results = []
    for building, idf_path in all_idfs.items():
        print(f"  → {building}...", flush=True)
        r = process_building(building, idf_path, args.target_pct, args.dry_run)
        results.append(r)
        if "saving_pct" in r:
            icon = "✅" if r["meets_target"] else "⚠️"
            print(f"     {icon} {r['saving_pct']}% reduction ({r['saving_kwh']:,} kWh)")
        elif "error" in r:
            print(f"     ❌ {r['error']}")

    report = format_slack_report(results, args.target_pct, args.folder)
    print("\n" + report)

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump({"folder": args.folder, "target_pct": args.target_pct, "results": results}, f, indent=2)
        print(f"\nResults written to {args.output_json}")

    return results


if __name__ == "__main__":
    main()
