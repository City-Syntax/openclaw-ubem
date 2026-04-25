#!/usr/bin/env python3
"""
patch_idf.py — NUS Energy Calibration IDF Patcher
Applies approved parameter changes to IDF files with backup and audit log.

Usage:
  python3 patch_idf.py --building FOE6 \
    --set Infiltration_ACH=0.5 \
    --set Equipment_W_per_m2=10.0 \
    --iteration 1 \
    --approver "Ye"

Supported parameters:
  Infiltration_ACH       — ZoneInfiltration:DesignFlowRate ACH value (all zones)
  Lighting_W_per_m2      — Lights Watts_per_Zone_Floor_Area (all zones)
  Equipment_W_per_m2     — ElectricEquipment Watts_per_Zone_Floor_Area (all zones)

NOTE: Cooling_Setpoint_C is NOT a supported parameter. IDF setpoints reflect each
building's actual operational settings and must never be changed by any agent.
"""

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path

# ── Eppy (preferred for reliable field patching) ──────────────────────────
try:
    from eppy.modeleditor import IDF as EppyIDF
    EPPY_AVAILABLE = True
except ImportError:
    EppyIDF = None
    EPPY_AVAILABLE = False

# ── Paths ──────────────────────────────────────────────────────────────────
import os
PROJECT_DIR = Path(os.environ.get("NUS_PROJECT_DIR", Path(__file__).parents[3]))
IDF_DIR = PROJECT_DIR / "idfs"
CALIBRATION_IDF_DIR = PROJECT_DIR / "calibration_idfs"
LOG_FILE = PROJECT_DIR / "calibration_log.md"
BOUNDS_FILE = PROJECT_DIR / "parameter_bounds.json"

# ── Parameter bounds (loaded from file, fallback inline) ───────────────────
DEFAULT_BOUNDS = {
    "Infiltration_ACH":    {"min": 0.2,  "max": 6.0},
    "Lighting_W_per_m2":   {"min": 5.0,  "max": 20.0},
    "Equipment_W_per_m2":  {"min": 3.0,  "max": 60.0},
}

# ── Patch strategies ───────────────────────────────────────────────────────
# Each strategy is a function: (lines: list[str], value: float) -> (list[str], int changes)

def patch_infiltration_ach(lines, value):
    """Replace value on lines containing '!- @Zone_X-InfiltrationAch@'."""
    result = []
    changes = 0
    for line in lines:
        if "InfiltrationAch" in line and "!-" in line:
            new_line = re.sub(r"^\s*[\d.]+,", f"  {value},", line)
            result.append(new_line)
            if new_line != line:
                changes += 1
        else:
            result.append(line)
    return result, changes


def patch_lights(lines, value):
    """
    Replace Watts_per_Zone_Floor_Area field in Lights objects.
    Pattern in IDF: the 4th data field of a Lights block.
    We use a state-machine approach: detect 'Lights,' object, count fields.
    """
    result = []
    changes = 0
    in_lights = False
    field_count = 0

    for line in lines:
        stripped = line.strip()

        if re.match(r"^Lights\s*,", stripped):
            in_lights = True
            field_count = 0
            result.append(line)
            continue

        if in_lights:
            # Count comma-terminated or semicolon-terminated data lines (including blank-value lines like "  ,")
            if stripped and not stripped.startswith("!"):
                field_count += 1
                # Field 6 = LightingPowerDensity [W/m2]
                # Fields: 1=Name, 2=Zone, 3=Schedule, 4=Method, 5=DesignLevel(blank), 6=W/m²
                if field_count == 6:
                    is_last = stripped.endswith(";")
                    new_line = re.sub(
                        r"[\d.]+(\s*[,;])",
                        lambda m: f"{value}{m.group(1)}",
                        line,
                        count=1
                    )
                    result.append(new_line)
                    if new_line != line:
                        changes += 1
                    if is_last:
                        in_lights = False
                    continue
                if stripped.endswith(";"):
                    in_lights = False
        result.append(line)

    return result, changes


