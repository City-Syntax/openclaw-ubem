"""
carbon_scenarios.py — Compass's carbon scenario engine
Reads a building's parsed monthly CSV + IDF and produces ranked carbon reduction scenarios.

Run (estimate-only, fast):
  python3 carbon_scenarios.py --building BIZ8 --outputs /Users/ye/nus-energy/outputs --idfs /Users/ye/nus-energy/idfs

Run (with counterfactual EnergyPlus simulations for patchable interventions):
  python3 carbon_scenarios.py --building BIZ8 --outputs /Users/ye/nus-energy/outputs --idfs /Users/ye/nus-energy/idfs --simulate

Output:
  outputs/{BUILDING}/carbon/{BUILDING}_carbon_scenarios.json
"""

import argparse
import importlib.util
import json
import os
import re
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd


def _load_preflight_registry():
    """Dynamically import preflight_registry from generate_registry.py."""
    spec = importlib.util.spec_from_file_location(
        "generate_registry", GENERATE_REGISTRY_SCRIPT
    )
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)
        return mod.preflight_registry
    except Exception as e:
        print(f"  ⚠ Could not load generate_registry.py: {e} — registry pre-flight skipped")
        return None

# ─── Constants ────────────────────────────────────────────────────────────────

GRID_FACTOR       = 0.4168   # kgCO2e/kWh (EMA Singapore 2023)
INTERACTION_DISC  = 0.15     # 15% discount when combining multiple demand-side levers
BCA_BENCHMARKS_IHL      = [  # (threshold_kwh_m2, label) — BCA Green Mark 2021 Pathway 1, IHL thresholds
    (90,  "Super Low Energy"),
    (120, "Platinum"),
    (130, "GoldPLUS"),
]
BCA_BENCHMARKS_HOSPITAL = [  # (threshold_kwh_m2, label) — BCA Green Mark 2021 Pathway 1, Hospital thresholds
    (300, "Super Low Energy"),
    (340, "Platinum"),
    (375, "GoldPLUS"),
]

# Counterfactual simulation support
# All interventions are simulated where IDF patching is feasible.
# Exception: Intervention 5 (HVAC system swap IdealLoads → VRF/chiller) requires a full
# HVAC model rebuild and cannot be done via parameter patch — stays as literature estimate.
# Intervention 1 (cooling setpoint) is patched for counterfactual simulation ONLY —
# the temporary IDF is never persisted; this does not violate the agent setpoint-freeze rule.
SIMULATABLE_INTERVENTIONS = {1, 2, 3, 4, 6, 7}
# IV6 (PV): patched IDF injects Output:Meter,ElectricityNet:Facility,Monthly so that
# net facility consumption (facility minus PV generation) is metered. The parser reads
# ElectricityNet:Facility (not Electricity:Facility) for IV6 runs.

PROJECT_ROOT           = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy")).resolve()
EP_BINARY              = "/Applications/EnergyPlus-23-1-0/energyplus"
EPW_DEFAULT            = str(PROJECT_ROOT / "weather" / "SGP_Singapore.486980_IWEC.epw")
CALIBRATED_EPW_DIR     = str(PROJECT_ROOT / "weather" / "calibrated")
INTERVENTION_IDFS_DIR  = str(PROJECT_ROOT / "intervention_idfs")
GENERATE_REGISTRY_SCRIPT = (
    "/Users/ye/.openclaw/workspace-simulationagent/skills/nus-registry/scripts/generate_registry.py"
)


def resolve_epw(month: str = None, override: str = None) -> tuple[str, str]:
    """
    Return the best available EPW path for counterfactual simulations.
    Priority: Nimbus calibrated EPW for month > CLI override > base TMY default.
    Returns (epw_path, source_label).
    """
    if month:
        candidate = Path(CALIBRATED_EPW_DIR) / f"{month}_site_calibrated.epw"
        if candidate.exists():
            return str(candidate), f"calibrated:{month}"
    if override and override != EPW_DEFAULT:
        return override, "tmy:override"
    return EPW_DEFAULT, "tmy:default"

# Counterfactual target values
IV1_SETPOINT_DELTA    = 1.0   # °C increase (capped at +1 per NUS policy)
IV2_TARGET_LPD        = 5.5   # W/m²  (LED upgrade target)
IV3_DIMMING_FRACTION  = 0.70  # retain 70% of LPD after dimming controls (30% saving)
IV4_SHGC_MULTIPLIER   = 0.60  # reduce SHGC by 40% (dynamic blind/shading effect)
IV7_ROOF_ABSORPTANCE  = 0.25  # cool paint roof
IV7_WALL_ABSORPTANCE  = 0.40  # cool paint wall

# Reduction ranges (min, max) as fraction of total building electricity — used when
# simulation is unavailable.
INTERVENTION_REDUCTIONS = {
    1: (0.03, 0.06),   # Cooling setpoint +1°C (per degree; take midpoint × delta)
    2: (0.08, 0.12),   # Lighting LPD 9→6 W/m²
    3: (0.04, 0.07),   # Dimming controls
    4: (0.03, 0.05),   # Shading / blind control
    5: (0.15, 0.25),   # HVAC upgrade (real COP)
    6: (0.20, 0.35),   # PV installation (offset)
    7: (0.02, 0.04),   # Cool painting
}

CAPEX_TIERS = {
    1: "zero",     # Cooling setpoint — BAS edit, no capex
    2: "medium",   # Lighting upgrade — moderate capex (SGD 35/m²)
    3: "high",     # Dimming controls — higher capex (SGD 60/m²), deep scenario only
    4: "medium",   # Shading upgrades — moderate capex (SGD 60/m²)
    5: "high",     # HVAC upgrade — major capex (SGD 280/m²)
    6: "high",     # PV installation — major capex (SGD 50/m²)
    7: "zero",     # Cool painting — near-zero capex (SGD 4/m²)
}

PATCH_COMPLEXITY = {
    1: "param_patch",
    2: "param_patch",
    3: "new_object",
    4: "new_object",
    5: "system_swap",
    6: "new_object",
    7: "param_patch",
}

INTERVENTION_IDF_OBJECTS = {
    1: ["Zone_nCooling_SP_Sch (×N zones)"],
    2: ["Lights → LightingPowerDensity (×N zones)"],
    3: ["Daylighting:Controls (inject per zone)", "Lights → FractionReplaceable"],
    4: ["WindowProperty:ShadingControl (inject per window)"],
    5: ["ZoneHVAC:IdealLoadsAirSystem → replace with VRF/chiller objects"],
    6: ["Generator:Photovoltaic (inject)", "ElectricLoadCenter:Distribution (inject)"],
    7: ["Material (outer roof layer) → SolarAbsorptance", "Material (outer wall layer) → SolarAbsorptance"],
}

INTERVENTION_NAMES = {
    1: "Cooling setpoint increase",
    2: "Lighting upgrade",
    3: "Dimming control system",
    4: "Shading upgrades",
    5: "HVAC system upgrade",
    6: "PV installation",
    7: "Cool painting",
}

SCENARIO_DEFINITIONS = {
    # 🟢 Zero Cost — zero capex, immediate deployment, no procurement or construction works
    #               IV1 (setpoint) only — BAS schedule edit performed in-house
    "shallow": {"interventions": [1],                "label": "Zero Cost",    "tier": "🟢"},
    # 🟡 Medium Cost — moderate capex, operational upgrades + envelope retrofit
    "medium":  {"interventions": [1, 2, 4, 7],       "label": "Medium Cost",  "tier": "🟡"},
    # 🔴 High Cost — major capex, full infrastructure: HVAC + envelope + PV + controls
    "deep":    {"interventions": [1, 2, 3, 4, 5, 6, 7], "label": "High Cost", "tier": "🔴"},
}

# ─── Singapore Market Cost Model (SGD/m² GFA, 2024–2025 rates) ──────────────
#
# ALL interventions are expressed as SGD per m² GFA so that scenarios can
# report a single comparable cost intensity (SGD/m² GFA) alongside tCO2e saved.
#
# Sources / benchmarks used:
#   BCA Green Buildings Innovation Cluster (GBIC) retrofit cost data
#   NEA / EDB energy efficiency grant indicatives (ETSS, ENER+ schemes)
#   BCA Building Cost Information Service (BCIS) Singapore 2024
#   Industry quotes aggregated from local M&E contractors (Bintai Kinden,
#   Rotol, Boustead Projects), LED distributors (Philips, Havells), and
#   PV EPCs (Sunseap, Cleantech Solar, Sembcorp Solar Singapore)
#
# Conversion ratios used to translate non-GFA units to SGD/m² GFA:
#   IV3 (dimming)  : assumed 1 zone per 75 m² GFA (NUS institutional avg)
#   IV4 (shading)  : assumed 1 window per 8 m² GFA (glazing ratio ~0.40 facade)
#   IV6 (PV)       : roof area = GFA × 0.35; panel coverage = 70%; 150 Wp/m²;
#                    SGD 1,350/kWp  →  SGD 1,350 × 0.35 × 0.70 × 0.150 ≈ SGD 49.6/m² GFA
#
# Each entry:
#   cost_per_m2_mid  — SGD/m² GFA mid-point
#   cost_per_m2_low  — SGD/m² GFA lower bound (–20 to –25%)
#   cost_per_m2_high — SGD/m² GFA upper bound (+20 to +30%)
#   ref_unit_cost    — original unit rate for reference / audit
#   ref_unit         — original unit for reference
#   unit_desc        — human-readable description
#   min_cost         — absolute floor for small buildings (SGD total)
#   conversion_note  — explains how ref_unit_cost → SGD/m² GFA
#   notes            — sources and scope assumptions
#   payback_years    — typical simple payback at SGD 0.32/kWh (SP commercial 2024)

