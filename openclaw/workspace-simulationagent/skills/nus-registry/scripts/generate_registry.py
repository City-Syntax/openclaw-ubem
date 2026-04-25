#!/usr/bin/env python3
"""
generate_registry.py — NUS Building Registry Generator
Scans all IDF files in NUS_PROJECT_DIR/idfs/ and extracts key parameters,
merging with existing registry metadata to produce building_registry.json.

What it extracts from each IDF:
  - zone_count          : number of Zone objects
  - floor_area_m2_idf   : sum of all zone floor areas (m²)
  - cooling_setpoint_c  : first CoolingSetpoint schedule value found
  - infiltration_ach    : first InfiltrationAch value found
  - lighting_w_m2       : first Lights Watts_per_Zone_Floor_Area value found
  - equipment_w_m2      : first ElectricEquipment Watts_per_Zone_Floor_Area value found
  - people_density_per_m2 : first PeopleDensity value found

Metadata not in IDFs (full_name, faculty, type, etc.) is preserved from
the existing registry or from BUILDING_META below.

Usage:
  # Full batch — scan all IDFs (top-level + subdirectories) and update registry
  cd $NUS_PROJECT_DIR
  python3 <SKILL_DIR>/scripts/generate_registry.py [--idfs idfs/] [--out building_registry.json] [--dry-run]

  # Pre-flight — check/add a single building by name (fast, no full rescan)
  python3 <SKILL_DIR>/scripts/generate_registry.py --building FOE5 [--idfs idfs/] [--out building_registry.json]

  # Pre-flight — check/add a single building by IDF path
  python3 <SKILL_DIR>/scripts/generate_registry.py --idf idfs/A1_L_L/FOE5.idf [--out building_registry.json]

Notes:
  - Scans subdirectories recursively (rglob). When multiple variants exist for the same
    building stem (e.g. idfs/FOE5.idf and idfs/A1_L_L/FOE5.idf), the top-level IDF
    takes precedence; subdirectory variants are used only if no top-level IDF exists.
  - Pre-flight mode (--building / --idf) is idempotent: skips the write if the entry
    already exists and floor_area_m2 is non-null.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Optional

# ── Config ────────────────────────────────────────────────────────────────────
PROJECT_DIR = Path(os.environ.get("NUS_PROJECT_DIR", Path(__file__).parents[4]))
IDF_DIR_DEFAULT = PROJECT_DIR / "idfs"
REGISTRY_DEFAULT = PROJECT_DIR / "building_registry.json"

GRID_FACTOR = 0.4168  # kgCO2e/kWh (EMA Singapore 2023)

# ── Static metadata not extractable from IDF ──────────────────────────────────
# Update this when new buildings are added or metadata changes.
BUILDING_META = {
    "BIZ12": {
        "full_name": "NUS Business School Block 12",
        "faculty": "NUS Business School",
        "type": "Lecture / Seminar",
        "floors": 5,
        "hvac_system": "Central chilled water AHU + FCU",
        "occupancy_peak_people": 600,
        "occupancy_hours": "07:00-22:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Business School",
        "green_mark_target": "GoldPlus",
        "notes": "Mix of lecture theatres and seminar rooms. High occupancy during peak teaching hours.",
    },
    "BIZ14": {
        "full_name": "NUS Business School Block 14",
        "faculty": "NUS Business School",
        "type": "Office / Research",
        "floors": 6,
        "hvac_system": "Central chilled water AHU + FCU",
        "occupancy_peak_people": 500,
        "occupancy_hours": "08:00-20:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Business School",
        "green_mark_target": "GoldPlus",
        "notes": "Faculty offices and research spaces. More consistent occupancy than pure teaching buildings.",
    },
    "BIZ8": {
        "full_name": "NUS Business School Block 8",
        "faculty": "NUS Business School",
        "type": "Lecture / Office",
        "floors": 4,
        "hvac_system": "Central chilled water AHU + FCU",
        "occupancy_peak_people": 450,
        "occupancy_hours": "07:00-21:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Business School",
        "green_mark_target": "GoldPlus",
        "notes": "Primarily lecture and tutorial rooms. Lower floors have higher foot traffic.",
    },
    "CLB6": {
        "full_name": "Central Library Block 6",
        "faculty": "NUS Libraries",
        "type": "Library / Study",
        "floors": 6,
        "hvac_system": "Central chilled water AHU + FCU",
        "occupancy_peak_people": 800,
        "occupancy_hours": "07:00-22:00 (term), 09:00-21:00 (vacation — library stays open)",
        "nus_zone": "Central Campus",
        "green_mark_target": "Platinum",
        "notes": "Heavy glazing on south facade — high solar gain. Occupancy spikes during exam periods. Generated with Climate Studio 2.0.",
    },
    "FASS2": {
        "full_name": "Faculty of Arts and Social Sciences Block 2",
        "faculty": "Faculty of Arts and Social Sciences",
        "type": "Lecture / Tutorial",
        "floors": 4,
        "hvac_system": "Split system + centralised AHU",
        "occupancy_peak_people": 400,
        "occupancy_hours": "08:00-21:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Arts Campus",
        "green_mark_target": "Gold",
        "notes": "Lecture theatres and tutorial rooms. Lower equipment loads than science/engineering buildings.",
    },
    "FASS6": {
        "full_name": "Faculty of Arts and Social Sciences Block 6",
        "faculty": "Faculty of Arts and Social Sciences",
        "type": "Office / Research",
        "floors": 5,
        "hvac_system": "Split system + centralised AHU",
        "occupancy_peak_people": 350,
        "occupancy_hours": "08:00-19:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Arts Campus",
        "green_mark_target": "Gold",
        "notes": "Faculty offices and research rooms. Standard office occupancy profile.",
    },
    "FASS7": {
        "full_name": "Faculty of Arts and Social Sciences Block 7",
        "faculty": "Faculty of Arts and Social Sciences",
        "type": "Office / Seminar",
        "floors": 4,
        "hvac_system": "Split system + centralised AHU",
        "occupancy_peak_people": 300,
        "occupancy_hours": "08:00-19:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Arts Campus",
        "green_mark_target": "Gold",
        "notes": "Mix of offices and seminar rooms. Relatively low energy intensity.",
    },
    "FOE13": {
        "full_name": "Faculty of Engineering Block 13",
        "faculty": "College of Design and Engineering",
        "type": "Research / Laboratory",
        "floors": 7,
        "hvac_system": "VAV + lab exhaust systems",
        "occupancy_peak_people": 550,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Engineering Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "High equipment loads. Some labs operate 24/7. Exhaust systems add significant fan energy.",
    },
    "FOE18": {
        "full_name": "Faculty of Engineering Block 18",
        "faculty": "College of Design and Engineering",
        "type": "Research / Laboratory",
        "floors": 8,
        "hvac_system": "VAV + lab exhaust systems",
        "occupancy_peak_people": 600,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Engineering Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Large research building. High base load from 24/7 servers and lab equipment.",
    },
    "FOE6": {
        "full_name": "Faculty of Engineering Block 6",
        "faculty": "College of Design and Engineering",
        "type": "Lecture / Tutorial",
        "floors": 6,
        "hvac_system": "Central chilled water AHU",
        "occupancy_peak_people": 700,
        "occupancy_hours": "07:00-22:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Engineering Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Primarily teaching building. High occupancy during term lectures.",
    },
    "FOE9": {
        "full_name": "Faculty of Engineering Block 9",
        "faculty": "College of Design and Engineering",
        "type": "Research / Office",
        "floors": 7,
        "hvac_system": "VAV + split system",
        "occupancy_peak_people": 500,
        "occupancy_hours": "08:00-21:00 (term), 09:00-18:00 (vacation)",
        "nus_zone": "Engineering Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Mix of research offices and light labs. More consistent occupancy than pure teaching blocks.",
    },
    "FOS1": {
        "full_name": "Faculty of Science Block 1",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 8,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 600,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Wet labs with fume hoods — very high exhaust air loads. 100% fresh air supply year-round.",
    },
    "FOS11": {
        "full_name": "Faculty of Science Block 11",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 7,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 500,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Chemistry and biology labs. High latent cooling load from humid outdoor air intake.",
    },
    "FOS14": {
        "full_name": "Faculty of Science Block 14",
        "faculty": "Faculty of Science",
        "type": "Lecture / Office",
        "floors": 5,
        "hvac_system": "Central chilled water AHU + FCU",
        "occupancy_peak_people": 500,
        "occupancy_hours": "07:00-21:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Science Faculty",
        "green_mark_target": "Gold",
        "notes": "Teaching and office block. Lower energy intensity than wet lab buildings.",
    },
    "FOS28": {
        "full_name": "Faculty of Science Block 28",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 7,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 520,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Physics and materials science labs. High equipment loads from spectrometers and electron microscopes.",
    },
    "FOS34": {
        "full_name": "Faculty of Science Block 34",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 6,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 480,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Life sciences research. Biosafety cabinets and incubators contribute to base load.",
    },
    "FOS38": {
        "full_name": "Faculty of Science Block 38",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 7,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 500,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Chemistry labs. Highest energy intensity type due to fume hoods and solvent storage HVAC.",
    },
    "FOS40": {
        "full_name": "Faculty of Science Block 40",
        "faculty": "Faculty of Science",
        "type": "Research / Office",
        "floors": 6,
        "hvac_system": "Central chilled water AHU + FCU",
        "occupancy_peak_people": 420,
        "occupancy_hours": "08:00-21:00 (term), 09:00-18:00 (vacation)",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Research offices and dry labs. Lower exhaust load than wet chemistry labs.",
    },
    "FOS43": {
        "full_name": "Faculty of Science Block 43",
        "faculty": "Faculty of Science",
        "type": "Lecture / Tutorial",
        "floors": 5,
        "hvac_system": "Central chilled water AHU",
        "occupancy_peak_people": 550,
        "occupancy_hours": "07:00-21:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Science Faculty",
        "green_mark_target": "Gold",
        "notes": "Teaching building. High transient occupancy — full during lectures, near-empty between.",
    },
    "FOS45": {
        "full_name": "Faculty of Science Block 45",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 7,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 510,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Multi-discipline science labs. Shared instrumentation rooms with high continuous loads.",
    },
    "FOS46": {
        "full_name": "Faculty of Science Block 46",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 6,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 480,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Biochemistry and molecular biology labs. Ultra-low temperature freezers add significant base load.",
    },
    "FOS8": {
        "full_name": "Faculty of Science Block 8",
        "faculty": "Faculty of Science",
        "type": "Lecture / Office",
        "floors": 5,
        "hvac_system": "Central chilled water AHU + FCU",
        "occupancy_peak_people": 520,
        "occupancy_hours": "07:00-21:00 (term), 09:00-17:00 (vacation)",
        "nus_zone": "Science Faculty",
        "green_mark_target": "Gold",
        "notes": "Teaching and faculty office block. Standard academic building profile.",
    },
    "FOS9": {
        "full_name": "Faculty of Science Block 9",
        "faculty": "Faculty of Science",
        "type": "Research / Laboratory",
        "floors": 7,
        "hvac_system": "VAV + fume hood exhaust",
        "occupancy_peak_people": 530,
        "occupancy_hours": "08:00-22:00 (term), labs 24/7",
        "nus_zone": "Science Faculty",
        "green_mark_target": "GoldPlus",
        "notes": "Science research labs. Adjacent to FOS8. Shared chiller plant expected — verify with NUS OED.",
    },
}

MATCHED_BUILDINGS = {"FOE6", "FOE9", "FOE13", "FOE18", "FOS43", "FOS46"}


# ── IDF Parsing ───────────────────────────────────────────────────────────────

def extract_idf_params(idf_path: Path) -> dict:
    """
    Parse a single IDF file and extract:
      zone_count, floor_area_m2_idf, cooling_setpoint_c,
      infiltration_ach, lighting_w_m2, equipment_w_m2, people_density_per_m2
    """
    with open(idf_path, encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    params = {
        "zone_count": 0,
        "floor_area_m2_idf": 0.0,
        "cooling_setpoint_c": None,
        "infiltration_ach": None,
        "lighting_w_m2": None,
        "equipment_w_m2": None,
        "people_density_per_m2": None,
    }

    # ── Zone count + floor area (sum of Zone Area fields) ─────────────────
    in_zone = False
    zone_field = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r"^Zone\s*,", stripped):
            in_zone = True
            zone_field = 0
            params["zone_count"] += 1
            continue
        if in_zone:
            if stripped and not stripped.startswith("!"):
                zone_field += 1
                # Zone fields: 1=Name, 2=Rotation, 3=X, 4=Y, 5=Z,
                #              6=Type, 7=Multiplier, 8=CeilingHeight,
                #              9=Volume, 10=Area
                if zone_field == 10:
                    val = re.match(r"\s*([\d.]+)\s*[,;]", line)
                    if val:
                        params["floor_area_m2_idf"] += float(val.group(1))
                if stripped.endswith(";"):
                    in_zone = False

    params["floor_area_m2_idf"] = round(params["floor_area_m2_idf"], 2)

    # ── Cooling setpoint (first CoolingSetpoint schedule value) ───────────
    for line in lines:
        if "!- CoolingSetpoint" in line:
            m = re.search(r"([\d.]+)\s*;", line)
            if m:
                params["cooling_setpoint_c"] = float(m.group(1))
                break

    # ── Infiltration ACH (first InfiltrationAch value) ────────────────────
    for line in lines:
        if "InfiltrationAch" in line and "!-" in line:
            m = re.match(r"\s*([\d.]+)\s*,", line)
            if m:
                params["infiltration_ach"] = float(m.group(1))
                break

    # ── Lighting W/m² (first Lights object, field 4) ──────────────────────
    in_lights = False
    field_count = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r"^Lights\s*,", stripped):
            in_lights = True
            field_count = 0
            continue
        if in_lights and stripped and not stripped.startswith("!"):
            field_count += 1
            if field_count == 4:
                m = re.search(r"([\d.]+)", line)
                if m:
                    params["lighting_w_m2"] = float(m.group(1))
                break
            if stripped.endswith(";"):
                in_lights = False

    # ── Equipment W/m² (first ElectricEquipment object, field 4) ─────────
    in_equip = False
    field_count = 0
    for line in lines:
        stripped = line.strip()
        if re.match(r"^ElectricEquipment\s*,", stripped):
            in_equip = True
            field_count = 0
            continue
        if in_equip and stripped and not stripped.startswith("!"):
            field_count += 1
            if field_count == 4:
                m = re.search(r"([\d.]+)", line)
                if m:
                    params["equipment_w_m2"] = float(m.group(1))
                break
            if stripped.endswith(";"):
                in_equip = False

    # ── People density (first People object, PeopleDensity field) ────────
    for line in lines:
        if "PeopleDensity" in line or "!- PeopleDensity" in line:
            m = re.search(r"([\d.]+)", line)
            if m:
                params["people_density_per_m2"] = float(m.group(1))
                break

    return params


# ── Registry builder ──────────────────────────────────────────────────────────

def _resolve_idf_for_building(building: str, idf_dir: Path) -> Optional[Path]:
    """
    Find the best IDF file for a given building stem.
    Preference order: top-level idfs/<BUILDING>.idf > first subdirectory match.
    """
    top_level = idf_dir / f"{building}.idf"
    if top_level.exists():
        return top_level
    # Search subdirectories
    matches = sorted(idf_dir.rglob(f"{building}.idf"))
    return matches[0] if matches else None


def _make_entry(building: str, idf_path: Path, existing: dict) -> dict:
    """Build a registry entry for a single building from its IDF."""
    idf_params = extract_idf_params(idf_path)
    meta = BUILDING_META.get(building, {})
    existing_entry = existing.get(building, {})
    return {
        "building": building,
        "full_name": meta.get("full_name", existing_entry.get("full_name", building)),
        "faculty": meta.get("faculty", existing_entry.get("faculty", "Unknown")),
        "type": meta.get("type", existing_entry.get("type", "Unknown")),
        "floor_area_m2": idf_params["floor_area_m2_idf"],  # IDF is authoritative
        "floors": meta.get("floors", existing_entry.get("floors", None)),
        "hvac_system": meta.get("hvac_system", existing_entry.get("hvac_system", "Unknown")),
        "cooling_setpoint_c": idf_params["cooling_setpoint_c"],
        "occupancy_peak_people": meta.get("occupancy_peak_people", existing_entry.get("occupancy_peak_people", None)),
        "occupancy_hours": meta.get("occupancy_hours", existing_entry.get("occupancy_hours", "Unknown")),
        "nus_zone": meta.get("nus_zone", existing_entry.get("nus_zone", "Unknown")),
        "green_mark_target": meta.get("green_mark_target", existing_entry.get("green_mark_target", "Unknown")),
        "has_ground_truth": building in MATCHED_BUILDINGS,
        "notes": meta.get("notes", existing_entry.get("notes", "")),
        # IDF-extracted fields
        "floor_area_m2_idf": idf_params["floor_area_m2_idf"],
        "cooling_setpoint_c_idf": idf_params["cooling_setpoint_c"],
        "lighting_w_m2": idf_params["lighting_w_m2"],
        "equipment_w_m2": idf_params["equipment_w_m2"],
        "infiltration_ach": idf_params["infiltration_ach"],
        "people_density_per_m2": idf_params["people_density_per_m2"],
        "zone_count": idf_params["zone_count"],
    }


def _notes_block() -> dict:
    return {
        "floor_area_m2": "Extracted from IDF Zone objects (sum of all zone floor areas)",
        "floor_area_m2_idf": "Same as floor_area_m2 — direct IDF extract",
        "occupancy_peak_people": "Estimate based on building type — verify with NUS OED",
        "source": "BIZ=Business, CLB=Central Library, FASS=Arts & Social Sciences, FOE=Engineering, FOS=Science",
        "grid_factor": f"{GRID_FACTOR} kgCO2e/kWh (EMA Singapore 2023)",
        "has_ground_truth": f"Buildings with OED meter data: {sorted(MATCHED_BUILDINGS)}",
        "generated_by": "generate_registry.py (nus-registry skill, workspace-simulationagent)",
    }


def preflight_registry(building: str, idf_dir: Path, out_path: Path, idf_path: Path = None) -> bool:
    """
    Pre-flight check: ensure `building` exists in the registry with a non-null floor_area_m2.
    - If entry already complete: no-op, return True.
    - If missing or floor_area_m2 is null: find IDF, extract, write entry, return True.
    - If IDF not found: warn and return False.

    Returns True if registry is good to go, False if floor area unavailable.
    """
    existing = {}
    if out_path.exists():
        with open(out_path) as f:
            existing = json.load(f)

    entry = existing.get(building, {})
    if entry.get("floor_area_m2"):
        print(f"[registry] {building}: already registered (floor_area_m2={entry['floor_area_m2']} m²) — skipping")
        return True

    # Need to find/use IDF
    if idf_path is None:
        idf_path = _resolve_idf_for_building(building, idf_dir)

    if idf_path is None:
        print(f"[registry] WARNING: {building}: no IDF found under {idf_dir} — EUI will be unavailable", file=sys.stderr)
        return False

    print(f"[registry] {building}: not in registry — extracting from {idf_path}")
    new_entry = _make_entry(building, idf_path, existing)
    area = new_entry["floor_area_m2"]

    if not area:
        print(f"[registry] WARNING: {building}: IDF has no Zone area data — EUI will be unavailable", file=sys.stderr)
        return False

    existing.setdefault("_notes", _notes_block())
    existing[building] = new_entry

    with open(out_path, "w") as f:
        json.dump(existing, f, indent=2)

    print(f"[registry] {building}: added — floor_area_m2={area} m², zones={new_entry['zone_count']}")
    return True


def build_registry(idf_dir: Path, existing: dict) -> dict:
    """
    Full batch scan: recursively find all unique building stems in idf_dir,
    preferring top-level IDFs over subdirectory variants.
    """
    # Collect all IDF files recursively; prefer top-level over subdirectory for same stem
    top_level: dict[str, Path] = {}
    subdir: dict[str, Path] = {}
    for idf_path in sorted(idf_dir.rglob("*.idf")):
        stem = idf_path.stem
        if idf_path.parent == idf_dir:
            top_level[stem] = idf_path
        else:
            if stem not in subdir:  # keep first subdir match per stem
                subdir[stem] = idf_path

    # Merge: top-level wins; subdir fills gaps
    all_buildings: dict[str, Path] = {**subdir, **top_level}

    if not all_buildings:
        print(f"ERROR: No IDF files found under {idf_dir}", file=sys.stderr)
        sys.exit(1)

    registry = {"_notes": _notes_block()}

    for building, idf_path in sorted(all_buildings.items()):
        print(f"  Scanning {building} ({idf_path.relative_to(idf_dir)})...", end=" ")
        entry = _make_entry(building, idf_path, existing)
        registry[building] = entry
        idf_params = entry  # fields are same names
        print(f"zones={entry['zone_count']}, area={entry['floor_area_m2']:.0f}m², "
              f"setpoint={entry['cooling_setpoint_c']}°C, ACH={entry['infiltration_ach']}")

    return registry


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Generate NUS building_registry.json from IDF files.")
    parser.add_argument("--idfs", default=str(IDF_DIR_DEFAULT), help="Path to IDF directory (recursive scan)")
    parser.add_argument("--out", default=str(REGISTRY_DEFAULT), help="Output registry JSON path")
    parser.add_argument("--dry-run", action="store_true", help="Print registry without writing file (batch mode only)")
    # Pre-flight: single-building modes
    parser.add_argument("--building", metavar="STEM", help="Pre-flight: ensure one building is registered (e.g. FOE5)")
    parser.add_argument("--idf", metavar="PATH", help="Pre-flight: path to a specific IDF (implies --building from stem)")
    args = parser.parse_args()

    idf_dir = Path(args.idfs)
    out_path = Path(args.out)

    if not idf_dir.exists():
        print(f"ERROR: IDF directory not found: {idf_dir}", file=sys.stderr)
        sys.exit(1)

    # ── Pre-flight mode (single building) ────────────────────────────────────
    if args.building or args.idf:
        idf_path = Path(args.idf) if args.idf else None
        building = args.building or (idf_path.stem if idf_path else None)
        if not building:
            print("ERROR: --building or --idf required for pre-flight mode", file=sys.stderr)
            sys.exit(1)
        ok = preflight_registry(building, idf_dir, out_path, idf_path=idf_path)
        sys.exit(0 if ok else 1)

    # ── Full batch mode ───────────────────────────────────────────────────────
    existing = {}
    if out_path.exists():
        with open(out_path) as f:
            existing = json.load(f)
        print(f"Loaded existing registry: {len(existing) - 1} buildings (will merge)")
    else:
        print("No existing registry found — generating from scratch")

    print(f"\nScanning IDF files (recursive) in: {idf_dir}")
    registry = build_registry(idf_dir, existing)

    n_buildings = len(registry) - 1  # exclude _notes
    n_gt = sum(1 for k, v in registry.items() if k != "_notes" and v.get("has_ground_truth"))

    print(f"\n{'─' * 55}")
    print(f"Registry summary: {n_buildings} buildings, {n_gt} with ground truth")

    if args.dry_run:
        print("\n[DRY RUN] Output:\n")
        print(json.dumps(registry, indent=2))
        return

    with open(out_path, "w") as f:
        json.dump(registry, f, indent=2)

    print(f"Written: {out_path}")
    print(f"\nDone. Run this script after any IDF changes to keep the registry in sync.")


if __name__ == "__main__":
    main()
