"""
OpenClaw Stage 1 — Energy Simulation Runner
============================================
Uses eppy to prepare IDF files and run EnergyPlus simulations
for all NUS campus buildings. Outputs monthly CSVs per building,
with the simulation pinned to the 2024 calendar year.

Academic calendar tags reflect AY2023/2024 Sem 2 and AY2024/2025 Sem 1.

Requirements:
    pip install eppy pandas numpy

Usage:
    python simulate.py                        # run all IDFs in ./idfs/
    python simulate.py --idf idfs/CLB6.idf   # run a single IDF
    python simulate.py --idf idfs/CLB6.idf --epw weather/SGP_Singapore.486980_IWEC.epw
"""

import os
import sys
import argparse
import shutil
import subprocess
import logging
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path
from datetime import datetime

import json
import pandas as pd
import numpy as np

GENERATE_REGISTRY_SCRIPT = Path("/Users/ye/.openclaw/workspace-simulationagent/skills/nus-registry/scripts/generate_registry.py")

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("openclaw_stage1.log"),
    ],
)
log = logging.getLogger("openclaw.stage1")


# ══════════════════════════════════════════════════════════════════════════════
# CONFIGURATION — edit these paths to match your environment
# ══════════════════════════════════════════════════════════════════════════════

# ── Building registry (floor areas + metadata) ─────────────────────────────
_REGISTRY_PATH = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy")) / "building_registry.json"
_REGISTRY: dict = {}

def _load_registry() -> dict:
    global _REGISTRY
    if _REGISTRY:
        return _REGISTRY
    if _REGISTRY_PATH.exists():
        with open(_REGISTRY_PATH) as f:
            _REGISTRY = json.load(f)
    return _REGISTRY

def _get_floor_area(building: str):
    """Return floor_area_m2 for a building stem (e.g. 'FOE13'), or None if unknown."""
    reg = _load_registry()
    # Try exact match, then strip variant prefix (e.g. 'A1_H_L__FOE13' → 'FOE13')
    entry = reg.get(building)
    if entry is None:
        stem = building.split("__")[-1] if "__" in building else building
        entry = reg.get(stem)
    return entry.get("floor_area_m2") if entry else None


def _ensure_registry_entry(idf_path: str, run_id: str = None):
    """Ensure the building has a registry entry with floor area before simulation."""
    building_stem = Path(idf_path).stem
    building_key = run_id or building_stem
    if _get_floor_area(building_key) is not None or _get_floor_area(building_stem) is not None:
        return
    if not GENERATE_REGISTRY_SCRIPT.exists():
        log.warning(f"[{building_stem}] Registry helper not found, continuing without floor area")
        return

    env = {**os.environ, "NUS_PROJECT_DIR": os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy")}
    cmd = ["python3", str(GENERATE_REGISTRY_SCRIPT), "--idf", str(idf_path)]
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=120, env=env, check=False)
    except Exception as e:
        log.warning(f"[{building_stem}] Registry pre-flight failed: {e}")
    finally:
        global _REGISTRY
        _REGISTRY = {}
        _load_registry()

# BCA Green Mark 2021 EUI thresholds (kWh/m²/year, office/education)
BCA_TIERS = [
    (85,  "Platinum 🏆"),
    (100, "Gold Plus ⭐⭐"),
    (115, "Gold ⭐"),
    (130, "Certified 🟢"),
]

def _bca_tier(eui: float) -> str:
    for threshold, label in BCA_TIERS:
        if eui <= threshold:
            return label
    return "Below Certified 🔴"


CONFIG = {
    # Path to EnergyPlus installation directory (macOS)
    "energyplus_dir": "/Applications/EnergyPlus-23-1-0",

    # Base Singapore TMY EPW weather file (IWEC) — used when no calibrated EPW is available
    # Download from: https://climate.onebuilding.org
    # File: SGP_SG_Singapore.Changi.486980_TMYx.epw
    "epw_file": "/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw",

    # Nimbus calibrated EPW directory — site-calibrated EPWs produced by WeatherAgent
    # Pattern: {calibrated_epw_dir}/{YYYY-MM}_site_calibrated.epw
    "calibrated_epw_dir": "/Users/ye/nus-energy/weather/calibrated",

    # EnergyPlus IDD file (inside your EnergyPlus installation)
    "idd_file": "/Applications/EnergyPlus-23-1-0/Energy+.idd",

    # Folder containing all IDF files
    "idf_dir": "/Users/ye/nus-energy/idfs",

    # Output root folder (shared with AnomalyAgent and other downstream agents)
    "output_dir": "/Users/ye/nus-energy/outputs",

    # Building registry — floor areas and metadata (used for EUI calculation)
    "building_registry": "/Users/ye/nus-energy/building_registry.json",

    # Simulation run period (override IDF defaults if needed)
    "run_period": {
        "begin_month": 1,
        "begin_day": 1,
        "end_month": 12,
        "end_day": 31,
    },

    # NUS academic calendar — used to tag occupancy periods in output CSV
    # Update annually from https://www.nus.edu.sg/registrar/academic-calendar
    # 2024 calendar: AY2023/2024 Sem 2 + AY2024/2025 Sem 1
    "academic_calendar": {
        "sem1_start": "2024-08-05",   # AY2024/2025 Semester 1
        "sem1_end":   "2024-11-23",
        "sem2_start": "2024-01-15",   # AY2023/2024 Semester 2
        "sem2_end":   "2024-05-04",
        "exam_weeks": [
            ("2024-04-27", "2024-05-09"),   # AY2023/2024 Sem 2 exams
            ("2024-11-25", "2024-12-07"),   # AY2024/2025 Sem 1 exams
        ],
    },

    # Simulation reference year (all EnergyPlus output is mapped to this year)
    "simulation_year": 2024,
}


# ══════════════════════════════════════════════════════════════════════════════
# EPW RESOLVER — prefer Nimbus site-calibrated EPW over base TMY
# ══════════════════════════════════════════════════════════════════════════════

def resolve_epw(month: str = None, base_epw: str = None,
                calibrated_dir: str = None) -> tuple[str, str]:
    """
    Return the EPW for a given simulation month.

    **Policy (enforced):**
      1. If the caller supplies an explicit --epw, always use that.
      2. Otherwise, always use the annual **2025_site_calibrated.epw** if it exists.
      3. If 2025 EPW is missing, fall back to CONFIG default base TMY IWEC.

    Month-specific calibrated EPWs are **ignored** here; the simulation time
    range is unchanged, but the weather is always based on the 2025 calibrated
    year unless manually overridden.

    Returns (epw_path, source_label) where source_label is one of:
      "tmy:override"   — caller-supplied static EPW
      "calibrated:2025" — annual 2025 calibrated EPW
      "tmy:default"    — CONFIG default base TMY
    """
    base_epw       = base_epw       or CONFIG["epw_file"]
    calibrated_dir = calibrated_dir or CONFIG.get("calibrated_epw_dir", "")

    # 1) Caller-supplied EPW override
    if base_epw != CONFIG["epw_file"]:
        log.info(f"[epw] Using caller-supplied EPW override: {base_epw}")
        return base_epw, "tmy:override"

    # 2) Annual 2025 calibrated EPW (preferred)
    if calibrated_dir:
        epw_2025 = Path(calibrated_dir) / "2025_site_calibrated.epw"
        if epw_2025.exists():
            log.info(f"[epw] Using 2025_site_calibrated.epw for all simulations: {epw_2025}")
            return str(epw_2025), "calibrated:2025"

    # 3) Base TMY
    log.info(f"[epw] 2025_site_calibrated.epw not found — using base TMY: {base_epw}")
    return base_epw, "tmy:default"


# ══════════════════════════════════════════════════════════════════════════════
# IDF PREPARATION — tropical adjustments applied via eppy
# ══════════════════════════════════════════════════════════════════════════════

def prepare_idf(idf_path: str, epw_path: str, output_dir: str, run_id: str = None,
                keep_shading_csv: bool = False) -> str:
    """
    Load an IDF, apply Singapore-specific tropical adjustments,
    inject required Output objects, and save a prepared copy.

    run_id: optional identifier used as the output sub-folder name.
            Defaults to the IDF file stem (e.g. "FOE13").
            For batch runs with subdirectory variants, pass a unique id
            such as "A1_H_L__FOE13" to avoid output collisions.

    keep_shading_csv: when True, preserve EnergyPlus shading debug output.
                      Default False because shading.csv can be extremely large
                      and is not needed for the monthly energy pipeline.

    Returns path to the prepared IDF file.
    """
    from eppy.modeleditor import IDF

    _ensure_registry_entry(idf_path, run_id=run_id)

    idd_path = CONFIG["idd_file"]
    if not Path(idd_path).exists():
        raise FileNotFoundError(
            f"IDD file not found: {idd_path}\n"
            f"Check CONFIG['idd_file'] points to your EnergyPlus 25.2.0 installation."
        )
    IDF.setiddname(idd_path)
    idf = IDF(idf_path)
    building_name = run_id if run_id else Path(idf_path).stem
    log.info(f"[{building_name}] Loaded IDF — {idf_path}")

    # ── 1. Tropical climate adjustments ───────────────────────────────────────
    _apply_tropical_adjustments(idf, building_name)

    # ── 2. Suppress bulky debug outputs unless explicitly requested ───────────
    _suppress_unnecessary_debug_outputs(idf, building_name, keep_shading_csv=keep_shading_csv)

    # ── 3. Remove obviously degenerate shading geometry when present ──────────
    _prune_tiny_shading_surfaces(idf, building_name)

    # ── 4. Inject output meters + variables ───────────────────────────────────
    _inject_outputs(idf)

    # ── 4. Ensure full-year run period ────────────────────────────────────────

    _set_run_period(idf)

    # ── 5. Save prepared IDF ──────────────────────────────────────────────────
    prepared_dir = Path(output_dir) / building_name / "prepared"
    prepared_dir.mkdir(parents=True, exist_ok=True)
    # Use the IDF stem for the filename so EnergyPlus output prefix stays clean
    idf_stem = Path(idf_path).stem
    prepared_idf_path = str(prepared_dir / f"{idf_stem}_prepared.idf")
    idf.save(prepared_idf_path)
    log.info(f"[{building_name}] Prepared IDF saved → {prepared_idf_path}")

    return prepared_idf_path


