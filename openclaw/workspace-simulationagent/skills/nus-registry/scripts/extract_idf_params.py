#!/usr/bin/env python3
"""
extract_idf_params.py — Robust IDF parameter extractor using eppy
===================================================================
Extracts key simulation parameters from EnergyPlus IDF files using
the official IDD schema (eppy) rather than fragile regex parsing.
Designed to scale to 300+ buildings.

WHAT IS EXTRACTED
-----------------
Per-zone (aggregated to building level):
  zone_count             : number of Zone objects
  floor_area_m2          : sum of all Zone.Floor_Area × Zone.Multiplier (m²)
  total_volume_m3        : sum of all Zone.Volume × Zone.Multiplier (m³)

Load densities (area-weighted mean across all zones):
  lighting_w_m2          : Lights  Watts_per_Zone_Floor_Area
  equipment_w_m2         : ElectricEquipment  Watts_per_Zone_Floor_Area
  people_per_m2          : People  People_per_Floor_Area

Infiltration:
  infiltration_ach       : mean Air_Changes_per_Hour (zones using AirChanges/Hour method)
  infiltration_m3s_m2    : mean Flow_Rate_per_Floor_Area (zones using Flow/Area method)

Setpoints (resolved through Schedule:Constant, Schedule:Compact, Schedule:Year):
  cooling_setpoint_c     : building-level cooling setpoint (°C)
  heating_setpoint_c     : building-level heating setpoint (°C)

USAGE
-----
  # Single building
  python3 extract_idf_params.py path/to/FOE13.idf

  # All buildings in a directory → JSON summary
  python3 extract_idf_params.py --dir /Users/ye/nus-energy/idfs --out params.json

  # Pretty-print one building
  python3 extract_idf_params.py path/to/FOE13.idf --pretty

OPTIONS
-------
  --idd      Path to Energy+.idd  (default: /Applications/EnergyPlus-23-1-0/Energy+.idd)
  --dir      Directory of .idf files to batch-process
  --out      Output JSON path for batch mode (default: stdout)
  --pretty   Pretty-print JSON output
  --csv      Also write a CSV summary (batch mode only)
  --quiet    Suppress per-building progress output
"""

import argparse
import csv
import json
import os
import sys
from pathlib import Path
from statistics import mean

# ── Defaults ──────────────────────────────────────────────────────────────────
DEFAULT_IDD = Path(os.environ.get(
    "ENERGYPLUS_IDD",
    "/Applications/EnergyPlus-23-1-0/Energy+.idd"
))

# ── Schedule resolver ─────────────────────────────────────────────────────────

def _build_schedule_index(idf) -> dict:
    """
    Build a name→value lookup for simple constant-value schedules.
    Covers Schedule:Constant and single-value Schedule:Compact entries.
    Returns {schedule_name_lower: float_value}.
    """
    index = {}

    # Schedule:Constant — direct value
    for s in idf.idfobjects.get("SCHEDULE:CONSTANT", []):
        try:
            index[s.Name.lower()] = float(s.Hourly_Value)
        except (ValueError, AttributeError):
            pass

    # Schedule:Compact — grab the first numeric "Until" value
    # Field pattern: ..., Through: 12/31, For: AllDays, Until: 24:00, <value>;
    for s in idf.idfobjects.get("SCHEDULE:COMPACT", []):
        name = s.Name.lower()
        if name in index:
            continue
        # Walk all fields after the type-limits field looking for a float
        found = False
        for fname in s.fieldnames[2:]:  # skip key, Name
            raw = getattr(s, fname, "")
            if raw in (None, ""):
                continue
            try:
                val = float(raw)
                index[name] = val
                found = True
                break
            except (ValueError, TypeError):
                pass

    return index


def _resolve_setpoint(schedule_name: str, sched_index: dict):
    """Return the numeric setpoint for a schedule name, or None."""
    if not schedule_name:
        return None
    return sched_index.get(schedule_name.lower())


# ── Zone geometry helpers ─────────────────────────────────────────────────────

def _zone_floor_area(zone) -> float:
    """Return zone floor area in m², accounting for multiplier."""
    try:
        area = float(zone.Floor_Area) if zone.Floor_Area not in (None, "") else 0.0
    except (ValueError, TypeError):
        area = 0.0
    try:
        mult = float(zone.Multiplier) if zone.Multiplier not in (None, "") else 1.0
    except (ValueError, TypeError):
        mult = 1.0
    return area * mult


def _zone_volume(zone) -> float:
    """Return zone volume in m³, accounting for multiplier."""
    try:
        vol = float(zone.Volume) if zone.Volume not in (None, "") else 0.0
    except (ValueError, TypeError):
        vol = 0.0
    try:
        mult = float(zone.Multiplier) if zone.Multiplier not in (None, "") else 1.0
    except (ValueError, TypeError):
        mult = 1.0
    return vol * mult


# ── Per-object extractors ─────────────────────────────────────────────────────

