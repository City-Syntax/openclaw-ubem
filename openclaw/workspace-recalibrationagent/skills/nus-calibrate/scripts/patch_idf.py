#!/usr/bin/env python3
"""
patch_idf.py — NUS Energy Calibration IDF Patcher
Applies approved parameter changes to IDF files with backup and audit log.

Usage:
  python3 patch_idf.py --building FOE6 \
    --set Infiltration_ACH=0.5 \
    --set Equipment_W_per_m2=10.0 \
    --set Occupancy_density=0.05 \
    --set Ventilation_rate_m3_s_m2=0.0006 \
    --set Chiller_COP=5.2 \
    --iteration 1 \
    --approver "Ye"

Supported parameters:
  Infiltration_ACH            — ZoneInfiltration:DesignFlowRate ACH value (all zones)
  Lighting_W_per_m2           — Lights Watts_per_Zone_Floor_Area (all zones)
  Equipment_W_per_m2          — ElectricEquipment Watts_per_Zone_Floor_Area (all zones)
  Occupancy_density           — People per Zone Floor Area, people/m² (all zones)
  Ventilation_rate_m3_s_m2    — DesignSpecification:OutdoorAir flow per floor area, m³/s·m²
  Chiller_COP                 — Chiller:Electric:EIR / ReformulatedEIR Reference COP (all chillers)
  Cooling_Setpoint_C          — ThermostatSetpoint cooling temperature, °C (all dual/single setpoints)

NOTE: Cooling_Setpoint_C is bounded to 22.0–26.0 °C to reflect plausible operational
variation. Values outside this range are rejected to prevent physically unrealistic runs.
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
    "Infiltration_ACH":          {"min": 0.2,    "max": 6.0},
    "Lighting_W_per_m2":         {"min": 5.0,    "max": 20.0},
    "Equipment_W_per_m2":        {"min": 3.0,    "max": 60.0},
    # People per Zone Floor Area (people/m²).
    # 0.02 ≈ very sparse (large open lab); 0.5 ≈ dense lecture theatre.
    "Occupancy_density":         {"min": 0.02,   "max": 0.5},
    # Outdoor air per floor area (m³/s·m²).
    # ASHRAE 62.1 typical office ~0.0003; lab/high-density ~0.003.
    "Ventilation_rate_m3_s_m2":  {"min": 0.0001, "max": 0.005},
    # Reference COP for water-cooled chillers.
    # Aging/degraded plant: ~2.5; high-efficiency modern: ~7.0.
    "Chiller_COP":               {"min": 2.5,    "max": 7.0},
    # Cooling setpoint (°C). 22 °C = cold end of typical Singapore office range;
    # 26 °C = warm end still consistent with SS 554 / BCA guidelines.
    "Cooling_Setpoint_C":        {"min": 22.0,   "max": 26.0},
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


def patch_occupancy(lines, value):
    """
    Replace People_per_Zone_Floor_Area in People objects.
    EnergyPlus People fields: 1=Name, 2=Zone, 3=Schedule, 4=Method,
    5=Number_of_People, 6=People_per_Zone_Floor_Area, 7=Zone_Floor_Area_per_Person, ...
    We target field 6 when Method (field 4) is 'People/Area'.
    State machine mirrors patch_lights / patch_equipment.
    """
    result = []
    changes = 0
    in_people = False
    field_count = 0
    method_is_per_area = False

    for line in lines:
        stripped = line.strip()

        if re.match(r"^People\s*,", stripped):
            in_people = True
            field_count = 0
            method_is_per_area = False
            result.append(line)
            continue

        if in_people:
            if stripped and not stripped.startswith("!"):
                field_count += 1
                if field_count == 4:
                    # Capture the calculation method from the comment or value
                    method_is_per_area = "People/Area" in line or "people/area" in line.lower()
                if field_count == 6 and method_is_per_area:
                    is_last = stripped.endswith(";")
                    new_line = re.sub(
                        r"[\d.]+(\s*[,;])",
                        lambda m: f"{value}{m.group(1)}",
                        line,
                        count=1,
                    )
                    result.append(new_line)
                    if new_line != line:
                        changes += 1
                    if is_last:
                        in_people = False
                    continue
                if stripped.endswith(";"):
                    in_people = False
        result.append(line)

    return result, changes


def patch_ventilation(lines, value):
    """
    Replace Outdoor_Air_Flow_per_Zone_Floor_Area in DesignSpecification:OutdoorAir objects.
    Fields: 1=Name, 2=Outdoor_Air_Method, 3=Flow_per_Person, 4=Flow_per_Zone_Floor_Area.
    Patches field 4 regardless of method (conservative — always update the per-area field
    so it takes effect if the method is switched to Flow/Area or Sum).
    """
    result = []
    changes = 0
    in_dsoa = False
    field_count = 0

    for line in lines:
        stripped = line.strip()

        if re.match(r"^DesignSpecification:OutdoorAir\s*,", stripped):
            in_dsoa = True
            field_count = 0
            result.append(line)
            continue

        if in_dsoa:
            if stripped and not stripped.startswith("!"):
                field_count += 1
                if field_count == 4:
                    is_last = stripped.endswith(";")
                    # Value may be blank (e.g. "  ,") — replace or insert
                    if re.search(r"[\d.]+", line):
                        new_line = re.sub(
                            r"[\d.]+(\s*[,;])",
                            lambda m: f"{value}{m.group(1)}",
                            line,
                            count=1,
                        )
                    else:
                        # Blank field: insert value before the comma/semicolon
                        new_line = re.sub(r"(\s*)(,|;)", rf"\g<1>{value}\2", line, count=1)
                    result.append(new_line)
                    if new_line != line:
                        changes += 1
                    if is_last:
                        in_dsoa = False
                    continue
                if stripped.endswith(";"):
                    in_dsoa = False
        result.append(line)

    return result, changes


def patch_chiller_cop(lines, value):
    """
    Replace Reference_COP in Chiller:Electric:EIR and
    Chiller:Electric:ReformulatedEIR objects.
    Fields for both types: 1=Name, 2=Reference_Capacity, 3=Reference_COP.
    """
    result = []
    changes = 0
    in_chiller = False
    field_count = 0

    chiller_pattern = re.compile(
        r"^Chiller:Electric:(EIR|ReformulatedEIR)\s*,", re.IGNORECASE
    )

    for line in lines:
        stripped = line.strip()

        if chiller_pattern.match(stripped):
            in_chiller = True
            field_count = 0
            result.append(line)
            continue

        if in_chiller:
            if stripped and not stripped.startswith("!"):
                field_count += 1
                if field_count == 3:  # Reference COP
                    is_last = stripped.endswith(";")
                    new_line = re.sub(
                        r"[\d.]+(\s*[,;])",
                        lambda m: f"{value}{m.group(1)}",
                        line,
                        count=1,
                    )
                    result.append(new_line)
                    if new_line != line:
                        changes += 1
                    if is_last:
                        in_chiller = False
                    continue
                if stripped.endswith(";"):
                    in_chiller = False
        result.append(line)

    return result, changes


def patch_cooling_setpoint(lines, value):
    """
    Replace the cooling setpoint temperature in ThermostatSetpoint:SingleCoolingSetPoint
    and ThermostatSetpoint:DualSetPoint objects.

    SingleCoolingSetPoint fields: 1=Name, 2=Setpoint_Temperature_Schedule_Name
      — schedule-based; we patch the compact schedule values instead (see below).

    DualSetPoint fields: 1=Name, 2=Heating_Setpoint_Temp_Schedule, 3=Cooling_Setpoint_Temp_Schedule
      — again schedule-based.

    In practice NUS IDFs define setpoints via Schedule:Compact objects whose names
    contain "Cool" or "Cooling". We patch the numeric tokens on those schedule lines.
    This is the standard regex approach when schedules are not resolved to objects.
    """
    result = []
    changes = 0
    in_cool_schedule = False

    cool_sched_pattern = re.compile(
        r"^Schedule:Compact\s*,", re.IGNORECASE
    )
    name_has_cool = re.compile(r"cool", re.IGNORECASE)

    for line in lines:
        stripped = line.strip()

        if cool_sched_pattern.match(stripped):
            in_cool_schedule = False  # reset; next line is the name
            result.append(line)
            continue

        # The line immediately after Schedule:Compact, is the schedule name
        # We detect it by checking if we are right after the object header.
        # Use a simpler heuristic: any data line inside a Schedule:Compact whose
        # name (prior line) contained "cool".
        # Implemented as: track whether the *current* schedule block has "cool" in name.
        if result and cool_sched_pattern.match(result[-1].strip()):
            in_cool_schedule = bool(name_has_cool.search(line))
            result.append(line)
            continue

        if in_cool_schedule:
            if stripped.startswith("!") or not stripped:
                result.append(line)
                continue
            # Lines like "  Until: 18:00, 24.0," — replace the trailing numeric token
            new_line = re.sub(
                r"(Until.*?,\s*)([\d.]+)(\s*[,;])",
                lambda m: f"{m.group(1)}{value}{m.group(3)}",
                line,
            )
            if new_line == line:
                # Fallback: bare numeric token at end of a Through/For/Until line
                new_line = re.sub(
                    r"(?<=,\s)([\d.]{4,})(\s*[,;])",
                    lambda m: f"{value}{m.group(2)}",
                    line,
                )
            result.append(new_line)
            if new_line != line:
                changes += 1
            if stripped.endswith(";"):
                in_cool_schedule = False
            continue

        result.append(line)

    return result, changes


PATCH_FUNCS = {
    "Infiltration_ACH":         patch_infiltration_ach,
    "Lighting_W_per_m2":        patch_lights,
    "Equipment_W_per_m2":       patch_equipment,
    "Occupancy_density":        patch_occupancy,
    "Ventilation_rate_m3_s_m2": patch_ventilation,
    "Chiller_COP":              patch_chiller_cop,
    "Cooling_Setpoint_C":       patch_cooling_setpoint,
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
                    if field_count == 6:
                        m = re.search(r"([\d.]+)", line)
                        if m:
                            current[param] = float(m.group(1))
                        break
                    if stripped.endswith(";"):
                        in_obj = False

        elif param == "Occupancy_density":
            in_obj = False
            field_count = 0
            method_ok = False
            for line in lines:
                stripped = line.strip()
                if re.match(r"^People\s*,", stripped):
                    in_obj = True
                    field_count = 0
                    method_ok = False
                    continue
                if in_obj and stripped and not stripped.startswith("!"):
                    field_count += 1
                    if field_count == 4:
                        method_ok = "People/Area" in line or "people/area" in line.lower()
                    if field_count == 6 and method_ok:
                        m = re.search(r"([\d.]+)", line)
                        if m:
                            current[param] = float(m.group(1))
                        break
                    if stripped.endswith(";"):
                        in_obj = False

        elif param == "Ventilation_rate_m3_s_m2":
            in_obj = False
            field_count = 0
            for line in lines:
                stripped = line.strip()
                if re.match(r"^DesignSpecification:OutdoorAir\s*,", stripped):
                    in_obj = True
                    field_count = 0
                    continue
                if in_obj and stripped and not stripped.startswith("!"):
                    field_count += 1
                    if field_count == 4:
                        m = re.search(r"([\d.]+)", line)
                        if m:
                            current[param] = float(m.group(1))
                        break
                    if stripped.endswith(";"):
                        in_obj = False

        elif param == "Chiller_COP":
            in_obj = False
            field_count = 0
            chiller_pat = re.compile(
                r"^Chiller:Electric:(EIR|ReformulatedEIR)\s*,", re.IGNORECASE
            )
            for line in lines:
                stripped = line.strip()
                if chiller_pat.match(stripped):
                    in_obj = True
                    field_count = 0
                    continue
                if in_obj and stripped and not stripped.startswith("!"):
                    field_count += 1
                    if field_count == 3:
                        m = re.search(r"([\d.]+)", line)
                        if m:
                            current[param] = float(m.group(1))
                        break
                    if stripped.endswith(";"):
                        in_obj = False

        elif param == "Cooling_Setpoint_C":
            in_cool = False
            cool_header = re.compile(r"^Schedule:Compact\s*,", re.IGNORECASE)
            for i, line in enumerate(lines):
                stripped = line.strip()
                if cool_header.match(stripped) and i + 1 < len(lines):
                    if re.search(r"cool", lines[i + 1], re.IGNORECASE):
                        in_cool = True
                        continue
                if in_cool:
                    m = re.search(r"Until.*?,\s*([\d.]+)", line)
                    if m:
                        current[param] = float(m.group(1))
                        break
                    if stripped.endswith(";"):
                        in_cool = False

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

    # Validate parameter names
    for param in params:
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

        if "Occupancy_density" in params:
            value = params["Occupancy_density"]
            objs = idf_obj.idfobjects.get("PEOPLE", [])
            old_vals, n = [], 0
            for obj in objs:
                if str(obj.Number_of_People_Calculation_Method).strip() == "People/Area":
                    old_vals.append(float(obj.People_per_Zone_Floor_Area or 0))
                    obj.People_per_Zone_Floor_Area = value
                    n += 1
            old = old_vals[0] if old_vals else "?"
            changes_applied.append(("Occupancy_density", old, value, n))
            print(f"  Patched Occupancy_density: {old} → {value} ({n} zones) [eppy]")

        if "Ventilation_rate_m3_s_m2" in params:
            value = params["Ventilation_rate_m3_s_m2"]
            objs = idf_obj.idfobjects.get("DESIGNSPECIFICATION:OUTDOORAIR", [])
            old_vals, n = [], 0
            for obj in objs:
                old_vals.append(float(obj.Outdoor_Air_Flow_per_Zone_Floor_Area or 0))
                obj.Outdoor_Air_Flow_per_Zone_Floor_Area = value
                n += 1
            old = old_vals[0] if old_vals else "?"
            changes_applied.append(("Ventilation_rate_m3_s_m2", old, value, n))
            print(f"  Patched Ventilation_rate_m3_s_m2: {old} → {value} ({n} specs) [eppy]")

        if "Chiller_COP" in params:
            value = params["Chiller_COP"]
            old_vals, n = [], 0
            for chiller_type in ("CHILLER:ELECTRIC:EIR", "CHILLER:ELECTRIC:REFORMULATEDEIR"):
                for obj in idf_obj.idfobjects.get(chiller_type, []):
                    old_vals.append(float(obj.Reference_COP or 0))
                    obj.Reference_COP = value
                    n += 1
            old = old_vals[0] if old_vals else "?"
            changes_applied.append(("Chiller_COP", old, value, n))
            print(f"  Patched Chiller_COP: {old} → {value} ({n} chillers) [eppy]")

        if "Cooling_Setpoint_C" in params:
            value = params["Cooling_Setpoint_C"]
            old_vals, n = [], 0
            for sched_type in (
                "SCHEDULE:COMPACT",
                "SCHEDULE:YEAR",
                "SCHEDULE:DAY:INTERVAL",
            ):
                for obj in idf_obj.idfobjects.get(sched_type, []):
                    name = str(getattr(obj, "Name", "") or "")
                    if re.search(r"cool", name, re.IGNORECASE):
                        # Walk all extension fields that hold numeric setpoint values
                        for field in obj.fieldnames:
                            raw = getattr(obj, field, None)
                            try:
                                fval = float(raw)
                                # Only touch values that look like temperature (18–30 °C)
                                if 18.0 <= fval <= 30.0:
                                    old_vals.append(fval)
                                    setattr(obj, field, value)
                                    n += 1
                            except (TypeError, ValueError):
                                pass
            old = old_vals[0] if old_vals else "?"
            changes_applied.append(("Cooling_Setpoint_C", old, value, n))
            print(f"  Patched Cooling_Setpoint_C: {old} → {value} ({n} schedule values) [eppy]")

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