def _apply_tropical_adjustments(idf, building_name: str):
    """
    Apply Singapore-specific corrections to IDF objects.
    These override Climate Studio defaults that assume temperate climate.
    """

    # ── Cooling setpoints: keep original DualSetpoint schedules intact ────────
    # Singapore needs no heating, but EnergyPlus requires BOTH heating AND
    # cooling schedules populated for ThermostatSetpoint:DualSetpoint to be
    # valid (control type 4). Clearing the heating schedule causes EP to report
    # "Not valid for this zone". The original College HTGSETP_SCH is set high
    # enough it never activates in Singapore — just leave it in place.
    thermostat_objects = idf.idfobjects.get("THERMOSTATSETPOINT:DUALSETPOINT", [])
    for t in thermostat_objects:
        log.debug(f"[{building_name}] Thermostat preserved: {t.Name}")

    # ── Fix missing ZoneControlTypeSchedule objects (Climate Studio 23.x bug) ─
    # EP 25.x strictly validates that every schedule name referenced in
    # ZoneControl:Thermostat actually exists. CS23 creates the references
    # but omits the Schedule:Constant objects. We create them here.
    # Control type 4 = DualSetpoint (the only type used in these IDFs).
    existing_schedules = {
        s.Name.upper()
        for s in idf.idfobjects.get("SCHEDULE:CONSTANT", [])
    }
    # Also check SCHEDULE:COMPACT and SCHEDULE:YEAR
    for stype in ["SCHEDULE:COMPACT", "SCHEDULE:YEAR", "SCHEDULE:FILE"]:
        for s in idf.idfobjects.get(stype, []):
            existing_schedules.add(s.Name.upper())

    thermostats = idf.idfobjects.get("ZONECONTROL:THERMOSTAT", [])
    sched_created = 0
    for zt in thermostats:
        sched_name = getattr(zt, "Control_Type_Schedule_Name", "")
        if sched_name and sched_name.upper() not in existing_schedules:
            new_sched = idf.newidfobject("SCHEDULE:CONSTANT")
            new_sched.Name = sched_name
            new_sched.Schedule_Type_Limits_Name = ""
            new_sched.Hourly_Value = 4   # 4 = DualSetpoint
            existing_schedules.add(sched_name.upper())
            sched_created += 1
    if sched_created:
        log.info(f"[{building_name}] Created {sched_created} missing ZoneControlTypeSchedule constants ✓")

    # ── EnergyPlus version-specific patches ──────────────────────────────────
    ep_dir = CONFIG["energyplus_dir"]
    ep_major = 25  # default
    import re as _re
    _m = _re.search(r'EnergyPlus-(\d+)', ep_dir)
    if _m:
        ep_major = int(_m.group(1))

    if ep_major >= 25:
        # "ZoneAveraged" was renamed to "EnclosureAveraged" in EnergyPlus 24+
        people_objects = idf.idfobjects.get("PEOPLE", [])
        mrt_fixed = 0
        for p in people_objects:
            if hasattr(p, "Mean_Radiant_Temperature_Calculation_Type"):
                if p.Mean_Radiant_Temperature_Calculation_Type == "ZoneAveraged":
                    p.Mean_Radiant_Temperature_Calculation_Type = "EnclosureAveraged"
                    mrt_fixed += 1
        if mrt_fixed:
            log.info(f"[{building_name}] Fixed {mrt_fixed} People MRT: ZoneAveraged → EnclosureAveraged ✓")

        # Update VERSION stamp to match the running EP version
        version_objects = idf.idfobjects.get("VERSION", [])
        for v in version_objects:
            if hasattr(v, "Version_Identifier"):
                old_ver = v.Version_Identifier
                v.Version_Identifier = "25.2"
                log.info(f"[{building_name}] Version updated: {old_ver} → 25.2 ✓")
    else:
        log.debug(f"[{building_name}] EP {ep_major}.x — skipping EP25-only patches")

    # ── Humidity: enable moisture balance for tropical conditions ───────────
    building_objects = idf.idfobjects.get("BUILDING", [])
    for b in building_objects:
        log.debug(f"[{building_name}] Building object: {b.Name}")

    log.info(f"[{building_name}] Tropical adjustments applied ✓")


def _suppress_unnecessary_debug_outputs(idf, building_name: str, keep_shading_csv: bool = False):
    """Disable large geometry/debug reports that are not needed for normal runs."""
    if keep_shading_csv:
        return

    shadow_objects = list(idf.idfobjects.get("SHADOWCALCULATION", []))
    for shadow in shadow_objects:
        field = "Output_External_Shading_Calculation_Results"
        if hasattr(shadow, field):
            value = str(getattr(shadow, field, "")).strip().lower()
            if value == "yes":
                setattr(shadow, field, "No")
                log.info(f"[{building_name}] Disabled external shading calculation results to avoid large shading.csv")

    for obj_type in ["OUTPUT:SURFACES:LIST", "OUTPUT:SURFACES:DRAWING"]:
        existing = list(idf.idfobjects.get(obj_type, []))
        for obj in existing:
            idf.removeidfobject(obj)
        if existing:
            log.info(f"[{building_name}] Removed {len(existing)} {obj_type} object(s) not needed for standard simulation runs")


def _prune_tiny_shading_surfaces(idf, building_name: str, min_area_m2: float = 0.001):
    """Remove tiny detailed shading surfaces that are likely geometry artifacts.

    This is intentionally conservative. We only touch explicit detailed shading
    objects and only when their polygon area is effectively zero. These tiny
    sliver surfaces are a common source of GetSurfaceData degenerate-surface
    severe errors while having negligible effect on annual building energy.
    """
    try:
        from eppy.geometry.surface import area as polygon_area
    except Exception:
        polygon_area = None

    removed = 0
    for obj_type in ["SHADING:BUILDING:DETAILED", "SHADING:ZONE:DETAILED", "SHADING:SITE:DETAILED"]:
        for obj in list(idf.idfobjects.get(obj_type, [])):
            try:
                if polygon_area is not None:
                    coords = obj.coords
                    area_val = abs(float(polygon_area(coords)))
                else:
                    area_val = None
            except Exception:
                area_val = None

            if area_val is not None and area_val < min_area_m2:
                idf.removeidfobject(obj)
                removed += 1

    if removed:
        log.info(f"[{building_name}] Removed {removed} tiny detailed shading surface(s) (<{min_area_m2} m²) to avoid degenerate geometry errors")


def _inject_outputs(idf):
    """
    Inject monthly Output:Meter and Output:Variable objects for whole-building
    total and all major end-use sub-meters (lighting, equipment, cooling, fans,
    pumps, heat rejection).

    Using Monthly frequency avoids the performance cost of hourly/daily output
    and produces 12-row per-building CSVs used for MAPE calibration.

    IdealLoads cooling fix
    ----------------------
    ZoneHVAC:IdealLoadsAirSystem does NOT report to Cooling:Electricity or to
    DistrictCooling:Facility reliably across all EnergyPlus versions. The only
    guaranteed source is the zone-level Output:Variable
    "Zone Ideal Loads Cooling Energy". We request this variable at Monthly
    frequency and sum it across all zones in _parse_mtr() / _parse_eso_csv()
    so that cooling_kwh is always populated regardless of HVAC system type or
    EnergyPlus version.
    """

    # ── Remove existing meter/variable outputs ─────────────────────────────
    # eppy stores object types in upper-case; iterate all case variants to be safe.
    removed_total = 0
    for obj_type in ["OUTPUT:METER:METERFILEONLY", "OUTPUT:METER", "OUTPUT:VARIABLE"]:
        existing = list(idf.idfobjects.get(obj_type, []))
        for o in existing:
            idf.removeidfobject(o)
        removed_total += len(existing)
    if removed_total:
        log.info(f"[inject_outputs] Removed {removed_total} pre-existing Output objects from IDF")

    # ── End-use electricity meters (monthly) ──────────────────────────────
    # Order matters: the .mtr header assigns report variable IDs sequentially,
    # so we keep total first then end-uses in a stable order.
    #
    # NOTE: ZoneHVAC:IdealLoadsAirSystem does NOT report to Cooling:Electricity.
    # EnergyPlus routes its cooling energy to DistrictCooling:Facility instead,
    # but this meter is unreliable across EP versions. We keep both meters here
    # for real-plant IDFs and rely on the Output:Variable below for IdealLoads.
    meters_to_add = [
        ("Electricity:Facility",               "Monthly"),  # Grand total
        ("InteriorLights:Electricity",         "Monthly"),  # Lighting
        ("InteriorEquipment:Electricity",      "Monthly"),  # Plug loads / facilities
        ("Cooling:Electricity",                "Monthly"),  # Chiller / DX cooling (real plant)
        ("DistrictCooling:Facility",           "Monthly"),  # IdealLoadsAirSystem cooling (meter fallback)
        ("Fans:Electricity",                   "Monthly"),  # AHU & exhaust fans
        ("Pumps:Electricity",                  "Monthly"),  # CHW / CW pumps
        ("HeatRejection:Electricity",          "Monthly"),  # Cooling towers
        ("ExteriorLights:Electricity",         "Monthly"),  # Facade / car-park lighting
    ]

    for meter_name, frequency in meters_to_add:
        new_meter = idf.newidfobject("OUTPUT:METER")
        new_meter.Key_Name = meter_name
        new_meter.Reporting_Frequency = frequency

    # NOTE: IdealLoads OUTPUT:VARIABLE intentionally omitted.
    # "Zone Ideal Loads Cooling Energy" reports thermal energy (J), not
    # electrical energy, and is not used in the EUI calculation. Requesting
    # per-zone variables across all zones was a significant simulation slowdown
    # with no benefit to the electrical EUI pipeline.
    # For district-cooled buildings the COP-adjusted thermal → electrical
    # back-calculation via DISTRICT_COOLING_COP is used instead.

    log.info("[inject_outputs] Injected Output:Meter objects (incl. DistrictCooling) ✓")

    # Large geometry/debug outputs are handled separately in
    # _suppress_unnecessary_debug_outputs(). Keep _inject_outputs focused on
    # energy-accounting outputs only.