def _extract_zones(idf) -> dict:
    zones = idf.idfobjects.get("ZONE", [])
    total_area = sum(_zone_floor_area(z) for z in zones)
    total_vol  = sum(_zone_volume(z)      for z in zones)
    return {
        "zone_count":    len(zones),
        "floor_area_m2": round(total_area, 2),
        "total_volume_m3": round(total_vol, 2),
    }


def _extract_lights(idf) -> dict:
    """Area-weighted mean lighting density (W/m²)."""
    lights = idf.idfobjects.get("LIGHTS", [])
    weighted, total_area = [], 0.0
    for obj in lights:
        method = getattr(obj, "Design_Level_Calculation_Method", "")
        if "Watts/Area" in method or "watts/area" in method.lower():
            try:
                w = float(obj.Watts_per_Zone_Floor_Area)
            except (ValueError, TypeError):
                continue
            # Try to get zone area for weighting; fall back to 1.0
            try:
                area = float(obj.Zone_or_ZoneList_or_Space_or_SpaceList_Name)
            except (ValueError, TypeError):
                area = 1.0
            weighted.append(w)
            total_area += area

    if not weighted:
        # Fallback: grab any non-zero value
        for obj in lights:
            try:
                w = float(obj.Watts_per_Zone_Floor_Area)
                if w > 0:
                    return {"lighting_w_m2": round(w, 4)}
            except (ValueError, TypeError):
                pass
        return {"lighting_w_m2": None}

    return {"lighting_w_m2": round(mean(weighted), 4)}


def _extract_equipment(idf) -> dict:
    """Area-weighted mean equipment density (W/m²)."""
    equip = idf.idfobjects.get("ELECTRICEQUIPMENT", [])
    values = []
    for obj in equip:
        method = getattr(obj, "Design_Level_Calculation_Method", "")
        if "Watts/Area" in method or "watts/area" in method.lower():
            try:
                w = float(obj.Watts_per_Zone_Floor_Area)
                values.append(w)
            except (ValueError, TypeError):
                pass

    if not values:
        for obj in equip:
            try:
                w = float(obj.Watts_per_Zone_Floor_Area)
                if w > 0:
                    return {"equipment_w_m2": round(w, 4)}
            except (ValueError, TypeError):
                pass
        return {"equipment_w_m2": None}

    return {"equipment_w_m2": round(mean(values), 4)}


def _extract_people(idf) -> dict:
    """Area-weighted mean people density (people/m²)."""
    people = idf.idfobjects.get("PEOPLE", [])
    values = []
    for obj in people:
        method = getattr(obj, "Number_of_People_Calculation_Method", "")
        if "People/Area" in method or "people/area" in method.lower():
            try:
                d = float(obj.People_per_Floor_Area)
                if d > 0:
                    values.append(d)
            except (ValueError, TypeError):
                pass

    return {"people_per_m2": round(mean(values), 6) if values else None}


def _extract_infiltration(idf) -> dict:
    """
    Returns both ACH and flow/area means (whichever methods are used).
    Most NUS IDFs use AirChanges/Hour.
    """
    infil = idf.idfobjects.get("ZONEINFILTRATION:DESIGNFLOWRATE", [])
    ach_vals, flow_vals = [], []
    for obj in infil:
        method = getattr(obj, "Design_Flow_Rate_Calculation_Method", "")
        if "AirChanges/Hour" in method:
            try:
                v = float(obj.Air_Changes_per_Hour)
                if v > 0:
                    ach_vals.append(v)
            except (ValueError, TypeError):
                pass
        elif "Flow/Area" in method:
            try:
                v = float(obj.Flow_Rate_per_Floor_Area)
                if v > 0:
                    flow_vals.append(v)
            except (ValueError, TypeError):
                pass

    return {
        "infiltration_ach":      round(mean(ach_vals),  6) if ach_vals  else None,
        "infiltration_m3s_m2":   round(mean(flow_vals), 6) if flow_vals else None,
    }


def _extract_setpoints(idf, sched_index: dict) -> dict:
    """
    Resolve cooling + heating setpoints via ThermostatSetpoint:DualSetpoint.
    Returns None if schedule not resolvable (e.g. Schedule:Compact with ramps).
    """
    thermo = idf.idfobjects.get("THERMOSTATSETPOINT:DUALSETPOINT", [])
    cooling_vals, heating_vals = [], []

    for t in thermo:
        c = _resolve_setpoint(
            getattr(t, "Cooling_Setpoint_Temperature_Schedule_Name", ""),
            sched_index
        )
        h = _resolve_setpoint(
            getattr(t, "Heating_Setpoint_Temperature_Schedule_Name", ""),
            sched_index
        )
        if c is not None:
            cooling_vals.append(c)
        if h is not None:
            heating_vals.append(h)

    # Also try single-setpoint objects
    for obj in idf.idfobjects.get("THERMOSTATSETPOINT:SINGLECOOLING", []):
        c = _resolve_setpoint(
            getattr(obj, "Setpoint_Temperature_Schedule_Name", ""),
            sched_index
        )
        if c is not None:
            cooling_vals.append(c)

    for obj in idf.idfobjects.get("THERMOSTATSETPOINT:SINGLEHEATING", []):
        h = _resolve_setpoint(
            getattr(obj, "Setpoint_Temperature_Schedule_Name", ""),
            sched_index
        )
        if h is not None:
            heating_vals.append(h)

    return {
        "cooling_setpoint_c": round(mean(cooling_vals), 2) if cooling_vals else None,
        "heating_setpoint_c": round(mean(heating_vals), 2) if heating_vals else None,
    }