SINGAPORE_COST_MODEL = {
    1: {  # Cooling setpoint increase — BAS/IBMS reprogramming only
        "cost_per_m2_mid":  0,
        "cost_per_m2_low":  0,
        "cost_per_m2_high": 0,
        "ref_unit_cost":    0,
        "ref_unit":         "lump_sum",
        "unit_desc":        "SGD/m² GFA — zero (BAS schedule edit, in-house)",
        "min_cost":         0,
        "conversion_note":  "No cost — NUS facilities team performs setpoint change in BAS/IBMS.",
        "notes": (
            "Setpoint change is a BAS/IBMS schedule edit. "
            "Assumes NUS facilities team performs in-house. Zero external capex."
        ),
        "payback_years": None,  # zero capex — immediate; no payback calculation needed
    },
    2: {  # LED lighting retrofit — T8 fluorescent → LED panel, supply + install
        "cost_per_m2_mid":  35,
        "cost_per_m2_low":  25,
        "cost_per_m2_high": 45,
        "ref_unit_cost":    35,
        "ref_unit":         "SGD/m² GFA",
        "unit_desc":        "SGD/m² GFA (LED panel supply & install, incl. driver & labour)",
        "min_cost":         15_000,
        "conversion_note":  "Direct GFA rate from BCA GBIC benchmarks — no conversion needed.",
        "notes": (
            "BCA GBIC LED retrofit cost benchmarks SG 2023–24: "
            "SGD 25–45/m² GFA for office/educational buildings. "
            "Includes T8 removal, LED panel/tube supply, driver, cabling, and "
            "licensed electrical contractor (BCA reg. EW). "
            "NEA ENER+ grant may offset 50% of incremental cost for qualifying buildings."
        ),
        "payback_years": None,  # computed from building-specific capex and simulated savings
    },
    3: {  # Dimming controls — DALI-2 drivers + daylight/occupancy sensors
        # Conversion: SGD 4,500/zone ÷ 75 m²/zone = SGD 60/m² GFA
        "cost_per_m2_mid":  60,
        "cost_per_m2_low":  45,
        "cost_per_m2_high": 78,
        "ref_unit_cost":    4_500,
        "ref_unit":         "SGD/zone",
        "unit_desc":        "SGD/m² GFA (DALI-2 driver + sensors + commissioning; 1 zone per 75 m²)",
        "min_cost":         20_000,
        "conversion_note":  "SGD 4,500/zone ÷ 75 m² GFA/zone (NUS institutional avg) = SGD 60/m² GFA.",
        "notes": (
            "DALI-2 driver retrofit SGD 150–300/luminaire; sensor (Helvar/Zumtobel) "
            "SGD 200–350 each; commissioning and BAS integration SGD 800–1,500/zone. "
            "Blended zone cost SGD 3,500–5,500/zone; mid-point SGD 4,500/zone. "
            "Assumes 1 zone per 75 m² GFA, 20 luminaires per zone, 2 sensors per zone."
        ),
        "payback_years": None,  # computed from building-specific capex and simulated savings
    },
    4: {  # Shading / dynamic blind control — motorised blinds + BAS-linked solar sensor
        # Conversion: SGD 480/window ÷ 8 m² GFA/window = SGD 60/m² GFA
        "cost_per_m2_mid":  60,
        "cost_per_m2_low":  45,
        "cost_per_m2_high": 78,
        "ref_unit_cost":    480,
        "ref_unit":         "SGD/window",
        "unit_desc":        "SGD/m² GFA (motorised blind + BAS controller; 1 window per 8 m² GFA)",
        "min_cost":         25_000,
        "conversion_note":  "SGD 480/window ÷ 8 m² GFA/window (glazing ratio ~0.40 facade) = SGD 60/m² GFA.",
        "notes": (
            "Motorised roller blind (Somfy/Lutron): SGD 250–400/window. "
            "BAS-linked solar sensor controller (KNX/BACnet): SGD 50–100/point. "
            "Install and commissioning: SGD 80–150/window. Total mid-point SGD 480/window. "
            "Window density assumed 1 per 8 m² GFA based on NUS institutional facade survey. "
            "External fixed fins excluded (higher cost)."
        ),
        "payback_years": None,  # computed from building-specific capex and simulated savings
    },
    5: {  # HVAC system upgrade — chiller plant or full VRF replacement
        "cost_per_m2_mid":  280,
        "cost_per_m2_low":  200,
        "cost_per_m2_high": 360,
        "ref_unit_cost":    280,
        "ref_unit":         "SGD/m² GFA",
        "unit_desc":        "SGD/m² GFA (chiller/VRF supply, install & commissioning)",
        "min_cost":         500_000,
        "conversion_note":  "Direct GFA rate from BCA BCIS Singapore 2024 — no conversion needed.",
        "notes": (
            "BCA BCIS Singapore 2024: full HVAC system replacement (DX VRF or "
            "chiller + AHU) SGD 200–360/m² GFA for institutional buildings. "
            "Includes equipment, refrigerant piping, BAS integration, commissioning, "
            "and one-year defects liability. "
            "EDB Energy Efficiency Fund (E2F) grant may offset 50% of incremental "
            "capex for qualifying SMEs/public-sector entities."
        ),
        "payback_years": None,  # computed from building-specific capex and simulated savings
    },
    6: {  # PV installation — rooftop grid-tied EPC turnkey
        # Conversion: SGD 1,350/kWp × (GFA × 0.35 roof ratio × 0.70 coverage × 0.150 kWp/m²)
        #           = SGD 1,350 × 0.03675 kWp/m² GFA ≈ SGD 49.6/m² GFA
        "cost_per_m2_mid":  50,
        "cost_per_m2_low":  40,
        "cost_per_m2_high": 60,
        "ref_unit_cost":    1_350,
        "ref_unit":         "SGD/kWp",
        "unit_desc":        "SGD/m² GFA (EPC turnkey PV; roof=GFA×0.35, coverage 70%, 150 Wp/m²)",
        "min_cost":         80_000,
        "conversion_note": (
            "SGD 1,350/kWp × 0.150 kWp/m² panel × 0.70 coverage × 0.35 roof/GFA ratio "
            "= SGD 49.6/m² GFA ≈ SGD 50/m² GFA."
        ),
        "notes": (
            "SolarPVExchange / SolarQuotes SG 2024: commercial rooftop EPC SGD 1,200–1,550/kWp. "
            "Includes mono PERC/TOPCon panels, string inverter, aluminium racking, AC/DC cabling, "
            "SP Group metering application, and BCA structural endorsement. "
            "NEA SolarNova / EDB may provide additional incentive for NUS campus."
        ),
        "payback_years": None,  # computed from building-specific capex and simulated savings
    },
    7: {  # Cool painting — elastomeric cool-roof + cool-wall coating
        # Conversion: SGD 8/m² painted × 0.50 area ratio (roof+wall proxy) = SGD 4/m² GFA
        "cost_per_m2_mid":  4,
        "cost_per_m2_low":  3,
        "cost_per_m2_high": 6,
        "ref_unit_cost":    8,
        "ref_unit":         "SGD/m² painted surface",
        "unit_desc":        "SGD/m² GFA (cool-roof + cool-wall coating; painted area = GFA × 0.50)",
        "min_cost":         8_000,
        "conversion_note":  "SGD 8/m² painted × 0.50 (roof ≈ 30% GFA + walls ≈ 20% GFA) = SGD 4/m² GFA.",
        "notes": (
            "Nippon Paint CoolTect / Jotun Jotashield CoolSun: SGD 4–7/m² supply. "
            "Application (rope access / platform): SGD 2–4/m². "
            "Total SGD 6–11/m² painted surface; mid-point SGD 8/m² painted. "
            "Recoating required every 8–12 years."
        ),
        "payback_years": None,  # computed from building-specific capex and simulated savings
    },
}