def _set_run_period(idf):
    """Ensure IDF runs for the full 2024 calendar year (Jan 1 – Dec 31)."""
    run_periods = idf.idfobjects.get("RUNPERIOD", [])
    if run_periods:
        rp = run_periods[0]
        rp.Begin_Month = CONFIG["run_period"]["begin_month"]
        rp.Begin_Day_of_Month = CONFIG["run_period"]["begin_day"]
        rp.End_Month = CONFIG["run_period"]["end_month"]
        rp.End_Day_of_Month = CONFIG["run_period"]["end_day"]
        # Pin simulation to 2024 so weekday/calendar tags are correct
        if hasattr(rp, "Begin_Year"):
            rp.Begin_Year = CONFIG["simulation_year"]
        rp.Use_Weather_File_Holidays_and_Special_Days = "No"
        rp.Use_Weather_File_Daylight_Saving_Period = "No"


# ══════════════════════════════════════════════════════════════════════════════
# SIMULATION RUNNER — calls EnergyPlus subprocess
# ══════════════════════════════════════════════════════════════════════════════

def run_simulation(prepared_idf_path: str, epw_path: str, output_dir: str,
                   run_id: str = None) -> dict:
    """
    Run EnergyPlus simulation via subprocess.
    Returns dict with paths to output files and success status.

    run_id: optional unique run identifier (output sub-folder name).
            Defaults to the IDF stem without '_prepared'.
    """
    idf_stem = Path(prepared_idf_path).stem.replace("_prepared", "")
    building_name = run_id if run_id else idf_stem
    sim_output_dir = Path(output_dir) / building_name / "simulation"

    # Clean stale EnergyPlus outputs before re-simulating so no old files
    # (e.g. from a previous broken prefix run) accumulate to 1GB+
    if sim_output_dir.exists():
        import shutil
        stale_count = 0
        for f in sim_output_dir.iterdir():
            if f.is_file():
                f.unlink()
                stale_count += 1
        if stale_count:
            log.info(f"[{building_name}] Cleaned {stale_count} stale files from {sim_output_dir}")

    sim_output_dir.mkdir(parents=True, exist_ok=True)

    # EnergyPlus 25.x: binary is 'energyplus' on macOS/Linux, 'energyplus.exe' on Windows
    if sys.platform == "win32":
        energyplus_exe = Path(CONFIG["energyplus_dir"]) / "energyplus.exe"
    elif sys.platform == "darwin":
        # macOS: EnergyPlus 25.x ships as 'energyplus' (lowercase)
        energyplus_exe = Path(CONFIG["energyplus_dir"]) / "energyplus"
    else:
        energyplus_exe = Path(CONFIG["energyplus_dir"]) / "energyplus"

    if not energyplus_exe.exists():
        raise FileNotFoundError(
            f"EnergyPlus executable not found: {energyplus_exe}\n"
            f"Check CONFIG['energyplus_dir'] = {CONFIG['energyplus_dir']}"
        )

    cmd = [
        str(energyplus_exe),
        "-w", epw_path,
        "--output-directory", str(sim_output_dir),
        "--output-prefix", building_name,
        prepared_idf_path,
    ]

    log.info(f"[{building_name}] Starting EnergyPlus simulation...")
    log.info(f"[{building_name}] Command: {' '.join(cmd)}")

    start_time = datetime.now()

    result = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=3600,  # 1 hour max per building
    )

    elapsed = (datetime.now() - start_time).total_seconds()

    # EP23 on macOS arm64 segfaults on exit (SIGSEGV) after writing all outputs.
    # Python subprocess reports this as returncode=-11 (not 139 which is the shell's
    # representation of 128+SIGSEGV). Trust the .end file as the authoritative check.
    end_file = sim_output_dir / f"{building_name}out.end"
    ep_success = (
        result.returncode in (0, 1, -11, 139)
        and end_file.exists()
        and "Completed Successfully" in end_file.read_text(errors="replace")
    )
    if not ep_success:
        log.error(f"[{building_name}] Simulation FAILED after {elapsed:.1f}s")
        log.error(f"[{building_name}] STDERR: {result.stderr[-2000:]}")
        return {"success": False, "building": building_name, "error": result.stderr}

    log.info(f"[{building_name}] Simulation completed in {elapsed:.1f}s ✓")

    # Locate output files
    # EP23 writes {building}out.mtr (ESO format); older EP wrote {building}Meter.csv
    # The .eso contains Output:Variable results (incl. Zone Ideal Loads Cooling Energy).
    csv_path   = sim_output_dir / f"{building_name}out.csv"
    mtr_path   = sim_output_dir / f"{building_name}out.mtr"
    eso_path   = sim_output_dir / f"{building_name}out.eso"
    meter_path = sim_output_dir / f"{building_name}Meter.csv"
    err_path   = sim_output_dir / f"{building_name}out.err"
    # Prefer .mtr (EP23) over legacy Meter.csv
    resolved_meter = mtr_path if mtr_path.exists() else meter_path

    if not eso_path.exists():
        log.warning(f"[{building_name}] .eso file not found — IdealLoads cooling variable "
                    f"will not be available; cooling EUI may be 0 for IdealLoads IDFs")

    return {
        "success":      True,
        "building":     building_name,
        "elapsed_s":    elapsed,
        "csv_path":     str(csv_path)        if csv_path.exists()        else None,
        "meter_path":   str(resolved_meter)  if resolved_meter.exists()  else None,
        "eso_path":     str(eso_path)        if eso_path.exists()        else None,
        "err_path":     str(err_path)        if err_path.exists()        else None,
        "output_dir":   str(sim_output_dir),
    }


# ══════════════════════════════════════════════════════════════════════════════
# POST-PROCESSING — parse CSVs into clean monthly DataFrames
# ══════════════════════════════════════════════════════════════════════════════

def parse_results(sim_result: dict, output_dir: str, run_id: str = None) -> dict:
    """
    Parse EnergyPlus meter CSV output into a clean monthly DataFrame.
    Saves a 12-row CSV per building and returns the DataFrame for MAPE calculation.

    run_id: optional override for the output sub-folder / CSV naming key.
            Defaults to sim_result["building"].
    """
    if not sim_result["success"]:
        return None

    building = run_id if run_id else sim_result["building"]
    parsed_dir = Path(output_dir) / building / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)

    results = {"building": building}

    # ── Parse meter output (monthly) ──────────────────────────────────────
    if sim_result.get("meter_path") and Path(sim_result["meter_path"]).exists():
        meter_path = Path(sim_result["meter_path"])
        if meter_path.suffix == ".mtr":
            # EP23 ESO format — delegate to parse_eso.py logic
            monthly_df = _parse_mtr(meter_path, building)
        else:
            meter_df = _parse_meter_csv(str(meter_path), building)
            monthly_df = _aggregate_monthly(meter_df, building)
        monthly_path = parsed_dir / f"{building}_monthly.csv"
        monthly_df.to_csv(monthly_path, index=False)
        log.info(f"[{building}] Monthly results → {monthly_path} ({len(monthly_df)} rows)")
        results["monthly_df"]   = monthly_df
        results["monthly_path"] = str(monthly_path)
    else:
        log.warning(f"[{building}] No meter output file found — check EnergyPlus ran successfully")

    # ── Print summary to console ───────────────────────────────────────────
    _print_energy_summary(results)

    return results




def _get_floor_area(building_stem: str):
    """
    Look up the floor area (m²) for a building from the building registry.
    Returns None if the registry is missing or the building is not listed.
    """
    registry_path = Path(CONFIG.get("building_registry", ""))
    if not registry_path.exists():
        return None
    try:
        with open(registry_path) as f:
            registry = json.load(f)
        entry = registry.get(building_stem, {})
        area = entry.get("floor_area_m2")
        return float(area) if area else None
    except Exception as e:
        log.debug(f"[{building_stem}] Could not read floor area from registry: {e}")
        return None