# ── Main extractor ────────────────────────────────────────────────────────────

def extract_params(idf_path: Path, idd_path: Path = DEFAULT_IDD) -> dict:
    """
    Extract all parameters from a single IDF file.
    Returns a flat dict with all extracted fields.
    """
    from eppy.modeleditor import IDF

    # eppy stores IDD path globally; safe to call multiple times with same path
    IDF.setiddname(str(idd_path))

    idf = IDF(str(idf_path))

    sched_index = _build_schedule_index(idf)

    params = {"building": idf_path.stem}
    params.update(_extract_zones(idf))
    params.update(_extract_lights(idf))
    params.update(_extract_equipment(idf))
    params.update(_extract_people(idf))
    params.update(_extract_infiltration(idf))
    params.update(_extract_setpoints(idf, sched_index))

    return params


# ── Batch processing ──────────────────────────────────────────────────────────

def batch_extract(idf_dir: Path, idd_path: Path, quiet: bool = False) -> list[dict]:
    idf_files = sorted(idf_dir.glob("*.idf"))
    if not idf_files:
        print(f"ERROR: No .idf files found in {idf_dir}", file=sys.stderr)
        sys.exit(1)

    results = []
    for idf_path in idf_files:
        if not quiet:
            print(f"  {idf_path.name:<20}", end=" ", flush=True)
        try:
            params = extract_params(idf_path, idd_path)
            results.append(params)
            if not quiet:
                print(
                    f"zones={params['zone_count']:>4}  "
                    f"area={params['floor_area_m2']:>9.1f} m²  "
                    f"cool={params['cooling_setpoint_c']}°C  "
                    f"ACH={params['infiltration_ach']}  "
                    f"light={params['lighting_w_m2']} W/m²  "
                    f"equip={params['equipment_w_m2']} W/m²"
                )
        except Exception as exc:
            if not quiet:
                print(f"ERROR: {exc}")
            results.append({"building": idf_path.stem, "error": str(exc)})

    return results


def write_csv(results: list[dict], out_path: Path):
    if not results:
        return
    # Collect all keys preserving order; put building first
    keys = list(dict.fromkeys(
        ["building"] + [k for r in results for k in r if k != "building"]
    ))
    with open(out_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(results)
    print(f"CSV written: {out_path}")


# ── CLI ───────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Extract EnergyPlus IDF parameters using eppy.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "idf", nargs="?", help="Single IDF file path"
    )
    parser.add_argument(
        "--dir", help="Batch mode: directory of .idf files"
    )
    parser.add_argument(
        "--out", help="Output JSON path (default: stdout)"
    )
    parser.add_argument(
        "--idd", default=str(DEFAULT_IDD), help="Path to Energy+.idd"
    )
    parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON"
    )
    parser.add_argument(
        "--csv", action="store_true", help="Also write CSV (batch mode)"
    )
    parser.add_argument(
        "--quiet", action="store_true", help="Suppress progress output"
    )
    args = parser.parse_args()

    idd_path = Path(args.idd)
    if not idd_path.exists():
        print(f"ERROR: IDD not found: {idd_path}", file=sys.stderr)
        print("Set --idd or export ENERGYPLUS_IDD=/path/to/Energy+.idd", file=sys.stderr)
        sys.exit(1)

    indent = 2 if args.pretty else None

    # ── Single file ──────────────────────────────────────────────────────────
    if args.idf:
        idf_path = Path(args.idf)
        if not idf_path.exists():
            print(f"ERROR: IDF not found: {idf_path}", file=sys.stderr)
            sys.exit(1)
        params = extract_params(idf_path, idd_path)
        output = json.dumps(params, indent=indent)
        if args.out:
            Path(args.out).write_text(output)
        else:
            print(output)
        return

    # ── Batch mode ───────────────────────────────────────────────────────────
    if args.dir:
        idf_dir = Path(args.dir)
        if not idf_dir.exists():
            print(f"ERROR: Directory not found: {idf_dir}", file=sys.stderr)
            sys.exit(1)
        results = batch_extract(idf_dir, idd_path, quiet=args.quiet)

        if not args.quiet:
            ok    = sum(1 for r in results if "error" not in r)
            error = len(results) - ok
            print(f"\n{'─'*60}")
            print(f"Done: {ok} OK, {error} errors  (total: {len(results)})")

        output = json.dumps(results, indent=indent)
        if args.out:
            out_path = Path(args.out)
            out_path.write_text(output)
            print(f"JSON written: {out_path}")
            if args.csv:
                write_csv(results, out_path.with_suffix(".csv"))
        else:
            print(output)
        return

    parser.print_help()
    sys.exit(1)


if __name__ == "__main__":
    main()