def patch_equipment(lines, value):
    """
    Replace Watts_per_Zone_Floor_Area field in ElectricEquipment objects.
    Field 4 = Watts_per_Floor_Area.
    """
    result = []
    changes = 0
    in_equip = False
    field_count = 0

    for line in lines:
        stripped = line.strip()

        if re.match(r"^ElectricEquipment\s*,", stripped):
            in_equip = True
            field_count = 0
            result.append(line)
            continue

        if in_equip:
            if stripped and not stripped.startswith("!"):
                field_count += 1
                # Field 6 = EquipmentPowerDensity [W/m2]
                # Fields: 1=Name, 2=Zone, 3=Schedule, 4=Method, 5=DesignLevel(blank), 6=W/m²
                if field_count == 6:
                    is_last = stripped.endswith(";")
                    new_line = re.sub(
                        r"[\d.]+(\s*[,;])",
                        lambda m: f"{value}{m.group(1)}",
                        line,
                        count=1
                    )
                    result.append(new_line)
                    if new_line != line:
                        changes += 1
                    if is_last:
                        in_equip = False
                    continue
                if stripped.endswith(";"):
                    in_equip = False
        result.append(line)

    return result, changes


PATCH_FUNCS = {
    "Infiltration_ACH":    patch_infiltration_ach,
    "Lighting_W_per_m2":   patch_lights,
    "Equipment_W_per_m2":  patch_equipment,
}

# ── Helpers ────────────────────────────────────────────────────────────────

def load_bounds():
    if BOUNDS_FILE.exists():
        with open(BOUNDS_FILE) as f:
            raw = json.load(f)
        bounds = {}
        for param in DEFAULT_BOUNDS:
            if param in raw:
                bounds[param] = {
                    "min": raw[param]["min"],
                    "max": raw[param]["max"],
                }
            else:
                bounds[param] = DEFAULT_BOUNDS[param]
        return bounds
    return DEFAULT_BOUNDS


def validate_bounds(param, value, bounds):
    b = bounds.get(param)
    if b is None:
        return True  # unknown param — skip bounds check
    if not (b["min"] <= value <= b["max"]):
        raise ValueError(
            f"{param}={value} is outside allowed range [{b['min']}, {b['max']}]"
        )


def get_calibration_idf_path(building: str) -> Path:
    """Return the canonical path for a calibrated IDF in calibration_idfs/."""
    CALIBRATION_IDF_DIR.mkdir(parents=True, exist_ok=True)
    return CALIBRATION_IDF_DIR / f"{building}.idf"


def backup_idf(idf_path: Path, iteration: int) -> Path:
    """Copy idf_path to a timestamped backup alongside the original."""
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = idf_path.with_name(f"{idf_path.stem}_iter{iteration}_backup_{ts}.idf")
    shutil.copy2(idf_path, backup_path)
    return backup_path


def append_log(building, iteration, approver, changes_applied, dry_run):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S SGT")
    tag = " [DRY RUN]" if dry_run else ""
    lines = [
        f"\n## {building} — Iteration {iteration} — {ts}{tag}\n",
        f"- Approver: {approver}\n",
    ]
    for param, old_val, new_val, n_changes in changes_applied:
        lines.append(f"- {param}: {old_val} → {new_val} ({n_changes} zones patched)\n")

    with open(LOG_FILE, "a") as f:
        f.writelines(lines)


# ── Main ───────────────────────────────────────────────────────────────────

def parse_set_args(set_args):
    """Parse ['Cooling_Setpoint_C=23.0', 'Infiltration_ACH=0.5'] → dict."""
    result = {}
    for s in set_args:
        if "=" not in s:
            raise argparse.ArgumentTypeError(f"Invalid --set format: '{s}' (expected PARAM=VALUE)")
        param, val = s.split("=", 1)
        param = param.strip()
        try:
            result[param] = float(val.strip())
        except ValueError:
            raise argparse.ArgumentTypeError(f"Non-numeric value for {param}: '{val}'")
    return result