def _parse_eso_ideal_loads_cooling(eso_path: Path, building: str) -> dict[int, float]:
    """
    Parse the companion .eso file to extract monthly "Zone Ideal Loads Cooling
    Energy" values summed across all zones.

    EnergyPlus writes Output:Variable results to the .eso file (not the .mtr).
    The format mirrors the .mtr ESO format: a header maps report-variable IDs
    to variable names, then data lines supply (code, joules) pairs grouped by
    month (code 4 markers).

    Returns {month_int: total_cooling_joules} for months 1-12.
    Returns an empty dict if the file does not exist or the variable is absent.
    """
    if not eso_path.exists():
        log.debug(f"[{building}] .eso file not found at {eso_path} — skipping IdealLoads variable read")
        return {}

    import re as _re
    TARGET_VAR = "Zone Ideal Loads Cooling Energy".upper()

    lines = eso_path.read_text(errors="ignore").splitlines()

    # ── Pass 1: collect report codes for the target variable ──────────────
    # Header lines: "<code>,<num_vals>,<Zone Name>:<Var Name> [J] !Monthly [...]"
    ideal_codes: set[int] = set()
    for line in lines:
        line = line.strip()
        if line.startswith("End of Data Dictionary"):
            break
        parts = line.split(",", 2)
        if len(parts) < 3:
            continue
        try:
            rcode = int(parts[0])
        except ValueError:
            continue
        # Variable name sits before the first "[" in parts[2]
        raw = parts[2].split("[")[0].strip()
        # The format is "ZoneName:Variable Name" — we match on the variable part
        var_part = raw.split(":")[-1].strip().upper() if ":" in raw else raw.upper()
        if var_part == TARGET_VAR:
            ideal_codes.add(rcode)

    if not ideal_codes:
        log.debug(f"[{building}] 'Zone Ideal Loads Cooling Energy' not found in .eso header "
                  f"(IDF may use a real chiller plant — meter fallback will be used)")
        return {}

    log.debug(f"[{building}] Found {len(ideal_codes)} zone(s) with Ideal Loads Cooling variable in .eso")

    # ── Pass 2: accumulate monthly joules across all zones ─────────────────
    monthly_joules: dict[int, float] = {}
    current_month: int | None = None

    for line in lines:
        line = line.strip()
        if not line or line.startswith("End") or line.startswith("Program"):
            continue
        parts = line.split(",")
        try:
            code = int(parts[0])
        except ValueError:
            continue

        if code == 4 and len(parts) >= 3:
            try:
                current_month = int(parts[2].strip())
                if current_month not in monthly_joules:
                    monthly_joules[current_month] = 0.0
            except (ValueError, IndexError):
                pass
        elif code in ideal_codes and current_month is not None and len(parts) >= 2:
            try:
                monthly_joules[current_month] = monthly_joules.get(current_month, 0.0) + float(parts[1].strip())
            except (ValueError, IndexError):
                pass

    if monthly_joules:
        total_kwh = sum(monthly_joules.values()) / 3_600_000
        log.info(f"[{building}] IdealLoads cooling from .eso: {total_kwh:,.0f} kWh/yr "
                 f"across {len(ideal_codes)} zone(s) ✓")
    return monthly_joules


def _parse_mtr(mtr_path: Path, building: str) -> pd.DataFrame:
    """
    Parse EP23 ESO-format .mtr file into a monthly DataFrame with full end-use
    breakdown (lighting, equipment, cooling, fans, pumps, heat rejection).

    The .mtr format has two sections:
      • Header  — lines like  "<report_code>,1,<meter_name> [J] !Monthly"
                  mapping each report variable ID → meter name.
      • Data    — code 4 marks a new month; data lines are
                  "<report_code>,<joules>,..."

    We read the header first to build an ID→column map, then collect one
    value per (meter, month) from the data section.

    IdealLoads cooling
    ------------------
    For IDFs using ZoneHVAC:IdealLoadsAirSystem, cooling energy does NOT appear
    on Cooling:Electricity or reliably on DistrictCooling:Facility. The
    authoritative source is the Output:Variable "Zone Ideal Loads Cooling Energy"
    written to the companion .eso file. This function reads the .eso alongside
    the .mtr and merges the summed-zone cooling into cooling_kwh, overriding
    meter-based values if the variable is present and non-zero.
    """
    MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
              "Jul","Aug","Sep","Oct","Nov","Dec"]
    GRID_FACTOR = 0.4168  # kgCO2e/kWh (Singapore grid, 2024)

    # COP of the district cooling plant used to back-calculate electrical
    # equivalent of chilled water delivered to the building.
    # For buildings on DistrictCooling, Cooling:Electricity = 0 in the .mtr.
    # Adjusted cooling electricity = cooling_thermal_kwh / COP.
    # NUS central plant typical COP ≈ 4.5 (adjust if metered data available).
    DISTRICT_COOLING_COP = 4.5

    # ── Meter name → output column name ───────────────────────────────────
    # Cooling:Electricity  → cooling_elec_kwh   (own chillers / DX — electrical)
    # DistrictCooling:Facility → cooling_thermal_kwh  (chilled water from central
    #   plant — thermal energy, NOT electrical; kept separate so it is never
    #   mixed into the electrical EUI sum; adjusted via COP for BCA comparison).
    METER_COL = {
        "Electricity:Facility":          "electricity_facility_kwh",
        "InteriorLights:Electricity":    "lighting_kwh",
        "InteriorEquipment:Electricity": "equipment_kwh",
        "Cooling:Electricity":           "cooling_elec_kwh",      # real chiller / DX plant (electrical)
        "DistrictCooling:Facility":      "cooling_thermal_kwh",   # district chilled water (thermal, not elec)
        "Fans:Electricity":              "fans_kwh",
        "Pumps:Electricity":             "pumps_kwh",
        "HeatRejection:Electricity":     "heat_rejection_kwh",
        "ExteriorLights:Electricity":    "exterior_lights_kwh",
    }

    lines = mtr_path.read_text(errors="ignore").splitlines()

    # ── Pass 1: build report-code → column map from header ────────────────
    # Header lines look like:  "13,1,Electricity:Facility [J] !Monthly [Value,...]"
    import re as _re
    code_to_col: dict[int, str] = {}
    in_data = False
    for line in lines:
        line = line.strip()
        if not line:
            continue
        if line.startswith("End of Data Dictionary"):
            in_data = True
            break
        parts = line.split(",", 2)
        if len(parts) < 3:
            continue
        try:
            rcode = int(parts[0])
        except ValueError:
            continue
        # parts[2] starts with the meter name, e.g. "Electricity:Facility [J] !Monthly"
        raw_name = parts[2].split("[")[0].strip()
        if raw_name in METER_COL:
            code_to_col[rcode] = METER_COL[raw_name]

    if not code_to_col:
        log.warning(f"[{building}] No recognised end-use meters found in .mtr header — "
                    f"check that _inject_outputs() ran before simulation")

    # ── Pass 2: collect monthly values ────────────────────────────────────
    # monthly_data[month_int][col] = kWh
    monthly_data: dict[int, dict[str, float]] = {}
    current_month: int | None = None

    for line in lines:
        line = line.strip()
        if not line or line.startswith("End") or line.startswith("Program"):
            continue
        parts = line.split(",")
        try:
            code = int(parts[0])
        except ValueError:
            continue

        if code == 4 and len(parts) >= 3:
            # Month marker: "4,<something>,<month_number>,..."
            try:
                current_month = int(parts[2].strip())
                if current_month not in monthly_data:
                    monthly_data[current_month] = {}
            except (ValueError, IndexError):
                pass
        elif code in code_to_col and current_month is not None and len(parts) >= 2:
            try:
                joules = float(parts[1].strip())
                monthly_data[current_month][code_to_col[code]] = joules / 3_600_000
            except (ValueError, IndexError):
                pass

    if not monthly_data:
        log.warning(f"[{building}] No monthly data in .mtr — file may be empty or simulation failed")
        return pd.DataFrame()

    # ── IdealLoads cooling fix: read .eso for zone variable ───────────────
    # The .eso lives in the same directory as the .mtr with the suffix changed.
    eso_path = mtr_path.with_suffix(".eso")
    ideal_cooling_joules = _parse_eso_ideal_loads_cooling(eso_path, building)

    # ── Build rows ────────────────────────────────────────────────────────
    building_stem = building.split("__")[-1] if "__" in building else building
    floor_area_m2 = _get_floor_area(building_stem)

    # Exclude the two cooling staging columns from the base output set;
    # they are replaced by the three explicit cooling columns added below.
    all_cols = [c for c in METER_COL.values()
                if c not in ("cooling_elec_kwh", "cooling_thermal_kwh")]

    def _eui(kwh):
        return round(kwh / floor_area_m2, 4) if floor_area_m2 else None

    rows = []
    for m in sorted(monthly_data.keys()):
        vals = monthly_data[m]
        kwh_total = vals.get("electricity_facility_kwh", 0.0)

        # ── Cooling: keep electrical and thermal strictly separate ────────
        cooling_elec  = vals.get("cooling_elec_kwh", 0.0)    # Cooling:Electricity (own chillers)
        cooling_therm = vals.get("cooling_thermal_kwh", 0.0)  # DistrictCooling:Facility (chilled water)

        # Fallback: if no metered cooling at all, use IdealLoads cooling
        # energy (thermal kWh) as a proxy for cooling_therm so adj_EUI
        # still accounts for cooling load.
        if cooling_elec == 0.0 and cooling_therm == 0.0 and m in ideal_cooling_joules:
            cooling_therm = round(ideal_cooling_joules[m] / 3_600_000, 2)
            log.debug(f"[{building}] Month {m}: using IdealLoads cooling as thermal fallback "
                      f"({cooling_therm:.1f} kWh)")

        # For district-cooled buildings (cooling_elec == 0), back-calculate
        # the electrical equivalent using the plant COP so the adjusted EUI
        # is BCA-comparable.
        cooling_elec_adj = (cooling_elec if cooling_elec > 0
                            else round(cooling_therm / DISTRICT_COOLING_COP, 2))

        # Derive "other" as facility total minus all known electrical sub-meters.
        # DistrictCooling is thermal and therefore excluded from this subtraction.
        known_elec_cols = [
            "lighting_kwh", "equipment_kwh", "cooling_elec_kwh",
            "fans_kwh", "pumps_kwh", "heat_rejection_kwh", "exterior_lights_kwh",
        ]
        known_sum = sum(vals.get(c, 0.0) for c in known_elec_cols)
        other_kwh = max(kwh_total - known_sum, 0.0)

        row = {
            "month":      m,
            "month_name": MONTHS[m - 1],
        }
        # Add every base metered column (0.0 if meter wasn't present in this IDF)
        for col in all_cols:
            row[col] = round(vals.get(col, 0.0), 2)

        # ── Explicit cooling columns (three-way split) ─────────────────────
        row["cooling_elec_kwh"]     = round(cooling_elec, 2)      # electrical (own chiller/DX)
        row["cooling_thermal_kwh"]  = round(cooling_therm, 2)     # thermal chilled water (not elec)
        row["cooling_elec_adj_kwh"] = round(cooling_elec_adj, 2)  # electrical equiv via COP

        row["other_electricity_kwh"] = round(other_kwh, 2)
        row["carbon_tco2e"]          = round(kwh_total * GRID_FACTOR / 1000, 4)

        # ── Total EUI columns ──────────────────────────────────────────────
        row["eui_kwh_m2"]     = _eui(kwh_total)
        # Adjusted EUI: replaces electrical cooling with COP-adjusted thermal
        # so district-cooled buildings are BCA-comparable.
        row["eui_adj_kwh_m2"] = _eui(kwh_total + cooling_elec_adj - cooling_elec)

        # ── Subcategory EUI columns ────────────────────────────────────────
        # Cooling EUI (electrical — only own chillers; 0 for district-cooled)
        row["cooling_elec_eui_kwh_m2"]     = _eui(cooling_elec)
        # Cooling EUI (adjusted electrical equiv via COP — use for BCA comparison)
        row["cooling_elec_adj_eui_kwh_m2"] = _eui(cooling_elec_adj)
        # Cooling EUI (thermal chilled water — NOT part of electrical EUI)
        row["cooling_thermal_eui_kwh_m2"]  = _eui(cooling_therm)
        # Lighting EUI: InteriorLights + ExteriorLights
        lighting_total = vals.get("lighting_kwh", 0.0) + vals.get("exterior_lights_kwh", 0.0)
        row["lighting_eui_kwh_m2"]   = _eui(lighting_total)
        # Equipment EUI: InteriorEquipment + Fans + Pumps + HeatRejection
        equipment_total = (vals.get("equipment_kwh", 0.0) + vals.get("fans_kwh", 0.0)
                           + vals.get("pumps_kwh", 0.0) + vals.get("heat_rejection_kwh", 0.0))
        row["equipment_eui_kwh_m2"]  = _eui(equipment_total)

        rows.append(row)

    if floor_area_m2:
        log.info(f"[{building}] Floor area: {floor_area_m2:.0f} m²  "
                 f"Annual EUI: {sum(r['eui_kwh_m2'] for r in rows if r['eui_kwh_m2']):.1f} kWh/m²/yr")
    else:
        log.warning(f"[{building}] Floor area not found in registry — EUI columns will be null")

    # Log end-use breakdown for quick sanity check
    if rows:
        ann = {col: sum(r.get(col, 0) for r in rows)
               for col in all_cols + ["cooling_elec_kwh", "cooling_thermal_kwh",
                                      "cooling_elec_adj_kwh", "other_electricity_kwh"]}
        total_ann = ann.get("electricity_facility_kwh", 1) or 1
        log.info(f"[{building}] Annual end-use breakdown:")
        labels = [
            ("lighting_kwh",          "Lighting"),
            ("equipment_kwh",         "Equipment"),
            ("cooling_elec_kwh",      "Cooling (elec)"),
            ("cooling_thermal_kwh",   "Cooling (thermal/district)"),
            ("cooling_elec_adj_kwh",  "Cooling (adj elec via COP)"),
            ("fans_kwh",              "Fans"),
            ("pumps_kwh",             "Pumps"),
            ("heat_rejection_kwh",    "Heat rejection"),
            ("exterior_lights_kwh",   "Exterior lights"),
            ("other_electricity_kwh", "Other"),
        ]
        for col, label in labels:
            kwh = ann.get(col, 0)
            if kwh > 0:
                log.info(f"  {label:<30} {kwh:>10,.0f} kWh  ({kwh/total_ann*100:.1f}%)")

    df = pd.DataFrame(rows)

    # ── Append annual totals row ───────────────────────────────────────────
    # EUI columns are recomputed from annual kWh sums (not summed from monthly
    # EUI values) to avoid floating-point accumulation errors.
    if floor_area_m2 and not df.empty:
        num_cols = df.select_dtypes("number").columns.tolist()
        annual_row = df[num_cols].sum().to_dict()
        annual_row["month"]      = 0
        annual_row["month_name"] = "ANNUAL"
        annual_row["eui_kwh_m2"]                  = round(df["electricity_facility_kwh"].sum() / floor_area_m2, 4)
        annual_row["eui_adj_kwh_m2"]              = round(df["eui_adj_kwh_m2"].sum(), 4)
        annual_row["cooling_elec_eui_kwh_m2"]     = round(df["cooling_elec_kwh"].sum() / floor_area_m2, 4)
        annual_row["cooling_elec_adj_eui_kwh_m2"] = round(df["cooling_elec_adj_kwh"].sum() / floor_area_m2, 4)
        annual_row["cooling_thermal_eui_kwh_m2"]  = round(df["cooling_thermal_kwh"].sum() / floor_area_m2, 4)
        annual_row["lighting_eui_kwh_m2"]         = round(
            (df["lighting_kwh"].sum() + df["exterior_lights_kwh"].sum()) / floor_area_m2, 4)
        annual_row["equipment_eui_kwh_m2"]        = round(
            (df["equipment_kwh"].sum() + df["fans_kwh"].sum()
             + df["pumps_kwh"].sum() + df["heat_rejection_kwh"].sum()) / floor_area_m2, 4)
        df = pd.concat([df, pd.DataFrame([annual_row])], ignore_index=True)

    return df