def compute_intervention_capex(iv_id: int, fp: dict, floor_area_m2=None) -> dict:
    """
    Compute Singapore-market capex estimate for a single intervention.

    All costs are expressed in SGD/m² GFA so interventions and scenarios are
    directly comparable on a cost-intensity basis.

    Returns a dict with:
        cost_per_m2_gfa      — mid-point SGD/m² GFA
        cost_per_m2_gfa_low  — lower bound SGD/m² GFA
        cost_per_m2_gfa_high — upper bound SGD/m² GFA
        capex_sgd            — total SGD (mid), or 0 if floor area unknown
        capex_sgd_low        — total SGD (low), or 0 if floor area unknown
        capex_sgd_high       — total SGD (high), or 0 if floor area unknown
        floor_area_m2        — GFA used for total calculation (None if unknown)
        unit_desc            — human-readable description
        conversion_note      — how the per-m² GFA rate was derived
        ref_unit_cost        — original reference unit rate
        ref_unit             — original reference unit
        notes                — source and scope notes
        payback_years        — typical simple payback
        data_quality         — "floor_area_known" | "floor_area_estimated"
    """
    model = SINGAPORE_COST_MODEL[iv_id]

    mid_m2 = model["cost_per_m2_mid"]
    lo_m2  = model["cost_per_m2_low"]
    hi_m2  = model["cost_per_m2_high"]

    if floor_area_m2:
        capex_mid  = max(model["min_cost"], mid_m2 * floor_area_m2)
        capex_low  = max(model["min_cost"], lo_m2  * floor_area_m2)
        capex_high = hi_m2 * floor_area_m2
        quality    = "floor_area_known"
    else:
        # No floor area — totals unavailable; return min_cost as placeholder
        capex_mid = capex_low = capex_high = model["min_cost"]
        quality   = "floor_area_estimated"

    return {
        "cost_per_m2_gfa":       mid_m2,
        "cost_per_m2_gfa_low":   lo_m2,
        "cost_per_m2_gfa_high":  hi_m2,
        "capex_sgd":             round(capex_mid,  0),
        "capex_sgd_low":         round(capex_low,  0),
        "capex_sgd_high":        round(capex_high, 0),
        "floor_area_m2":         floor_area_m2,
        "unit_desc":             model["unit_desc"],
        "conversion_note":       model["conversion_note"],
        "ref_unit_cost":         model["ref_unit_cost"],
        "ref_unit":              model["ref_unit"],
        "notes":                 model["notes"],
        "payback_years":         model["payback_years"],
        "data_quality":          quality,
    }


# ─── IDF Fingerprinting ───────────────────────────────────────────────────────

def extract_fingerprint(idf_path: Path) -> dict:
    """Extract the 8 intervention parameters from an IDF file."""
    content = idf_path.read_text(errors="ignore")
    fp = {}

    # 1. Cooling setpoints
    # Pattern A: annotated source IDF — `25;   !- CoolingSetpoint`
    sp_vals = re.findall(r'(\d+(?:\.\d+)?)\s*;\s*!-\s*CoolingSetpoint', content)
    # Pattern B: prepared/plain IDF — Schedule:Constant block with Zone_<id>Cooling_SP_Sch name
    #   Zone_\w+ covers purely numeric IDs (Zone_0, Zone_10) and any future alpha suffixes.
    #   Matches the `Hourly Value` comment style used by the IDF prep pipeline.
    if not sp_vals:
        sp_vals = re.findall(
            r'Schedule:Constant,\s*\n\s*Zone_\w+Cooling_SP_Sch[^\n]*\n[^\n]*\n\s*(\d+(?:\.\d+)?)\s*;\s*!-\s*Hourly Value',
            content,
        )
    # Pattern C: prepared IDF without Schedule:Constant header in the matched region
    if not sp_vals:
        sp_vals = re.findall(
            r'Zone_\w+Cooling_SP_Sch[^\n]*\n[^\n]*Temperature[^\n]*\n\s*(\d+(?:\.\d+)?)\s*;\s*!-\s*Hourly Value',
            content,
        )
    sp_vals_f = [float(v) for v in sp_vals]
    fp["cooling_setpoint_c"]         = sp_vals_f[0] if sp_vals_f else None
    fp["cooling_setpoint_uniform"]   = len(set(sp_vals_f)) == 1 if sp_vals_f else True
    fp["cooling_setpoint_zones"]     = len(sp_vals_f)

    # 2. Lighting LPD
    # Matches annotated IDFs:  `9.0,   !- LightingPowerDensity`
    # and plain EnergyPlus IDFs: `9,   !- Watts per Zone Floor Area` inside a Lights block
    lpd_vals = re.findall(r'(\d+(?:\.\d+)?)\s*,\s*!-\s*LightingPowerDensity', content)
    if not lpd_vals:
        # Plain IDF: look for the Watts per Zone Floor Area field inside Lights blocks
        # Use a lookahead/lookbehind context anchored to Lights blocks
        lights_blocks = re.findall(
            r'Lights,[\s\S]*?(?=\n\S|\Z)',
            content,
            re.MULTILINE,
        )
        for block in lights_blocks:
            m = re.search(r'([\d.]+)\s*,\s*!-\s*Watts per Zone Floor Area', block)
            if m and float(m.group(1)) > 0:
                lpd_vals.append(m.group(1))
    lpd_f = [float(v) for v in lpd_vals]
    fp["lighting_lpd_wm2"]       = lpd_f[0] if lpd_f else None
    fp["lighting_lpd_uniform"]   = len(set(lpd_f)) == 1 if lpd_f else True
    fp["lighting_zones"]         = len(lpd_f)

    # EPD for context
    # Annotated: `12.0,   !- EquipmentPowerDensity`
    # Plain IDF: `12.5,   !- Watts per Zone Floor Area` inside ElectricEquipment blocks
    epd_vals = re.findall(r'(\d+(?:\.\d+)?)\s*,\s*!-\s*EquipmentPowerDensity', content)
    if not epd_vals:
        equip_blocks = re.findall(
            r'ElectricEquipment,[\s\S]*?(?=\n\S|\Z)',
            content,
            re.MULTILINE,
        )
        for block in equip_blocks:
            m = re.search(r'([\d.]+)\s*,\s*!-\s*Watts per Zone Floor Area', block)
            if m and float(m.group(1)) > 0:
                epd_vals.append(m.group(1))
                break
    fp["equipment_epd_wm2"] = float(epd_vals[0]) if epd_vals else None

    # 3. Daylighting controls
    fp["has_daylighting_controls"] = "Daylighting:Controls" in content and \
                                      content.count("Daylighting:Controls") > 1

    # Glazing VT and SHGC
    shgc = re.findall(r'([\d.]+)\s*,\s*!-\s*@.*?-SolarHeatGainCoefficient@', content)
    vt   = re.findall(r'([\d.]+)\s*;.*?VisibleTransmittance', content)
    u_val = re.findall(r'([\d.]+)\s*,\s*!-\s*@.*?-UValue@', content)
    fp["glazing_shgc"]    = float(shgc[0]) if shgc else None
    fp["glazing_vt"]      = float(vt[0])   if vt   else None
    fp["glazing_u_value"] = float(u_val[0]) if u_val else None

    # 4. Shading control
    fp["has_shading_control"] = "WindowProperty:ShadingControl" in content

    # Shading surfaces (context)
    fp["shading_surfaces"] = content.count("Shading:Building:Detailed")

    # 5. HVAC type
    if "ZoneHVAC:IdealLoadsAirSystem" in content:
        fp["hvac_type"] = "IdealLoadsAirSystem"
    elif "AirLoopHVAC" in content:
        fp["hvac_type"] = "AirLoopHVAC"
    elif "ZoneHVAC:VRF" in content or "AirConditioner:VariableRefrigerantFlow" in content:
        fp["hvac_type"] = "VRF"
    else:
        fp["hvac_type"] = "unknown"

    # Sizing:Zone supply temp
    supply_temps = re.findall(r'(\d+(?:\.\d+)?)\s*,\s*!-\s*Zone Cooling Design Supply Air Temperature', content)
    fp["cooling_supply_air_temp_c"] = float(supply_temps[0]) if supply_temps else None

    # 6. PV
    fp["has_pv"] = "Generator:Photovoltaic" in content

    # 7. Solar absorptance
    # Annotated IDF: `0.7,   !- @Zone_0-SolarAbsorptance@`
    # Plain IDF:     `0.7,   !- Solar Absorptance`  (appears on Material objects)
    absorb_vals = re.findall(r'([\d.]+)\s*,\s*!-\s*@.*?Solar\s*Absorptance', content)
    if not absorb_vals:
        absorb_vals = re.findall(r'(0\.\d+)\s*,\s*!-\s*[^!]*[Ss]olar[Aa]bsorptance', content)
    if not absorb_vals:
        # Plain EnergyPlus: field name is just "Solar Absorptance" (no exclamation annotation)
        absorb_vals = re.findall(r'([\d.]+)\s*,\s*!-\s*Solar Absorptance', content)
    fp["solar_absorptance"] = float(absorb_vals[0]) if absorb_vals else None

    # Roof construction layers (for context)
    roof_layers = re.findall(r'((?:XPS|Concrete|Vaporpermeable|Acoustic)[^\n]*Exterior Roof[^\n]*)', content)
    fp["roof_outer_layer"] = roof_layers[0].strip() if roof_layers else None

    # Wall insulation check
    mw_t = re.findall(r'(\d+\.\d+)\s*,\s*!-\s*@.*?Mineral Wool-Thickness', content)
    mw_k = re.findall(r'(\d+\.\d+)\s*,\s*!-\s*@.*?Mineral Wool-Conductivity', content)
    if mw_t and mw_k:
        r_val = float(mw_t[0]) / float(mw_k[0])
        fp["wall_insulation_r_value"] = round(r_val, 3)
    else:
        fp["wall_insulation_r_value"] = None

    # 8. Vegetation
    fp["has_vegetation"] = any(k in content for k in [
        "Material:RoofVegetation", "GreenRoof", "RoofVegetation", "EcoRoof"
    ])

    # Infiltration ACH
    ach_vals = re.findall(r'(\d+(?:\.\d+)?)\s*,\s*!-\s*@Zone_\d+-InfiltrationAch@', content)
    fp["infiltration_ach"] = float(ach_vals[0]) if ach_vals else None

    # Zone count
    zones = re.findall(r'^Zone,', content, re.MULTILINE)
    fp["zones"] = len(zones)

    # Fenestration count
    fp["fenestration_surfaces"] = content.count("FenestrationSurface:Detailed")

    return fp