def get_current_values(lines, params):
    """Extract current values for the given params from IDF lines (best-effort)."""
    current = {}

    for param in params:
        if param == "Infiltration_ACH":
            for line in lines:
                if "InfiltrationAch" in line and "!-" in line:
                    m = re.match(r"\s*([\d.]+)\s*,", line)
                    if m:
                        current[param] = float(m.group(1))
                        break

        elif param in ("Lighting_W_per_m2", "Equipment_W_per_m2"):
            obj_keyword = "Lights," if param == "Lighting_W_per_m2" else "ElectricEquipment,"
            in_obj = False
            field_count = 0
            for line in lines:
                stripped = line.strip()
                if re.match(rf"^{re.escape(obj_keyword.rstrip(','))}\s*,", stripped):
                    in_obj = True
                    field_count = 0
                    continue
                if in_obj and stripped and not stripped.startswith("!"):
                    field_count += 1
                    # Field 6 = W/m² (Name, Zone, Schedule, Method, DesignLevel-blank, W/m²)
                    if field_count == 6:
                        m = re.search(r"([\d.]+)", line)
                        if m:
                            current[param] = float(m.group(1))
                        break
                    if stripped.endswith(";"):
                        in_obj = False

    return current


def main():
    parser = argparse.ArgumentParser(description="Patch NUS IDF files with calibration parameters.")
    parser.add_argument("--building", required=True, help="Building ID, e.g. FOE6")
    parser.add_argument("--set", dest="sets", action="append", default=[],
                        help="PARAM=VALUE pair (repeat for multiple). E.g. --set Cooling_Setpoint_C=23.0")
    parser.add_argument("--iteration", type=int, default=1, help="Iteration number")
    parser.add_argument("--approver", default="unknown", help="Name of approver")
    parser.add_argument("--dry-run", action="store_true", help="Preview changes without writing")
    args = parser.parse_args()

    building = args.building.upper()

    # Resolve source IDF: prefer existing calibrated IDF (iterative calibration),
    # otherwise fall back to original in idfs/ (first iteration).
    calibrated_path = get_calibration_idf_path(building)
    if calibrated_path.exists():
        idf_path = calibrated_path
        print(f"  Source: calibrated IDF ({idf_path})")
    else:
        idf_path = IDF_DIR / f"{building}.idf"
        if not idf_path.exists():
            matches = list(IDF_DIR.rglob(f"{building}.idf"))
            if matches:
                idf_path = matches[0]
            else:
                print(f"ERROR: IDF not found: {IDF_DIR}/**/{building}.idf", file=sys.stderr)
                sys.exit(1)
        print(f"  Source: original IDF ({idf_path})")

    params = parse_set_args(args.sets)
    if not params:
        print("ERROR: No --set parameters provided.", file=sys.stderr)
        sys.exit(1)

    # Validate parameter names — block Cooling_Setpoint_C absolutely
    for param in params:
        if param == "Cooling_Setpoint_C":
            print("ERROR: Cooling_Setpoint_C must never be changed. "
                  "IDF setpoints reflect actual building operations.", file=sys.stderr)
            sys.exit(1)
        if param not in PATCH_FUNCS:
            print(f"ERROR: Unknown parameter '{param}'. Supported: {list(PATCH_FUNCS)}", file=sys.stderr)
            sys.exit(1)

    bounds = load_bounds()
    for param, value in params.items():
        try:
            validate_bounds(param, value, bounds)
        except ValueError as e:
            print(f"ERROR: Bounds violation — {e}", file=sys.stderr)
            sys.exit(1)

    # Read IDF
    with open(idf_path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    # Get current values for logging
    current_values = get_current_values(lines, params)

    # ── Dry run: regex preview only, no writes ────────────────────────────
    if args.dry_run:
        changes_applied = []
        for param, value in params.items():
            patch_fn = PATCH_FUNCS[param]
            _, n_changes = patch_fn(lines, value)
            old_val = current_values.get(param, "?")
            changes_applied.append((param, old_val, value, n_changes))
            print(f"  [DRY RUN] {param}: {old_val} → {value} ({n_changes} zones)")
        print("\nDry run complete — no files written.")
        return

    # ── Backup before any write ────────────────────────────────────────────
    backup_path = backup_idf(idf_path, args.iteration)
    print(f"\nBackup saved: {backup_path}")

    target_path = get_calibration_idf_path(building)
    if idf_path != target_path:
        shutil.copy2(idf_path, target_path)
        idf_path = target_path
        print(f"  Working copy: {idf_path}")

    # ── Apply patches via eppy (reliable field access) ─────────────────────
    if EPPY_AVAILABLE:
        IDD_FILE = "/Applications/EnergyPlus-23-1-0/Energy+.idd"
        EppyIDF.setiddname(IDD_FILE)
        idf_obj = EppyIDF(str(idf_path))
        changes_applied = []

        if "Infiltration_ACH" in params:
            value = params["Infiltration_ACH"]
            objs = idf_obj.idfobjects.get("ZONEINFILTRATION:DESIGNFLOWRATE", [])
            old_vals, n = [], 0
            for obj in objs:
                if str(obj.Design_Flow_Rate_Calculation_Method).strip() == "AirChanges/Hour":
                    old_vals.append(float(obj.Air_Changes_per_Hour or 0))
                    obj.Air_Changes_per_Hour = value
                    n += 1
            old = old_vals[0] if old_vals else "?"
            changes_applied.append(("Infiltration_ACH", old, value, n))
            print(f"  Patched Infiltration_ACH: {old} → {value} ({n} zones) [eppy]")

        if "Lighting_W_per_m2" in params:
            value = params["Lighting_W_per_m2"]
            objs = idf_obj.idfobjects.get("LIGHTS", [])
            old_vals, n = [], 0
            for obj in objs:
                if str(obj.Design_Level_Calculation_Method).strip() == "Watts/Area":
                    old_vals.append(float(obj.Watts_per_Zone_Floor_Area or 0))
                    obj.Watts_per_Zone_Floor_Area = value
                    n += 1
            old = old_vals[0] if old_vals else "?"
            changes_applied.append(("Lighting_W_per_m2", old, value, n))
            print(f"  Patched Lighting_W_per_m2: {old} → {value} ({n} zones) [eppy]")

        if "Equipment_W_per_m2" in params:
            value = params["Equipment_W_per_m2"]
            objs = idf_obj.idfobjects.get("ELECTRICEQUIPMENT", [])
            old_vals, n = [], 0
            for obj in objs:
                if str(obj.Design_Level_Calculation_Method).strip() == "Watts/Area":
                    old_vals.append(float(obj.Watts_per_Zone_Floor_Area or 0))
                    obj.Watts_per_Zone_Floor_Area = value
                    n += 1
            old = old_vals[0] if old_vals else "?"
            changes_applied.append(("Equipment_W_per_m2", old, value, n))
            print(f"  Patched Equipment_W_per_m2: {old} → {value} ({n} zones) [eppy]")

        idf_obj.save(str(idf_path))

    else:
        # ── Regex fallback (no eppy) ───────────────────────────────────────
        changes_applied = []
        for param, value in params.items():
            patch_fn = PATCH_FUNCS[param]
            lines, n_changes = patch_fn(lines, value)
            old_val = current_values.get(param, "?")
            changes_applied.append((param, old_val, value, n_changes))
            print(f"  Patched {param}: {old_val} → {value} ({n_changes} zones) [regex]")
        with open(idf_path, "w", encoding="utf-8") as f:
            f.writelines(lines)

    print(f"Patched IDF written: {idf_path}")

    # Log
    append_log(building, args.iteration, args.approver, changes_applied, dry_run=False)
    print(f"Change logged: {LOG_FILE}")
    print("\nDone. Next step: re-run simulation via nus-simulate skill.")


if __name__ == "__main__":
    main()