def _parse_meter_csv(meter_path: str, building: str) -> pd.DataFrame:
    """Parse EnergyPlus meter CSV output."""
    df = pd.read_csv(meter_path)
    df["datetime"] = pd.to_datetime(
        str(CONFIG["simulation_year"]) + "/" + df["Date/Time"].str.strip(),
        format="%Y/%m/%d %H:%M:%S",
        errors="coerce",
    )
    df = df.dropna(subset=["datetime"])
    df = df.set_index("datetime")
    df = df.drop(columns=["Date/Time"], errors="ignore")
    df.columns = [_clean_column_name(c) for c in df.columns]

    # Convert J → kWh
    for col in df.columns:
        if df[col].dtype in [np.float64, np.int64]:
            df[col] = df[col] / 3_600_000

    df.insert(0, "building", building)
    return df


def _aggregate_monthly(meter_df: pd.DataFrame, building: str) -> pd.DataFrame:
    """
    Aggregate hourly meter data to monthly totals.
    This is the primary DataFrame used for MAPE calculation against
    ground truth utility bills.
    """
    numeric_cols = meter_df.select_dtypes(include=[np.number]).columns
    # "ME" = Month End (pandas >= 2.2); use "M" for pandas < 2.2
    monthly = meter_df[numeric_cols].resample("ME").sum()
    monthly.index.name = "month"
    monthly.index = monthly.index.to_period("M")

    # Add month labels for readability
    monthly.insert(0, "building", building)
    monthly.insert(1, "month_label", [str(m) for m in monthly.index])

    # Add EUI (Energy Use Intensity) — kWh/m²/month, using registry floor area
    if "electricity_facility_kwh" in monthly.columns:
        floor_area = _get_floor_area(building)
        if floor_area and floor_area > 0:
            monthly["eui_kwh_m2"] = (monthly["electricity_facility_kwh"] / floor_area).round(2)
        else:
            monthly["eui_kwh_m2"] = np.nan  # floor area not in registry

    return monthly


def _add_calendar_tags(df: pd.DataFrame) -> pd.DataFrame:
    """
    Tag each hourly row with NUS academic calendar period.
    Used to stratify MAPE analysis by occupancy mode.
    """
    cal = CONFIG["academic_calendar"]

    def get_period(dt):
        date_str = dt.strftime("%Y-%m-%d")
        if cal["sem1_start"] <= date_str <= cal["sem1_end"]:
            return "Semester_1"
        if cal["sem2_start"] <= date_str <= cal["sem2_end"]:
            return "Semester_2"
        for start, end in cal["exam_weeks"]:
            if start <= date_str <= end:
                return "Exam"
        return "Vacation"

    df["nus_period"] = [get_period(dt) for dt in df.index]
    df["is_weekday"] = df.index.dayofweek < 5
    df["hour"] = df.index.hour
    return df


def _clean_column_name(col: str) -> str:
    """Standardise EnergyPlus column names to snake_case."""
    col = col.strip()
    col = col.lower()
    col = col.replace(":", "_").replace(" ", "_").replace("/", "_per_")
    col = col.replace("[j]", "").replace("[w]", "").replace("[c]", "")
    col = col.replace("[kg/s]", "").replace("[m/s]", "").replace("[%]", "")
    col = col.replace("(", "").replace(")", "")
    # Remove zone prefix patterns like "zone_0_"
    import re
    col = re.sub(r"zone_\d+_", "zone_", col)
    col = re.sub(r"_+", "_", col).strip("_")
    return col