# ─── Counterfactual Simulation ─────────────────────────────────────────────────

def patch_idf_for_intervention(content, iv_id, fp):
    """
    Apply an intervention's IDF changes to IDF content for counterfactual simulation.
    Returns patched content string, or None if not patchable / no change needed.

    NOTE: IV1 patches the setpoint in a *temporary* IDF copy for simulation only.
    The patched file is never persisted to the source IDF tree. This does not violate
    the agent setpoint-freeze rule — the source IDF is never touched.

    Handles two IDF comment styles:
      Annotated source IDFs:  `5,   !- LightingPowerDensity [W/m2]`
                              `0.7, !- @...-SolarAbsorptance@`
      Prepared/plain IDFs:    `5,   !- Watts per Zone Floor Area`
                              `0.7, !- Solar Absorptance`
    """
    if iv_id not in SIMULATABLE_INTERVENTIONS:
        return None

    if iv_id == 1:
        # +1 °C setpoint increase — temporary counterfactual only, never persisted
        sp = fp.get("cooling_setpoint_c")
        if sp is None:
            return None
        target = round(sp + IV1_SETPOINT_DELTA, 1)
        patched = content

        # Pattern A: annotated source IDF — `25;   !- CoolingSetpoint`
        patched = re.sub(
            r'([\d.]+)(\s*;\s*!-\s*CoolingSetpoint)',
            lambda m: f"{target}{m.group(2)}",
            patched,
        )
        if patched != content:
            return patched

        # Pattern B: prepared / plain IDF — Schedule:Constant block with Zone_<id>Cooling_SP_Sch
        # Structure (with leading whitespace):
        #   Zone_0Cooling_SP_Sch,     !- Name
        #   Temperature,              !- Schedule Type Limits Name
        #   25;                       !- Hourly Value
        # Zone_\w+ covers numeric IDs and any future alpha-suffix zone names.
        # The inner sub replaces the numeric value immediately before `; !- Hourly Value`.
        def _replace_cooling_sp(m):
            block = m.group(0)
            return re.sub(
                r'([\d.]+)(\s*;\s*!-\s*Hourly Value)',
                lambda mv: f"{target}{mv.group(2)}",
                block,
                count=1,
            )
        patched = re.sub(
            r'Zone_\w+Cooling_SP_Sch[^\n]*\n[^\n]*Temperature[^\n]*\n[^\n]*[\d.]+\s*;\s*!-\s*Hourly Value',
            _replace_cooling_sp,
            patched,
        )

        return patched if patched != content else None

    elif iv_id == 2:
        current_lpd = fp.get("lighting_lpd_wm2")
        if current_lpd is not None and current_lpd <= IV2_TARGET_LPD:
            return None  # already at or below target
        patched = content
        patched = re.sub(
            r'(\d+(?:\.\d+)?)\s*,(\s*!-\s*LightingPowerDensity)',
            lambda m: f"{IV2_TARGET_LPD} ,{m.group(2)}",
            patched,
        )
        patched = re.sub(
            r'(\d+(?:\.\d+)?),(\s+!-\s+Watts per Zone Floor Area)',
            lambda m: f"{IV2_TARGET_LPD},{m.group(2)}",
            patched,
        )
        return patched if patched != content else None

    elif iv_id == 3:
        # Dimming controls — reduce LPD by 30% (multiply by IV3_DIMMING_FRACTION)
        # Simulated as a direct LPD reduction (conservative proxy for dimming effect)
        current_lpd = fp.get("lighting_lpd_wm2")
        if current_lpd is None:
            return None
        target_lpd = round(current_lpd * IV3_DIMMING_FRACTION, 2)
        patched = content
        patched = re.sub(
            r'(\d+(?:\.\d+)?)\s*,(\s*!-\s*LightingPowerDensity)',
            lambda m: f"{target_lpd} ,{m.group(2)}",
            patched,
        )
        patched = re.sub(
            r'(\d+(?:\.\d+)?),(\s+!-\s+Watts per Zone Floor Area)',
            lambda m: f"{target_lpd},{m.group(2)}",
            patched,
        )
        return patched if patched != content else None

    elif iv_id == 4:
        # Shading — reduce SHGC by 40% (dynamic blind effect proxy)
        shgc = fp.get("glazing_shgc")
        if shgc is None:
            return None
        target_shgc = round(shgc * IV4_SHGC_MULTIPLIER, 3)
        patched = content
        # Pattern 1: @-annotated `0.35, !- @...-SolarHeatGainCoefficient@`
        patched = re.sub(
            r'([\d.]+)\s*,(\s*!-\s*@.*?SolarHeatGainCoefficient)',
            lambda m: f"{target_shgc} ,{m.group(2)}",
            patched,
        )
        # Pattern 2: plain `0.35,   !- Solar Heat Gain Coefficient`
        patched = re.sub(
            r'([\d.]+),(\s+!-\s+Solar Heat Gain Coefficient)',
            lambda m: f"{target_shgc},{m.group(2)}",
            patched,
        )
        return patched if patched != content else None

    elif iv_id == 6:
        # PV installation — inject a simple Generator:Photovoltaic + ElectricLoadCenter
        # Sized to 70% of roof area at 17% efficiency.
        # EnergyPlus requires a valid BuildingSurface:Detailed roof surface name.
        # We extract the first roof surface from the IDF; if none found, skip simulation.
        if "Generator:Photovoltaic" in content:
            return None  # already has PV

        # Find first roof surface name
        # BuildingSurface:Detailed blocks: first field after keyword is the surface name,
        # and the second field (Surface Type) must be "Roof".
        roof_match = re.search(
            r'BuildingSurface:Detailed,\s*\n\s*([^\n,;]+),\s*!-[^\n]*\n\s*Roof,',
            content,
            re.IGNORECASE,
        )
        if not roof_match:
            # Fallback: any surface with "Roof" as surface type on the same/next line
            roof_match = re.search(
                r'BuildingSurface:Detailed,\s*\n\s*([^\n,;]+),',
                content,
            )
        if not roof_match:
            print(f"  ⚠ No roof surface found in IDF — cannot inject PV block")
            return None

        roof_surface = roof_match.group(1).strip()

        pv_block = (
            "\n\nOutput:Meter,\n"
            "  ElectricityNet:Facility,  !- Key Name\n"
            "  Monthly;                  !- Reporting Frequency\n\n"
            "Generator:Photovoltaic,\n"
            f"  PV_Counterfactual,       !- Name\n"
            f"  {roof_surface},          !- Surface Name\n"
            "  PhotovoltaicPerformance:Simple,  !- Photovoltaic Performance Object Type\n"
            "  PV_Perf_Counterfactual,  !- Module Performance Name\n"
            "  Decoupled,               !- Heat Transfer Integration Mode\n"
            "  1,                       !- Number of Series Strings in Parallel\n"
            "  1;                       !- Number of Modules in Series\n\n"
            "PhotovoltaicPerformance:Simple,\n"
            "  PV_Perf_Counterfactual,  !- Name\n"
            "  0.7,                     !- Fraction of Surface Area with Active Solar Cells\n"
            "  Fixed,                   !- Conversion Efficiency Input Mode\n"
            "  0.17;                    !- Value for Cell Efficiency if Fixed\n\n"
            "ElectricLoadCenter:Generators,\n"
            "  PV_Generators,           !- Name\n"
            "  PV_Counterfactual,       !- Generator Name 1\n"
            "  Generator:Photovoltaic,  !- Generator Object Type 1\n"
            "  10000,                   !- Generator Rated Electric Power Output 1 {W}\n"
            "  ,                        !- Generator Availability Schedule Name 1\n"
            "  ;                        !- Generator Rated Thermal to Electrical Power Ratio 1\n\n"
            "ElectricLoadCenter:Distribution,\n"
            "  PV_Load_Center,          !- Name\n"
            "  PV_Generators,           !- Generator List Name\n"
            "  Baseload,                !- Generator Operation Scheme Type\n"
            "  0,                       !- Demand Limit Scheme Purchased Electric Demand Limit\n"
            "  ,                        !- Track Schedule Name Scheme Schedule Name\n"
            "  ,                        !- Track Meter Scheme Meter Name\n"
            "  AlternatingCurrent;      !- Electrical Buss Type\n"
        )
        return content + pv_block

    elif iv_id == 7:
        patched = content
        patched = re.sub(
            r'([\d.]+)\s*,(\s*!-\s*@.*?Solar\s*Absorptance)',
            lambda m: f"{IV7_ROOF_ABSORPTANCE} ,{m.group(2)}",
            patched,
        )
        patched = re.sub(
            r'([\d.]+),(\s+!-\s+Solar Absorptance)',
            lambda m: f"{IV7_ROOF_ABSORPTANCE},{m.group(2)}",
            patched,
        )
        if patched == content:
            patched = re.sub(
                r'(0\.\d+)\s*,(\s*!-\s*[^!]*[Ss]olar[Aa]bsorptance)',
                lambda m: f"{IV7_ROOF_ABSORPTANCE} ,{m.group(2)}",
                patched,
            )
        return patched if patched != content else None

    return None