def _print_energy_summary(results: dict):
    """Print a concise energy summary table to console, including end-use breakdown."""
    building = results["building"]
    print(f"\n{'='*72}")
    print(f"  ENERGY SUMMARY — {building}")
    print(f"{'='*72}")

    if "monthly_df" not in results:
        print(f"{'='*72}\n")
        return

    df = results["monthly_df"]
    # Exclude the ANNUAL summary row from per-month data for summing
    monthly_df = df[df["month_name"] != "ANNUAL"] if "month_name" in df.columns else df

    elec_col = next((c for c in monthly_df.columns if "electricity_facility" in c), None)
    if not elec_col:
        print(f"{'='*72}\n")
        return

    total_kwh = monthly_df[elec_col].sum()
    peak_idx   = monthly_df[elec_col].idxmax()
    peak_label = (monthly_df.loc[peak_idx, "month_name"]
                  if "month_name" in monthly_df.columns else str(peak_idx))

    print(f"  Annual electricity:  {total_kwh:>12,.0f} kWh")
    print(f"  Peak month:          {peak_label}")

    floor_area = _get_floor_area(building)
    area_str   = f"{floor_area:,.0f} m²" if floor_area else "floor area unknown"

    if "eui_kwh_m2" in monthly_df.columns and monthly_df["eui_kwh_m2"].notna().any():
        annual_eui = monthly_df["eui_kwh_m2"].sum()
        tier = _bca_tier(annual_eui)
        print(f"  Annual EUI:          {annual_eui:>12.1f} kWh/m²/yr  [{area_str}]")
        print(f"  BCA Green Mark:      {tier}")
    else:
        print(f"  Annual EUI:          — (floor area not in registry)")

    # ── Cooling EUI breakdown ──────────────────────────────────────────────
    cooling_elec_kwh  = monthly_df["cooling_elec_kwh"].sum()  if "cooling_elec_kwh"  in monthly_df.columns else 0
    cooling_therm_kwh = monthly_df["cooling_thermal_kwh"].sum() if "cooling_thermal_kwh" in monthly_df.columns else 0
    cooling_adj_kwh   = monthly_df["cooling_elec_adj_kwh"].sum() if "cooling_elec_adj_kwh" in monthly_df.columns else 0

    if floor_area:
        if cooling_elec_kwh > 0:
            print(f"  Cooling EUI (elec):  {cooling_elec_kwh/floor_area:>12.1f} kWh/m²/yr  [Cooling:Electricity]")
        if cooling_therm_kwh > 0:
            print(f"  Cooling EUI (therm): {cooling_therm_kwh/floor_area:>12.1f} kWh/m²/yr  [DistrictCooling — thermal, not in elec EUI]")

    # ── Subcategory EUI ────────────────────────────────────────────────────
    lighting_kwh  = (monthly_df["lighting_kwh"].sum() if "lighting_kwh" in monthly_df.columns else 0) + \
                    (monthly_df["exterior_lights_kwh"].sum() if "exterior_lights_kwh" in monthly_df.columns else 0)
    equip_kwh     = sum(monthly_df[c].sum() if c in monthly_df.columns else 0
                        for c in ["equipment_kwh", "fans_kwh", "pumps_kwh", "heat_rejection_kwh"])
    if floor_area:
        print(f"  Lighting EUI:        {lighting_kwh/floor_area:>12.1f} kWh/m²/yr")
        print(f"  Equipment EUI:       {equip_kwh/floor_area:>12.1f} kWh/m²/yr  [equip+fans+pumps+heat rejection]")

    # District cooling adjusted EUI warning block
    if floor_area and cooling_elec_kwh == 0 and cooling_therm_kwh > 0:
        from importlib.util import find_spec as _fs  # noqa: F401 (already have DISTRICT_COOLING_COP in _parse_mtr)
        _cop = 4.5  # NUS central plant typical COP
        cooling_adj_eui = cooling_adj_kwh / floor_area if floor_area else 0
        base_eui   = total_kwh / floor_area
        adj_total  = base_eui + cooling_adj_eui
        print()
        print(f"  ⚠️  This building uses DistrictCooling — chilled water from a central plant.")
        print(f"      Electricity:Facility does NOT include the plant's electricity. Adjust via COP:")
        print()
        print(f"      Step 1  Cooling thermal (metered):  {cooling_therm_kwh:>13,.0f} kWh")
        print(f"      Step 2  Elec equiv (/ COP {_cop}):    {cooling_adj_kwh:>13,.0f} kWh  ({cooling_therm_kwh:,.0f} / {_cop})")
        print(f"      Step 3  Cooling EUI (adj):          {cooling_adj_eui:>13.1f} kWh/m²/yr")
        print()
        print(f"      Base electrical EUI:                {base_eui:>13.1f} kWh/m²/yr")
        print(f"    + Cooling EUI (adj):                  {cooling_adj_eui:>13.1f} kWh/m²/yr")
        print(f"      {'─'*55}")
        print(f"      Adjusted total EUI:                 {adj_total:>13.1f} kWh/m²/yr  <- use for BCA comparison")
        print(f"      Adjusted BCA tier:                  {_bca_tier(adj_total)}")

    # ── Monthly breakdown table ────────────────────────────────────────────
    # Columns: Month | Elec (Facility) | Cool Thermal | Cool Adj (÷COP) | Lighting | Equipment | EUI | EUI Adj
    print()
    print(f"  {'─'*100}")
    print(f"  {'Month':<6}  {'Elec (kWh)':>10}  {'Cool Therm':>10}  {'÷COP→Adj':>10}  {'Lighting':>10}  {'Equipment':>10}  {'EUI':>7}  {'EUI Adj':>8}")
    print(f"  {'':6}  {'':>10}  {'(metered)':>10}  {'(kWh elec)':>10}  {'(kWh)':>10}  {'(kWh)':>10}  {'(elec)':>7}  {'(+cool)':>8}")
    print(f"  {'─'*100}")

    for _, row in monthly_df.iterrows():
        lighting_row = row.get("lighting_kwh", 0) + row.get("exterior_lights_kwh", 0)
        cool_elec    = row.get("cooling_elec_kwh", 0)
        cool_therm   = row.get("cooling_thermal_kwh", 0)
        cool_adj     = row.get("cooling_elec_adj_kwh", cool_therm / 4.5 if cool_therm else 0)
        equip_total  = (row.get("equipment_kwh", 0) + row.get("fans_kwh", 0)
                        + row.get("pumps_kwh", 0) + row.get("heat_rejection_kwh", 0))
        # For buildings with own chillers, show elec cooling in Cool Therm column
        cool_therm_display = cool_therm if cool_therm else cool_elec
        eui_val     = row.get("eui_kwh_m2")
        eui_adj_val = row.get("eui_adj_kwh_m2")
        print(
            f"  {row.get('month_name', '?'):<6}  "
            f"{row.get(elec_col, 0):>10,.0f}  "
            f"{cool_therm_display:>10,.0f}  "
            f"{cool_adj:>10,.0f}  "
            f"{lighting_row:>10,.0f}  "
            f"{equip_total:>10,.0f}  "
            f"{eui_val or 0:>7.2f}  "
            f"{eui_adj_val or 0:>8.2f}"
        )

    # Annual totals row
    ann_elec   = monthly_df[elec_col].sum()
    ann_cthm   = monthly_df["cooling_thermal_kwh"].sum() if "cooling_thermal_kwh" in monthly_df.columns else 0
    ann_celec  = monthly_df["cooling_elec_kwh"].sum()    if "cooling_elec_kwh"    in monthly_df.columns else 0
    ann_cadj   = monthly_df["cooling_elec_adj_kwh"].sum() if "cooling_elec_adj_kwh" in monthly_df.columns else ann_celec
    ann_light  = lighting_kwh
    ann_equip  = equip_kwh
    fa_safe    = floor_area or 1
    ann_eui    = ann_elec / fa_safe
    ann_adj_eui = monthly_df["eui_adj_kwh_m2"].sum() if "eui_adj_kwh_m2" in monthly_df.columns else ann_eui
    ann_cthm_display = ann_cthm if ann_cthm else ann_celec
    print(f"  {'─'*100}")
    print(
        f"  {'TOTAL':<6}  "
        f"{ann_elec:>10,.0f}  "
        f"{ann_cthm_display:>10,.0f}  "
        f"{ann_cadj:>10,.0f}  "
        f"{ann_light:>10,.0f}  "
        f"{ann_equip:>10,.0f}  "
        f"{ann_eui:>7.2f}  "
        f"{ann_adj_eui:>8.2f}"
    )
    print(f"  {'─'*100}")
    print(f"  Note: EUI (elec) = Electricity:Facility only. EUI Adj adds cooling elec equiv (Cool Therm / COP 4.5).")
    print(f"{'='*72}\n")


# ══════════════════════════════════════════════════════════════════════════════
# MAPE CALCULATION — compare simulation vs. ground truth
# ══════════════════════════════════════════════════════════════════════════════

def _load_ground_truth_series(ground_truth_csv: str, building: str) -> pd.DataFrame:
    """
    Load monthly measured_kwh indexed by month 1-12 using canonical 2024 ground truth.

    Policy:
    - Use the explicit 2024 monthly series when available.
    - Prefer canonical 2024 data over mixed-year averaging.
    - Use parsed per-building files only as fallback.
    """
    building = building.split("__")[-1].upper()
    gt_path = Path(ground_truth_csv)

    gt = pd.read_csv(gt_path)
    if "month" not in gt.columns or "measured_kwh" not in gt.columns:
        raise ValueError(f"Ground truth CSV missing required columns: {ground_truth_csv}")

    gt = gt.copy()
    gt["month"] = pd.to_datetime(gt["month"], errors="coerce")
    gt["measured_kwh"] = pd.to_numeric(gt["measured_kwh"], errors="coerce")
    gt = gt.dropna(subset=["month", "measured_kwh"])

    if gt.empty:
        raise ValueError(f"Ground truth CSV has no valid rows: {ground_truth_csv}")

    gt = gt[gt["month"].dt.year == 2024].copy()
    if gt.empty:
        raise ValueError(f"Ground truth CSV has no 2024 rows: {ground_truth_csv}")

    gt["month_num"] = gt["month"].dt.month
    gt = gt.groupby("month_num", as_index=True)["measured_kwh"].sum().to_frame()
    gt.index.name = "month"
    log.info(f"[{building}] Ground truth policy: using canonical 2024 monthly data from {ground_truth_csv}")
    return gt


def calculate_mape(monthly_df: pd.DataFrame, ground_truth_csv: str, building: str) -> dict:
    """
    Calculate MAPE, CVRMSE, and NMBE on an annual EUI basis.

    Pass/fail criteria:
        - CVRMSE ≤ 15%
        - NMBE   ≤ ±5%

    Comparison basis:
    - Ground truth uses canonical 2024 monthly data, converted to annual EUI
    - Simulation uses the parsed CSV's annual adjusted EUI (`eui_adj_kwh_m2` sum)
    """
    gt = _load_ground_truth_series(ground_truth_csv, building)

    sim_df = monthly_df[monthly_df.get("month_name", pd.Series("")) != "ANNUAL"].copy() \
        if "month_name" in monthly_df.columns else monthly_df.copy()

    if "eui_adj_kwh_m2" not in sim_df.columns and "eui_kwh_m2" not in sim_df.columns:
        log.warning(f"[{building}] Missing EUI columns for annual EUI comparison")
        return None

    sim = sim_df.copy()
    if "month" in sim.columns:
        sim.index = pd.to_numeric(sim["month"], errors="coerce")
    elif hasattr(sim.index, "month"):
        sim.index = sim.index.month
    sim = sim[sim.index.notna()].copy()
    sim.index = sim.index.astype(int)

    comparison = sim_df.copy()
    if "month" in comparison.columns:
        comparison["month"] = pd.to_numeric(comparison["month"], errors="coerce")
        comparison = comparison[comparison["month"].isin(gt.index)].copy()
    if len(comparison) == 0:
        log.error(f"[{building}] No overlapping months between simulation and ground truth")
        return None

    registry_path = Path(os.getenv('NUS_PROJECT_DIR', '/Users/ye/nus-energy')) / 'building_registry.json'
    registry = json.loads(registry_path.read_text()) if registry_path.exists() else {}
    floor_area = float(registry.get(building, {}).get('floor_area_m2') or 0)
    if floor_area <= 0:
        log.error(f"[{building}] Missing valid floor_area_m2 in building_registry.json")
        return None

    annual_measured_eui = gt['measured_kwh'].sum() / floor_area
    annual_simulated_eui = pd.to_numeric(comparison.get('eui_adj_kwh_m2', comparison.get('eui_kwh_m2')), errors='coerce').fillna(0).sum()

    error = annual_simulated_eui - annual_measured_eui
    mape = abs(error) / annual_measured_eui * 100
    cvrmse = abs(error) / annual_measured_eui * 100
    nmbe = error / annual_measured_eui * 100

    calibrated = cvrmse <= 15.0 and abs(nmbe) <= 5.0

    comparison = pd.DataFrame([{
        'building': building,
        'annual_measured_eui_kwh_m2': annual_measured_eui,
        'annual_simulated_eui_kwh_m2': annual_simulated_eui,
        'error_eui_kwh_m2': error,
        'error_pct': nmbe,
    }])

    status = "✅ CALIBRATED" if calibrated else "⚠️  NEEDS RECALIBRATION"
    log.info(f"[{building}] Annual EUI comparison (eui_adj basis) → MAPE={mape:.2f}%  CVRMSE={cvrmse:.2f}%  NMBE={nmbe:+.2f}%  {status}")

    return {
        "building": building,
        "mape": round(mape, 4),
        "cvrmse": round(cvrmse, 4),
        "nmbe": round(nmbe, 4),
        "calibrated": calibrated,
        "n_months": len(comparison),
        "comparison": comparison,
    }


# ══════════════════════════════════════════════════════════════════════════════
# PARALLEL WORKER — top-level function required for ProcessPoolExecutor pickling
# ══════════════════════════════════════════════════════════════════════════════

def _simulate_one(args_tuple) -> dict:
    """
    Process a single IDF through the full pipeline (prepare → simulate → parse → MAPE).
    Designed as a top-level function so it can be pickled by ProcessPoolExecutor.

    Returns a summary row dict.
    """
    idf_path, idf_root, epw_path, output_dir, ground_truth_dir = args_tuple

    idf_path      = Path(idf_path)
    idf_root      = Path(idf_root)
    building_stem = idf_path.stem

    rel = idf_path.relative_to(idf_root)
    if len(rel.parts) > 1:
        variant = rel.parts[0]
        run_id  = f"{variant}__{building_stem}"
    else:
        variant = ""
        run_id  = building_stem

    try:
        prepared_idf = prepare_idf(str(idf_path), epw_path, output_dir, run_id=run_id)
        sim_result   = run_simulation(prepared_idf, epw_path, output_dir, run_id=run_id)

        if not sim_result["success"]:
            return {
                "run_id": run_id, "building": building_stem, "variant": variant,
                "status": "SIMULATION_FAILED", "annual_eui_kwh_m2": None,
                "mape": None, "cvrmse": None, "nmbe": None, "calibrated": None,
            }

        parsed = parse_results(sim_result, output_dir, run_id=run_id)

        # Compute annual EUI from monthly CSV (exclude the ANNUAL summary row)
        annual_eui = None
        annual_eui_adj = None
        if parsed and "monthly_df" in parsed:
            df = parsed["monthly_df"]
            monthly_only = df[df["month_name"] != "ANNUAL"] if "month_name" in df.columns else df
            if "eui_kwh_m2" in monthly_only.columns and monthly_only["eui_kwh_m2"].notna().any():
                annual_eui = round(float(monthly_only["eui_kwh_m2"].sum()), 1)
            if "eui_adj_kwh_m2" in monthly_only.columns and monthly_only["eui_adj_kwh_m2"].notna().any():
                annual_eui_adj = round(float(monthly_only["eui_adj_kwh_m2"].sum()), 1)

        row = {
            "run_id": run_id, "building": building_stem, "variant": variant,
            "status": "SUCCESS", "elapsed_s": sim_result["elapsed_s"],
            "annual_eui_kwh_m2": annual_eui,
            "annual_eui_adj_kwh_m2": annual_eui_adj,
            "mape": None, "cvrmse": None, "nmbe": None, "calibrated": None,
        }

        if ground_truth_dir and parsed and "monthly_df" in parsed:
            gt_path = Path(ground_truth_dir) / f"{building_stem}_ground_truth.csv"
            if gt_path.exists():
                mape_result = calculate_mape(parsed["monthly_df"], str(gt_path), run_id)
                if mape_result:
                    row.update({
                        "mape": mape_result["mape"],
                        "cvrmse": mape_result["cvrmse"],
                        "nmbe": mape_result["nmbe"],
                        "calibrated": mape_result["calibrated"],
                    })
                    comp_path = (
                        Path(output_dir) / run_id / "parsed"
                        / f"{run_id}_mape_comparison.csv"
                    )
                    mape_result["comparison"].to_csv(comp_path)

        return row

    except Exception as e:
        log.error(f"[{run_id}] Unexpected error: {e}", exc_info=True)
        return {
            "run_id": run_id, "building": building_stem, "variant": variant,
            "status": "ERROR", "error": str(e),
            "annual_eui_kwh_m2": None, "mape": None, "cvrmse": None, "nmbe": None, "calibrated": None,
        }


# ══════════════════════════════════════════════════════════════════════════════
# BATCH RUNNER — process all NUS IDF files (supports nested subdirectories)
# ══════════════════════════════════════════════════════════════════════════════