def _parse_mtr_monthly_map(mtr_path, meter_name="Electricity:Facility"):
    """Parse monthly kWh map from an EnergyPlus .mtr file for a given meter."""
    try:
        lines = mtr_path.read_text(errors="ignore").splitlines()

        var_id_map: dict[int, str] = {}
        for line in lines:
            if line.strip() == "End of Data Dictionary":
                break
            parts = line.strip().split(",")
            try:
                var_id = int(parts[0])
            except (ValueError, IndexError):
                continue
            if len(parts) >= 3:
                var_id_map[var_id] = parts[2].strip()

        target_id = None
        for vid, label in var_id_map.items():
            if meter_name.lower() in label.lower():
                target_id = vid
                break
        if target_id is None and meter_name == "Electricity:Facility":
            target_id = 13
        if target_id is None:
            return {}

        monthly: dict[int, float] = {}
        current_month = None
        for line in lines:
            parts = line.strip().split(",")
            try:
                code = int(parts[0])
            except (ValueError, IndexError):
                continue
            if code == 4 and len(parts) >= 3:
                try:
                    current_month = int(parts[2].strip())
                except ValueError:
                    pass
            elif code == target_id and current_month is not None and len(parts) >= 2:
                try:
                    joules = float(parts[1].strip())
                    monthly[current_month] = max(0.0, joules / 3_600_000.0)
                except ValueError:
                    pass
        return monthly
    except Exception:
        return {}


def _parse_annual_kwh_from_mtr(mtr_path, meter_name="Electricity:Facility"):
    monthly = _parse_mtr_monthly_map(mtr_path, meter_name=meter_name)
    return sum(monthly.values()) if monthly else None