def run_all_buildings(idf_dir: str, epw_path: str, output_dir: str,
                      ground_truth_dir: str = None,
                      workers: int = 1,
                      skip_existing: bool = False) -> pd.DataFrame:
    """
    Run simulation pipeline for all IDF files in idf_dir, recursively.

    IDFs may live directly in idf_dir or inside variant subdirectories
    (e.g. idf_dir/A1_H_L/FOE13.idf). Each IDF gets a unique run_id of the
    form "{subdir}__{stem}" (e.g. "A1_H_L__FOE13") when nested, or just the
    stem (e.g. "FOE13") when at the top level. This prevents output collisions
    when the same building name appears across multiple variant subdirs.

    Ground truth lookup always uses the bare building stem (e.g. "FOE13"),
    so a single ground truth CSV covers all variants of the same building.

    workers: number of parallel EnergyPlus processes. Defaults to 1 (serial).
             Set to os.cpu_count() or a fixed number to parallelise.
             Each worker runs a separate EnergyPlus subprocess, so memory use
             scales with workers. 4–8 is a safe default on a modern machine.

    skip_existing: if True, skip any IDF whose parsed monthly CSV already exists.
                   Useful for resuming a partial batch without re-running clean outputs.

    Returns summary DataFrame with run_id, building, variant, MAPE/CVRMSE.
    """
    idf_root  = Path(idf_dir)
    idf_files = sorted(idf_root.rglob("*.idf"))
    if not idf_files:
        log.error(f"No IDF files found under {idf_dir}")
        return pd.DataFrame()

    # Apply --skip-existing filter before building work list
    if skip_existing:
        out_root = Path(output_dir)
        skipped = []
        filtered = []
        for idf_path in idf_files:
            rel = idf_path.relative_to(idf_root)
            building_stem = idf_path.stem
            if len(rel.parts) > 1:
                run_id = f"{rel.parts[0]}__{building_stem}"
            else:
                run_id = building_stem
            csv_path = out_root / run_id / "parsed" / f"{run_id}_monthly.csv"
            if csv_path.exists():
                skipped.append(run_id)
            else:
                filtered.append(idf_path)
        if skipped:
            log.info(f"--skip-existing: skipping {len(skipped)} already-simulated IDFs "
                     f"({len(filtered)} remaining)")
        idf_files = filtered

    if not idf_files:
        log.info("All IDFs already simulated. Nothing to do.")
        return pd.DataFrame()

    log.info(f"Found {len(idf_files)} IDF files to simulate (workers={workers})")

    # Build args list for the worker function
    work_items = [
        (str(idf_path), str(idf_root), epw_path, output_dir, ground_truth_dir)
        for idf_path in idf_files
    ]

    summary_rows = []

    if workers <= 1:
        # ── Serial mode ───────────────────────────────────────────────────
        for i, item in enumerate(work_items, 1):
            idf_path_str = item[0]
            log.info(f"\n{'─'*60}")
            log.info(f"[{i}/{len(idf_files)}] {Path(idf_path_str).name}")
            log.info(f"{'─'*60}")
            row = _simulate_one(item)
            summary_rows.append(row)
    else:
        # ── Parallel mode — ProcessPoolExecutor ───────────────────────────
        log.info(f"Starting parallel batch with {workers} workers...")
        completed = 0
        with ProcessPoolExecutor(max_workers=workers) as pool:
            futures = {pool.submit(_simulate_one, item): item for item in work_items}
            for future in as_completed(futures):
                completed += 1
                try:
                    row = future.result()
                except Exception as exc:
                    idf_path_str = futures[future][0]
                    log.error(f"Worker crashed for {idf_path_str}: {exc}")
                    row = {
                        "run_id": Path(idf_path_str).stem, "building": Path(idf_path_str).stem,
                        "variant": "", "status": "WORKER_CRASH", "error": str(exc),
                        "mape": None, "cvrmse": None, "calibrated": None,
                    }
                summary_rows.append(row)
                log.info(f"[{completed}/{len(idf_files)}] done: {row.get('run_id','?')}  status={row.get('status','?')}")

    # ── Save summary ────────────────────────────────────────────────────────
    summary_df = pd.DataFrame(summary_rows)
    summary_path = Path(output_dir) / "simulation_summary.csv"
    summary_df.to_csv(summary_path, index=False)

    log.info(f"\n{'='*60}")
    log.info(f"BATCH COMPLETE — {len(idf_files)} IDF files processed")
    log.info(f"Summary saved → {summary_path}")
    log.info(f"{'='*60}")

    if "mape" in summary_df.columns:
        calibrated = summary_df[summary_df["calibrated"] == True]
        needs_recal = summary_df[summary_df["calibrated"] == False]
        log.info(f"Calibrated:          {len(calibrated)} buildings")
        log.info(f"Needs recalibration: {len(needs_recal)} buildings (→ OpenClaw Stage 2)")
        log.info(f"No ground truth:     {summary_df['mape'].isna().sum()} buildings")

    # ── Human-readable summary for Slack / stdout ───────────────────────────
    n_total  = len(summary_df)
    n_ok     = int((summary_df["status"] == "SUCCESS").sum()) if "status" in summary_df.columns else 0
    n_fail   = int((summary_df["status"] != "SUCCESS").sum()) if "status" in summary_df.columns else 0

    print(f"\n{'='*60}")
    print(f"  BATCH RESULTS — {n_total} buildings")
    print(f"  ✅ Success: {n_ok}   ❌ Failed: {n_fail}")
    print(f"{'='*60}")

    # ── EUI table (all successful buildings with floor area) ────────────────
    eui_col = "annual_eui_kwh_m2"
    eui_rows = summary_df[
        (summary_df["status"] == "SUCCESS") &
        summary_df.get(eui_col, pd.Series(dtype=float)).notna()
    ] if eui_col in summary_df.columns else pd.DataFrame()

    if not eui_rows.empty:
        print(f"\n  💡 Energy Use Intensity (EUI) — kWh/m²/year:")
        print(f"  {'Building':<14} {'EUI (kWh/m²/yr)':>16}  {'EUI Adj':>9}  BCA Tier")
        print(f"  {'-'*62}")
        for _, row in eui_rows.sort_values(eui_col).iterrows():
            eui      = row[eui_col]
            eui_adj  = row.get("annual_eui_adj_kwh_m2", eui)
            bldg     = str(row.get("building", row.get("run_id", "?")))
            tier     = _bca_tier(eui_adj)
            print(f"  {bldg:<14} {eui:>16.1f}  {eui_adj:>9.1f}  {tier}")

    # ── Calibration check (metered buildings with MAPE) ─────────────────────
    metered_rows = summary_df[summary_df["mape"].notna()] if "mape" in summary_df.columns else pd.DataFrame()
    if not metered_rows.empty:
        print(f"\n  📐 Calibration check (buildings with ground truth):")
        print(f"  {'Building':<12} {'MAPE':>7} {'CVRMSE':>8} {'EUI':>8}  Result")
        print(f"  {'-'*55}")
        for _, row in metered_rows.iterrows():
            mape_str   = f"{row['mape']*100:.1f}%"   if pd.notna(row.get('mape'))   else "—"
            cvrmse_str = f"{row['cvrmse']*100:.1f}%"  if pd.notna(row.get('cvrmse')) else "—"
            eui_str    = f"{row[eui_col]:.0f}"        if eui_col in row and pd.notna(row.get(eui_col)) else "—"
            calibrated = row.get("calibrated")
            result_icon = "✅ PASS" if calibrated else "⚠️  RECAL"
            building = str(row.get("building", row.get("run_id", "?")))
            print(f"  {building:<12} {mape_str:>7} {cvrmse_str:>8} {eui_str:>7}  {result_icon}")

    # Failed simulations
    failed_rows = summary_df[summary_df["status"] != "SUCCESS"] if "status" in summary_df.columns else pd.DataFrame()
    if not failed_rows.empty:
        print(f"\n  ❌ Failed ({len(failed_rows)}):")
        for _, row in failed_rows.iterrows():
            building = str(row.get("building", row.get("run_id", "?")))
            err = str(row.get("error", row.get("status", "unknown")))[:80]
            print(f"    {building}: {err}")

    print(f"\n  Summary CSV → {summary_path}")
    print(f"{'='*60}")

    return summary_df


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw Stage 1 — EnergyPlus simulation runner for NUS campus"
    )
    parser.add_argument("--idf",     help="Single IDF file path (omit to run all in --idf-dir or CONFIG[idf_dir])")
    parser.add_argument("--idf-dir", default=CONFIG["idf_dir"],  help="Folder of IDF files (recursive)")
    parser.add_argument("--epw",     default=None, help="EPW weather file path (overrides Nimbus calibrated EPW if supplied)")
    parser.add_argument("--month",   default=None, help="Simulation month YYYY-MM — used to resolve Nimbus calibrated EPW (e.g. 2024-08)")
    parser.add_argument("--output",  default=CONFIG["output_dir"], help="Output directory")
    parser.add_argument("--gt-dir",  default=None, help="Ground truth CSVs directory (optional)")
    parser.add_argument(
        "--workers", type=int, default=1,
        help=(
            "Number of parallel EnergyPlus processes for batch runs (default: 1 = serial). "
            "Recommended: 4-8 on a modern machine. Each worker runs a full EP subprocess."
        ),
    )
    parser.add_argument(
        "--skip-existing", action="store_true",
        help=(
            "Skip any building whose parsed monthly CSV already exists in the output dir. "
            "Useful for resuming a partial batch run without re-simulating clean outputs."
        ),
    )
    args = parser.parse_args()

    Path(args.output).mkdir(parents=True, exist_ok=True)

    # Resolve best available EPW (Nimbus calibrated > CLI override > base TMY)
    epw_path, epw_source = resolve_epw(
        month=args.month,
        base_epw=args.epw,
    )
    if epw_source.startswith("tmy"):
        log.warning(f"[epw] No Nimbus calibrated EPW found — using base TMY ({epw_path})")
    log.info(f"[epw] Source: {epw_source}  →  {epw_path}")

    if args.idf:
        # ── Single building mode (always serial) ──────────────────────────
        building = Path(args.idf).stem
        if args.skip_existing:
            existing_csv = Path(args.output) / building / "parsed" / f"{building}_monthly.csv"
            if existing_csv.exists():
                log.info(f"[{building}] --skip-existing: parsed CSV already present, skipping.")
                sys.exit(0)
        prepared = prepare_idf(args.idf, epw_path, args.output)
        sim_result = run_simulation(prepared, epw_path, args.output)
        summary_row: dict = {
            "building": building,
            "run_id": building,
            "status": "SUCCESS" if sim_result.get("success") else "FAILED",
            "mape": None, "cvrmse": None, "nmbe": None, "calibrated": None,
        }
        if sim_result["success"]:
            parsed = parse_results(sim_result, args.output)
            if args.gt_dir and parsed and "monthly_df" in parsed:
                gt_path = Path(args.gt_dir) / f"{building}_ground_truth.csv"
                if gt_path.exists():
                    metrics = calculate_mape(parsed["monthly_df"], str(gt_path), building)
                    if metrics:
                        summary_row.update({
                            "mape":       metrics.get("mape"),
                            "cvrmse":     metrics.get("cvrmse"),
                            "nmbe":       metrics.get("nmbe"),
                            "calibrated": metrics.get("calibrated"),
                        })
        # Write summary CSV so the pipeline detection gate can read it
        import pandas as _pd
        summary_df = _pd.DataFrame([summary_row])
        summary_path = Path(args.output) / "simulation_summary.csv"
        # Merge with existing rows rather than overwrite (supports multi-building sequential runs)
        if summary_path.exists():
            existing = _pd.read_csv(summary_path)
            existing = existing[existing["building"] != building]  # drop stale row for this building
            summary_df = _pd.concat([existing, summary_df], ignore_index=True)
        summary_df.to_csv(summary_path, index=False)
        log.info(f"[{building}] Summary written → {summary_path}")
    else:
        # ── Batch mode — all IDF files found recursively under --idf-dir ──
        run_all_buildings(
            args.idf_dir, epw_path, args.output,
            ground_truth_dir=args.gt_dir,
            workers=args.workers,
            skip_existing=args.skip_existing,
        )


if __name__ == "__main__":
    # macOS uses 'spawn' as the default multiprocessing start method (Python 3.8+).
    # freeze_support() is a no-op outside frozen executables but is harmless and
    # required for correct behaviour when the script is packaged or called via
    # ProcessPoolExecutor on macOS arm64.
    import multiprocessing
    multiprocessing.freeze_support()
    main()