def run_ep_counterfactual(
    patched_content,
    building,
    iv_id,
    epw_path,
    outputs_dir,
    intervention_idfs_dir=None,
):
    """
    Write patched IDF to the intervention_idfs dir, run EnergyPlus, and return annual kWh.
    The original IDF is never modified — only the patched copy written here is used.
    Returns None if EnergyPlus is not available or the run fails.
    """
    from pathlib import Path as _P
    import shutil
    # Use dedicated intervention IDFs dir (never touches originals)
    idfs_root = _P(intervention_idfs_dir) if intervention_idfs_dir else _P(INTERVENTION_IDFS_DIR)
    work_dir = idfs_root / building / f"iv{iv_id}"
    # Clear stale outputs before running to avoid re-using old .mtr files
    if work_dir.exists():
        for f in work_dir.iterdir():
            if f.is_file() and f.suffix in (".mtr", ".eso", ".err", ".eio", ".end"):
                f.unlink()
    work_dir.mkdir(parents=True, exist_ok=True)

    idf_tmp = work_dir / f"{building}_iv{iv_id}.idf"
    idf_tmp.write_text(patched_content)

    ep = _P(EP_BINARY)
    if not ep.exists():
        print(f"  ⚠ EnergyPlus not found at {EP_BINARY} — skipping simulation for iv{iv_id}")
        return None

    try:
        # Use -d to set output directory; pass IDF and EPW as absolute paths.
        # Do not set cwd — EnergyPlus resolves relative paths against cwd which
        # can cause double-path issues when paths are already absolute.
        result = subprocess.run(
            [str(ep), "-w", epw_path, "-d", str(work_dir), "-r", str(idf_tmp)],
            capture_output=True,
            text=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        print(f"  ⚠ EnergyPlus timed out for {building} iv{iv_id}")
        return None
    except Exception as e:
        print(f"  ⚠ EnergyPlus subprocess error for {building} iv{iv_id}: {e}")
        return None

    # EnergyPlus on macOS/arm64 sometimes exits non-zero (SIGSEGV -11 on teardown)
    # even when the simulation completed and wrote valid output files.
    # Determine success by presence of .mtr output, not return code.
    mtr_candidates_early = list(work_dir.glob("*.mtr"))
    if not mtr_candidates_early:
        mtr_candidates_early = [work_dir / "eplusout.mtr"]
    mtr_exists = mtr_candidates_early[0].exists() if mtr_candidates_early else False

    if result.returncode != 0 and not mtr_exists:
        print(f"  ⚠ EnergyPlus failed for {building} iv{iv_id} (exit {result.returncode}, no .mtr output)")
        stdout_tail = (result.stdout or "")[-600:].strip()
        stderr_tail = (result.stderr or "")[-400:].strip()
        if stdout_tail:
            print(f"    stdout: {stdout_tail}")
        if stderr_tail:
            print(f"    stderr: {stderr_tail}")
        return None

    if result.returncode != 0 and mtr_exists:
        print(f"     (EnergyPlus exited {result.returncode} but .mtr output found — treating as success)")

    # Locate .mtr output
    mtr_candidates = list(work_dir.glob("*.mtr"))
    if not mtr_candidates:
        mtr_candidates = [work_dir / "eplusout.mtr"]
    mtr_path = mtr_candidates[0] if mtr_candidates and mtr_candidates[0].exists() else None
    if mtr_path is None:
        print(f"  ⚠ No .mtr output found for {building} iv{iv_id}")
        return None

    # Counterfactuals must be evaluated on the same accounting basis as baseline:
    # total kWh including cooling electrical equivalent.
    elec_meter = "ElectricityNet:Facility" if iv_id == 6 else "Electricity:Facility"
    annual_elec_kwh = _parse_annual_kwh_from_mtr(mtr_path, meter_name=elec_meter) or 0.0
    annual_cooling_therm_kwh = _parse_annual_kwh_from_mtr(mtr_path, meter_name="DistrictCooling:Facility") or 0.0
    annual_cooling_adj_kwh = annual_cooling_therm_kwh / 4.5 if annual_cooling_therm_kwh else 0.0
    return annual_elec_kwh + annual_cooling_adj_kwh


def compute_simulated_savings(
    building: str,
    idf_path: Path,
    baseline_kwh: float,
    epw_path: str,
    outputs_dir: Path,
    fp: dict,
    intervention_idfs_dir=None,
) -> dict:
    """
    Run counterfactual EnergyPlus simulations for all SIMULATABLE_INTERVENTIONS.
    Patched IDFs are written to intervention_idfs_dir (default: INTERVENTION_IDFS_DIR).
    Original IDFs are never modified.

    Returns:
        {intervention_id: kwh_saved}  — only for successfully simulated interventions.
        Unsuccessful or non-simulatable interventions are simply absent from the dict.
    """
    idf_content = idf_path.read_text(errors="ignore")
    sim_results: dict[int, float] = {}

    for iv_id in sorted(SIMULATABLE_INTERVENTIONS):
        iv_name = INTERVENTION_NAMES.get(iv_id, f"iv{iv_id}")
        print(f"  ⚡ Simulating counterfactual: Intervention {iv_id} — {iv_name} …", flush=True)

        patched = patch_idf_for_intervention(idf_content, iv_id, fp)
        if patched is None:
            print(f"     → No IDF change required or intervention not patchable — skipping simulation")
            continue

        cf_kwh = run_ep_counterfactual(patched, building, iv_id, epw_path, outputs_dir,
                                       intervention_idfs_dir=intervention_idfs_dir)
        if cf_kwh is None:
            print(f"     → Simulation failed — will fall back to literature estimate")
            continue

        saved = baseline_kwh - cf_kwh
        pct   = saved / baseline_kwh * 100 if baseline_kwh > 0 else 0
        print(f"     → Baseline: {baseline_kwh:,.0f} kWh  |  Counterfactual: {cf_kwh:,.0f} kWh"
              f"  |  Saved: {saved:,.0f} kWh ({pct:.1f}%)")
        if abs(saved) < 1.0:
            print(f"     ⚠ Near-zero delta — baseline simulation may be simplified/uniform. "
                  f"Falling back to literature estimate for iv{iv_id}.")
            continue
        sim_results[iv_id] = saved

    return sim_results


# ─── Intervention Scoring ─────────────────────────────────────────────────────

def score_interventions(
    fp: dict,
    annual_kwh: float,
    sim_results=None,
    floor_area_m2=None,
) -> tuple[list, list]:
    """
    Score all 8 interventions against the IDF fingerprint.

    Args:
        fp:             IDF fingerprint from extract_fingerprint().
        annual_kwh:     Baseline annual electricity consumption [kWh].
        sim_results:    Optional dict {intervention_id: kwh_saved} from counterfactual
                        simulations. When provided, simulation values override literature
                        estimates for those interventions.
        floor_area_m2:  Gross floor area in m² — used for Singapore capex estimation.

    Returns:
        (interventions list, flags list)
    """
    if sim_results is None:
        sim_results = {}

    interventions = []
    flags = []

    for i in range(1, 8):
        lo, hi = INTERVENTION_REDUCTIONS[i]
        mid = (lo + hi) / 2

        current_val = target_val = note = None
        skip = False
        flag = None

        if i == 1:
            sp = fp.get("cooling_setpoint_c")
            current_val = f"{sp} °C constant" if sp else "unknown"
            target_sp   = round((sp or 25) + 1, 1)
            target_val  = f"{target_sp} °C (+1 °C from current)"
            delta        = 1   # capped at +1 °C per NUS policy
            mid          = 0.035 * delta   # 3.5% per °C raised

        elif i == 2:
            lpd = fp.get("lighting_lpd_wm2")
            current_val = f"{lpd} W/m²" if lpd else "unknown"
            target_val  = f"{IV2_TARGET_LPD} W/m²"
            if lpd and lpd <= IV2_TARGET_LPD:
                mid   = 0.02   # already efficient — minor gain
                note  = "LPD already low — minor gain from further reduction"

        elif i == 3:
            current_val = "Not present" if not fp.get("has_daylighting_controls") else "Present"
            target_val  = "Daylighting:Controls per zone with occupancy + daylight sensor"
            vt = fp.get("glazing_vt", 0)
            if vt and vt < 0.3:
                mid   = 0.03
                flag  = f"Glazing VT={vt:.3f} limits dimming to perimeter zones only"
                flags.append(flag)

        elif i == 4:
            shgc = fp.get("glazing_shgc", 0.3)
            current_val = f"SHGC={shgc}, no ShadingControl" if not fp.get("has_shading_control") else f"SHGC={shgc}, ShadingControl present"
            target_val  = "Dynamic blind control (solar-triggered ShadingControl)"
            if shgc and shgc < 0.3:
                mid   = 0.02
                note  = "SHGC already low — shading benefit is marginal"

        elif i == 5:
            hvac = fp.get("hvac_type", "unknown")
            current_val = hvac
            target_val  = "High-COP VRF or chiller (COP 4–6)"
            if hvac == "IdealLoadsAirSystem":
                flag  = "HVAC is IdealLoadsAirSystem — Intervention 5 saving is estimated, not simulated"
                flags.append(flag)
            PATCH_COMPLEXITY[5] = "system_swap"

        elif i == 6:
            current_val = "Not present"
            target_val  = "Rooftop PV array sized to 70% of roof area (17% efficiency)"
            # PV is supply-side — no interaction discount applies

        elif i == 7:
            absorb = fp.get("solar_absorptance", 0.7)
            current_val = f"{absorb} (roof + walls)"
            target_val  = f"{IV7_ROOF_ABSORPTANCE} (roof) / {IV7_WALL_ABSORPTANCE} (wall) — cool white coating"
            if absorb and absorb < 0.4:
                mid   = 0.01
                note  = "Solar absorptance already low — marginal gain"

        # ── Override with simulation result if available ──────────────────────
        if i in sim_results:
            est_kwh_saved = sim_results[i]
            if annual_kwh > 0:
                mid = est_kwh_saved / annual_kwh
            source = "simulated"
        else:
            est_kwh_saved = annual_kwh * mid
            source = "estimated"
            if i == 5 and not note:
                note = (
                    "HVAC system swap (IdealLoads → VRF/chiller) requires full model rebuild — "
                    "cannot be patched parametrically. Savings are literature estimates."
                )

        est_co2_saved = est_kwh_saved * GRID_FACTOR / 1000  # tCO2e

        # Singapore-market capex estimate
        capex = compute_intervention_capex(i, fp, floor_area_m2)

        # Simple payback (years) — only meaningful when capex > 0 and savings > 0
        if capex["capex_sgd"] > 0 and est_kwh_saved > 0:
            # SGD electricity saving: use mid commercial tariff SGD 0.32/kWh (SP 2024)
            annual_sgd_saved = est_kwh_saved * 0.32
            simple_payback_years = round(capex["capex_sgd"] / annual_sgd_saved, 1)
        else:
            simple_payback_years = None

        interventions.append({
            "id":                      i,
            "name":                    INTERVENTION_NAMES[i],
            "current_value":           current_val,
            "target_value":            target_val,
            "estimated_reduction_pct": round(mid * 100, 1),
            "est_kwh_saved":           round(est_kwh_saved, 0),
            "est_co2_saved_tco2e":     round(est_co2_saved, 2),
            "capex_tier":              CAPEX_TIERS[i],
            "capex_sgd":               capex,
            "simple_payback_years":    simple_payback_years,
            "patch_complexity":        PATCH_COMPLEXITY[i],
            "idf_objects":             INTERVENTION_IDF_OBJECTS[i],
            "source":                  source,
            "note":                    note,
            "flag":                    flag,
        })

    return interventions, flags


# ─── Scenario Assembly ────────────────────────────────────────────────────────

def assemble_scenarios(
    interventions: list,
    annual_tco2e: float,
    annual_kwh: float,
    fp: dict,
    mode: str = None,
) -> list:
    """Assemble scenario bundles from intervention scores."""

    iv_by_id = {iv["id"]: iv for iv in interventions}
    scenarios = []

    defs = SCENARIO_DEFINITIONS
    if mode and mode in defs:
        defs = {mode: defs[mode]}

    for sid, sdef in defs.items():
        iv_ids = sdef["interventions"]

        # Separate supply-side (PV=6) from demand-side
        demand_ids = [i for i in iv_ids if i != 6]
        supply_ids = [i for i in iv_ids if i == 6]

        # Sum demand-side reductions, then apply interaction discount
        demand_pct = sum(iv_by_id[i]["estimated_reduction_pct"] / 100 for i in demand_ids if i in iv_by_id)
        if len(demand_ids) > 1:
            demand_pct *= (1 - INTERACTION_DISC)

        # Supply-side (PV) adds on top without discount
        supply_pct = sum(iv_by_id[i]["estimated_reduction_pct"] / 100 for i in supply_ids if i in iv_by_id)

        total_pct = min(1.0, demand_pct + supply_pct)
        co2_saved = annual_tco2e * total_pct
        co2_remaining = annual_tco2e - co2_saved

        # Capex tier = highest tier among applied interventions
        tier_order = ["zero", "low", "medium", "medium-high", "high"]
        capex_tiers_applied = [iv_by_id[i]["capex_tier"] for i in iv_ids if i in iv_by_id]
        capex_tier = max(capex_tiers_applied, key=lambda t: tier_order.index(t) if t in tier_order else 0)

        # Flags specific to this scenario
        scenario_flags = [iv_by_id[i]["flag"] for i in iv_ids if i in iv_by_id and iv_by_id[i]["flag"]]

        # Sources used in this scenario
        sources_in_scenario = list({iv_by_id[i]["source"] for i in iv_ids if i in iv_by_id})

        # ── Scenario-level capex rollup (Singapore SGD) ───────────────────────
        scenario_capex_sgd      = sum(iv_by_id[i]["capex_sgd"]["capex_sgd"]      for i in iv_ids if i in iv_by_id)
        scenario_capex_sgd_low  = sum(iv_by_id[i]["capex_sgd"]["capex_sgd_low"]  for i in iv_ids if i in iv_by_id)
        scenario_capex_sgd_high = sum(iv_by_id[i]["capex_sgd"]["capex_sgd_high"] for i in iv_ids if i in iv_by_id)

        # Per-m² GFA cost intensity — sum of each intervention's SGD/m² GFA rate
        scenario_cost_per_m2_mid  = sum(iv_by_id[i]["capex_sgd"]["cost_per_m2_gfa"]      for i in iv_ids if i in iv_by_id)
        scenario_cost_per_m2_low  = sum(iv_by_id[i]["capex_sgd"]["cost_per_m2_gfa_low"]  for i in iv_ids if i in iv_by_id)
        scenario_cost_per_m2_high = sum(iv_by_id[i]["capex_sgd"]["cost_per_m2_gfa_high"] for i in iv_ids if i in iv_by_id)

        # Cost of carbon abated (SGD / tCO2e)
        co2_saved_for_capex = annual_tco2e * total_pct
        if co2_saved_for_capex > 0 and scenario_capex_sgd > 0:
            # Annualise capex over a 15-year asset life
            annualised_capex = scenario_capex_sgd / 15
            cost_per_tco2e   = round(annualised_capex / co2_saved_for_capex, 0)
        else:
            cost_per_tco2e   = 0

        # Scenario capex label (human-readable range)
        if scenario_capex_sgd == 0:
            capex_label = "SGD 0 (zero capex)"
        elif scenario_capex_sgd < 100_000:
            capex_label = f"SGD {scenario_capex_sgd_low:,.0f} – {scenario_capex_sgd_high:,.0f}"
        elif scenario_capex_sgd < 1_000_000:
            capex_label = (f"SGD {scenario_capex_sgd_low/1000:.0f}k – "
                           f"{scenario_capex_sgd_high/1000:.0f}k")
        else:
            capex_label = (f"SGD {scenario_capex_sgd_low/1e6:.1f}M – "
                           f"{scenario_capex_sgd_high/1e6:.1f}M")

        capex_summary = {
            "total_sgd":               round(scenario_capex_sgd, 0),
            "total_sgd_low":           round(scenario_capex_sgd_low, 0),
            "total_sgd_high":          round(scenario_capex_sgd_high, 0),
            "cost_per_m2_gfa":         round(scenario_cost_per_m2_mid, 1),
            "cost_per_m2_gfa_low":     round(scenario_cost_per_m2_low, 1),
            "cost_per_m2_gfa_high":    round(scenario_cost_per_m2_high, 1),
            "label":                   capex_label,
            "cost_per_tco2e_sgd":      int(cost_per_tco2e),
            "annualisation_years":     15,
            "currency":                "SGD",
            "tariff_assumption":       "SGD 0.32/kWh (SP commercial 2024)",
            "note": (
                "Costs are Singapore market estimates (BCA BCIS / NEA ENER+ 2024). "
                "All interventions normalised to SGD/m² GFA for comparability. "
                "Grants (NEA, EDB E2F, BCA Green Mark Incentive) may reduce net capex "
                "by 30–50% for qualifying NUS buildings."
            ),
        }

        # Explanation
        explanation = _build_explanation(sid, iv_ids, iv_by_id, fp, total_pct, co2_saved, annual_tco2e)
        tradeoff    = _build_tradeoff(sid, capex_tier, total_pct)

        # ── Scenario-level simple payback (computed, not literature) ─────────
        # Total scenario kWh saved = sum of individual intervention savings
        scenario_kwh_saved = sum(
            iv_by_id[i]["est_kwh_saved"] for i in iv_ids if i in iv_by_id
        )
        if scenario_capex_sgd > 0 and scenario_kwh_saved > 0:
            scenario_annual_sgd_saved = scenario_kwh_saved * 0.32  # SGD 0.32/kWh SP 2024
            scenario_simple_payback_years = round(
                scenario_capex_sgd / scenario_annual_sgd_saved, 1
            )
        else:
            scenario_simple_payback_years = None  # zero capex or zero savings

        scenarios.append({
            "id":                       sid,
            "label":                    sdef["label"],
            "tier":                     sdef.get("tier", ""),
            "interventions_applied":    iv_ids,
            "co2_saved_tco2e":          round(co2_saved, 2),
            "co2_remaining_tco2e":      round(co2_remaining, 2),
            "reduction_pct":            round(total_pct * 100, 1),
            "capex_tier":               capex_tier,
            "capex_summary":            capex_summary,
            "simple_payback_years":     scenario_simple_payback_years,  # computed
            "sources":                  sources_in_scenario,
            "flags":                    scenario_flags,
            "explanation":              explanation,
            "tradeoff":                 tradeoff,
        })

    return scenarios


def _build_explanation(sid, iv_ids, iv_by_id, fp, total_pct, co2_saved, annual_tco2e):
    sp     = fp.get("cooling_setpoint_c") or 25
    lpd    = fp.get("lighting_lpd_wm2") or 9
    absorb = fp.get("solar_absorptance") or 0.7
    zones  = fp.get("zones") or 0
    vt     = fp.get("glazing_vt") or 0.25
    r_val  = fp.get("wall_insulation_r_value") or 0.15

    if sid == "shallow":
        target_sp = round(sp + 1, 1)
        return (
            f"Zero-cost intervention deployable immediately — no procurement, hardware, or "
            f"construction works required. "
            f"Raising the cooling setpoint by +1 °C (from {sp} °C → {target_sp} °C, "
            f"uniform across all {zones} zones) is a BAS/IBMS schedule edit performed "
            f"in-house by the NUS facilities team at zero external capex. "
            f"Estimated saving: {co2_saved:.1f} tCO2e/year ({total_pct*100:.0f}% of baseline)."
        )
    elif sid == "medium":
        return (
            f"Moderate-capex operational upgrades and envelope retrofit — all reversible or low-risk. "
            f"Includes the zero-cost setpoint increase, LED lighting ({lpd} W/m² → {IV2_TARGET_LPD} W/m² "
            f"across {zones} zones; typically SGD 25–45/m² GFA, simple payback 3–5 years, NEA ENER+ "
            f"grant may offset up to 50% of incremental cost), dynamic shading controls "
            f"(SGD ~480/window, motorised blind + BAS-linked solar sensor), and cool-paint coating "
            f"on roof and walls (SGD 6–11/m² painted surface, Nippon CoolTect / Jotun Jotashield, "
            f"payback 2–5 years). "
            f"Combined, saves ~{co2_saved:.1f} tCO2e/year ({total_pct*100:.0f}% reduction)."
        )
    elif sid == "deep":
        return (
            f"Full infrastructure programme: all 7 interventions applied sequentially. "
            f"Adds dimming controls (DALI-2 + sensors, SGD ~4,500/zone; note: glazing "
            f"VT={vt:.3f} limits benefit to perimeter zones) and a high-COP HVAC system "
            f"upgrade (SGD 200–360/m² GFA, BCA BCIS 2024; EDB E2F grant may offset 50% "
            f"for qualifying public-sector entities) on top of medium-tier measures. "
            f"The HVAC upgrade (Intervention 5) is flagged as a simulation limitation — "
            f"real COP gains must be validated against a full HVAC model. "
            f"With all levers active, estimated pathway: {total_pct*100:.0f}% reduction "
            f"({co2_saved:.1f} tCO2e/year saved), consistent with NUS 2030 target. "
            f"Any residual emissions offset via RECs or carbon credits."
        )
    return ""


def _build_tradeoff(sid, capex_tier, total_pct):
    tradeoffs = {
        "shallow": (
            "Zero cost — maximum speed of deployment, no procurement or works required. "
            "Savings are modest (setpoint only) and don't address structural energy drivers, "
            "but the measure is risk-free and immediately reversible."
        ),
        "medium":  (
            "Best overall ROI — meaningful savings at moderate capital outlay. "
            "LED payback 3–5 yr, cool-paint 2–5 yr, shading 6–10 yr; all measures reversible "
            "or incremental. NEA / EDB grants can cut effective capex by 30–50% for qualifying "
            "NUS buildings."
        ),
        "deep":    (
            "Highest impact; achieves NUS 2030 target but requires a phased multi-year programme "
            "and major capital commitment. HVAC replacement (largest cost line) has a long "
            "payback (8–15 yr) but dramatically improves occupant comfort and system reliability. "
            "EDB E2F grant and BCA Green Mark Incentive can materially reduce net cost."
        ),
    }
    return tradeoffs.get(sid, "")


# ─── Baseline ────────────────────────────────────────────────────────────────

def compute_baseline(csv_path: Path, building: str, registry_path: Path) -> dict:
    """Compute annual carbon baseline from parsed monthly CSV."""
    df = pd.read_csv(csv_path, index_col=0)

    # parsed monthly CSVs include a final ANNUAL summary row. Exclude it here,
    # otherwise baseline totals get double-counted (12 monthly rows + annual row).
    if "month_name" in df.columns:
        df = df[df["month_name"].astype(str).str.upper() != "ANNUAL"].copy()

    annual_elec_kwh = df["electricity_facility_kwh"].sum()
    annual_cooling_adj_kwh = df["cooling_elec_adj_kwh"].sum() if "cooling_elec_adj_kwh" in df.columns else 0
    annual_kwh   = annual_elec_kwh + annual_cooling_adj_kwh
    annual_tco2e = annual_kwh * GRID_FACTOR / 1000

    # Floor area and typology from registry
    floor_area = None
    building_typology = None
    if registry_path.exists():
        registry = json.loads(registry_path.read_text())
        entry = registry.get(building, {})
        floor_area = entry.get("floor_area_m2") or entry.get("floor_area")
        building_typology = entry.get("typology") or entry.get("building_type") or ""

    eui = annual_kwh / floor_area if floor_area else None
    base_eui = annual_elec_kwh / floor_area if floor_area else None

    # BCA rating — use Hospital thresholds for health facilities, IHL thresholds for all others
    is_health = building_typology.lower() in ("health", "hospital", "medical") if building_typology else False
    bca_benchmarks = BCA_BENCHMARKS_HOSPITAL if is_health else BCA_BENCHMARKS_IHL
    bca_benchmark_type = "Hospital" if is_health else "IHL"
    bca_rating = "No rating"
    if eui:
        for threshold, label in bca_benchmarks:
            if eui < threshold:
                bca_rating = label
                break

    # Monthly profile
    monthly = []
    for _, row in df.iterrows():
        monthly.append({
            "month":        int(row.name) if str(row.name).isdigit() else row.name,
            "month_name":   row.get("month_name", ""),
            "kwh":          round(row["electricity_facility_kwh"] + row.get("cooling_elec_adj_kwh", 0), 0),
            "tco2e":        round((row["electricity_facility_kwh"] + row.get("cooling_elec_adj_kwh", 0)) * GRID_FACTOR / 1000, 3),
        })

    return {
        "annual_kwh":      round(annual_kwh, 0),
        "annual_elec_kwh": round(annual_elec_kwh, 0),
        "annual_cooling_adj_kwh": round(annual_cooling_adj_kwh, 0),
        "annual_tco2e":    round(annual_tco2e, 2),
        "carbon_tco2e":    round(annual_tco2e, 2),  # alias for downstream consumers
        "floor_area_m2":   floor_area,
        "base_eui_kwh_m2": round(base_eui, 1) if base_eui else None,
        "eui_kwh_m2":      round(eui, 1) if eui else None,
        "bca_rating":      bca_rating,
        "bca_benchmark_type": bca_benchmark_type,
        "scope":           "Scope 2 only (all-electric)",
        "grid_factor":     GRID_FACTOR,
        "monthly_profile": monthly,
    }


# ─── Console Summary ─────────────────────────────────────────────────────────

def print_summary(building, baseline, fp, scenarios, flags):
    print(f"\n🧭 {building} — carbon scenario analysis")
    print(f"\nBaseline: {baseline['annual_tco2e']:.1f} tCO2e/year  |  "
          f"EUI: {baseline['eui_kwh_m2'] or 'N/A'} kWh/m²  |  BCA: {baseline['bca_rating']}")

    print(f"\nIDF fingerprint:")
    print(f"  Cooling SP:  {fp.get('cooling_setpoint_c')} °C (all {fp.get('zones')} zones)")
    print(f"  LPD:         {fp.get('lighting_lpd_wm2')} W/m²")
    print(f"  EPD:         {fp.get('equipment_epd_wm2')} W/m²")
    print(f"  Solar abs.:  {fp.get('solar_absorptance')} (roof + walls)")
    print(f"  HVAC:        {fp.get('hvac_type')}")
    print(f"  PV:          {'Yes' if fp.get('has_pv') else 'Not modelled'}")
    print(f"  Glazing:     SHGC={fp.get('glazing_shgc')}  U={fp.get('glazing_u_value')}  VT={fp.get('glazing_vt')}")

    print(f"\n{'Scenario':<26} {'Reduction':>10} {'CO2 saved':>12} {'SGD/m² GFA':>12} {'Capex (SGD)':>22} {'SGD/tCO2e':>10} {'Sources'}")
    print(f"{'─'*110}")
    for s in scenarios:
        label       = f"{s.get('tier','')} {s['label']}"
        sources_str = "+".join(sorted(s.get("sources", ["estimated"])))
        cs          = s.get("capex_summary", {})
        capex_label = cs.get("label", s["capex_tier"])
        cpt         = cs.get("cost_per_tco2e_sgd", "—")
        cpt_str     = f"${cpt:,}" if isinstance(cpt, int) and cpt > 0 else "—"
        m2_mid      = cs.get("cost_per_m2_gfa", 0)
        m2_low      = cs.get("cost_per_m2_gfa_low", 0)
        m2_high     = cs.get("cost_per_m2_gfa_high", 0)
        m2_str      = f"${m2_low:.0f}–${m2_high:.0f}/m²" if m2_mid > 0 else "$0/m²"
        print(f"  {label:<24} {s['reduction_pct']:>8.0f}%  "
              f"{s['co2_saved_tco2e']:>9.1f} tCO2e  "
              f"{m2_str:>12}  {capex_label:>22} {cpt_str:>10}  {sources_str}")

    if flags:
        print(f"\n⚠ Flags:")
        for f in flags:
            print(f"  • {f}")

    print(f"\n  Cost basis: Singapore market rates (BCA BCIS / NEA ENER+ 2024, SGD).")
    print(f"  Grants (NEA, EDB E2F, BCA Green Mark Incentive) may reduce net capex by 30–50%.")
    print(f"  SGD/tCO2e = annualised capex (÷15 yr) ÷ annual CO2 saved.")


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Compass carbon scenario engine")
    parser.add_argument("--building",  required=True,             help="Building name, e.g. FOE6")
    parser.add_argument("--outputs",   default="outputs",         help="Path to outputs dir")
    parser.add_argument("--idfs",      default="idfs",            help="Path to IDFs dir")
    parser.add_argument("--mode",      default=None,              help="Scenario mode filter: shallow|medium|deep")
    parser.add_argument("--simulate",  action="store_true", default=True, help=(
        "Run counterfactual EnergyPlus simulations for all patchable interventions "
        f"({', '.join(f'iv{i} ({INTERVENTION_NAMES[i]})' for i in sorted(SIMULATABLE_INTERVENTIONS))}). "
        "IV5 (HVAC system swap) uses literature estimate — requires full model rebuild. "
        "Use --no-simulate to skip and use literature estimates only."
    ))
    parser.add_argument("--no-simulate", dest="simulate", action="store_false")
    parser.add_argument("--epw",       default=None, help="EPW weather file override (bypasses Nimbus auto-resolution)")
    parser.add_argument("--month",     default=None, help="Simulation month YYYY-MM — used to resolve Nimbus calibrated EPW")
    parser.add_argument("--intervention-idfs", default=None, dest="intervention_idfs",
                        help=(
                            "Directory for counterfactual (patched) IDF files and EnergyPlus run outputs. "
                            f"Original IDFs in idfs/ are never modified. "
                            f"Default: {INTERVENTION_IDFS_DIR}"
                        ))
    args = parser.parse_args()

    b           = args.building
    outputs_dir = Path(args.outputs)
    idfs_dir    = Path(args.idfs)

    # Locate parsed CSV
    csv_path = outputs_dir / b / "parsed" / f"{b}_monthly.csv"
    if not csv_path.exists():
        print(f"ERROR: No parsed data for {b}. Run `nus-parse` first.")
        sys.exit(1)

    # ── IDF path resolution ───────────────────────────────────────────────────
    # source_idf: annotated IDF from idfs/ dir — used for fingerprinting (has @..@ comments).
    # sim_idf:    simulation-ready IDF — used as base for counterfactual EP runs.
    #             Prefer calibrated > prepared > source (calibrated is most accurate).

    source_idf_matches = list(idfs_dir.rglob(f"{b}.idf"))
    source_idf = source_idf_matches[0] if source_idf_matches else None

    sim_idf = outputs_dir / b / "calibrated" / f"{b}.idf"
    if not sim_idf.exists():
        sim_idf = outputs_dir / b / "prepared" / f"{b}_prepared.idf"
    if not sim_idf.exists():
        sim_idf = source_idf  # fallback: use source IDF directly

    # Fingerprint always from source IDF (annotated)
    fingerprint_idf = source_idf if source_idf and source_idf.exists() else sim_idf

    if not fingerprint_idf or not Path(fingerprint_idf).exists():
        print(f"ERROR: No IDF found for {b}. Checked {idfs_dir}/**/{b}.idf and outputs dirs.")
        sys.exit(1)
    if not sim_idf or not Path(sim_idf).exists():
        print(f"ERROR: No simulation-ready IDF found for {b}. Run simulate.py first.")
        sys.exit(1)

    registry_path = outputs_dir.parent / "building_registry.json"

    # ── Registry pre-flight: ensure building has floor_area_m2 ───────────────
    # Auto-populates from IDF if missing; never blocks the run.
    preflight_fn = _load_preflight_registry()
    if preflight_fn and fingerprint_idf:
        preflight_fn(b, Path(args.idfs), registry_path, idf_path=Path(fingerprint_idf))

    # Compute baseline from simulation-parsed CSV
    baseline = compute_baseline(csv_path, b, registry_path)

    # IDF fingerprint — from source (annotated) IDF
    fp = extract_fingerprint(Path(fingerprint_idf))

    # ── Optional: counterfactual simulations ──────────────────────────────────
    sim_results = {}
    if args.simulate:
        epw_path, epw_source = resolve_epw(month=args.month, override=args.epw)
        if epw_source.startswith("tmy"):
            print(f"  ⚠ No Nimbus calibrated EPW — using base TMY ({epw_path})")
        else:
            print(f"  🌦️ Using Nimbus calibrated EPW ({epw_source}): {epw_path}")
        if not Path(epw_path).exists():
            print(f"WARNING: EPW file not found at {epw_path} — falling back to estimates only")
        else:
            print(f"\n  Running counterfactual simulations for {b} …")
            print(f"  Fingerprint IDF : {fingerprint_idf}")
            print(f"  Simulation base : {sim_idf}")
            sim_results = compute_simulated_savings(
                b, Path(sim_idf), baseline["annual_kwh"], epw_path, outputs_dir, fp,
                intervention_idfs_dir=args.intervention_idfs,
            )
            n_sim = len(sim_results)
            n_est = len(SIMULATABLE_INTERVENTIONS) - n_sim
            print(f"  → {n_sim} intervention(s) simulated, {n_est} fell back to estimates\n")

    # Score interventions (simulation results override estimates where available)
    interventions, flags = score_interventions(
        fp, baseline["annual_kwh"],
        sim_results=sim_results,
        floor_area_m2=baseline.get("floor_area_m2"),
    )

    # Assemble scenarios
    scenarios = assemble_scenarios(
        interventions, baseline["annual_tco2e"], baseline["annual_kwh"], fp, args.mode
    )

    # Write output
    out_dir  = outputs_dir / b / "carbon"
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{b}_carbon_scenarios.json"

    result = {
        "building":         b,
        "run_date":         str(date.today()),
        "fingerprint_idf":  str(fingerprint_idf),
        "sim_idf":          str(sim_idf),
        "simulation_mode":  "counterfactual+estimates" if sim_results else "estimates_only",
        "simulated_interventions": sorted(sim_results.keys()),
        "baseline":         baseline,
        "idf_fingerprint":  fp,
        "interventions":    interventions,
        "scenarios":        scenarios,
        "flags":            flags,
    }

    out_path.write_text(json.dumps(result, indent=2))

    print_summary(b, baseline, fp, scenarios, flags)
    print(f"\n  Saved → {out_path}\n")


if __name__ == "__main__":
    main()
