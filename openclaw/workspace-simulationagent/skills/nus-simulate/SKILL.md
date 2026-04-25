---
name: nus-simulate
description: Run EnergyPlus simulations for NUS campus buildings and parse monthly energy output. Use when SimulationAgent is asked to simulate one or all buildings, check calibration status, or re-run after IDF changes.
metadata: {"openclaw": {"emoji": "⚡"}}
---

# NUS Simulate Skill

## Overview

This skill runs EnergyPlus energy simulations for NUS campus buildings via `simulate.py`, parses the outputs into monthly CSVs, and optionally calculates MAPE against ground truth meter data.

## Key Paths

| What | Path |
|---|---|
| Skill root (`SKILL_DIR`) | `/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/` |
| Simulation script | `SKILL_DIR/scripts/simulate.py` |
| IDF files | `/Users/ye/nus-energy/idfs/` (nested subdirs supported) |
| Base TMY/Default EPW | `/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw`  (Permanent default input for all runs unless overridden; Changji Airport can be mapped here for now)
| Calibrated EPW dir (Nimbus) | `/Users/ye/nus-energy/weather/calibrated/` |
| Outputs root | `/Users/ye/nus-energy/outputs/` |
| Ground truth (canonical 2024 wide CSV) | `/Users/ye/nus-energy/ground_truth/ground-truth.csv` |
| Ground truth (per-building, parsed fallback) | `/Users/ye/nus-energy/ground_truth/parsed/{BUILDING}_ground_truth.csv` |
| EnergyPlus | `/Applications/EnergyPlus-23-1-0/` |
| prepare_ground_truth.py | `workspace-anomalyagent/skills/nus-groundtruth/scripts/prepare_ground_truth.py` |

## Related Skills (invoke after simulation)
- `nus-parse` — read and display monthly kWh from simulation output (handled by AnomalyAgent)
- `nus-groundtruth` — compute ASHRAE metrics vs meter data
- `nus-calibrate` — propose and apply IDF parameter fixes
- `nus-report` — generate PDF calibration reports

## Buildings

318 IDF files total, organised in `/Users/ye/nus-energy/idfs/`. Structure:
- **Top-level `.idf`** (e.g. `idfs/FOE13.idf`) — baseline variants
- **Subdirectory variants** (e.g. `idfs/A1_H_L/FOE13.idf`) — calibration scenario variants
  named after parameter-sweep codes (A1_H_L = scenario A1, High infiltration, Low setpoint, etc.)

Buildings with ground truth meter data (MAPE can be calculated):
- FOE6, FOE9, FOE13, FOE18, FOS43, FOS46 (A1_H_L) — plus 17 more across other archetypes (see nus-soul for full list)
- Additional GT files present for: FOE1, FOE3, FOE5, FOE10–12, FOE15–16, FOE19–20, FOE23–24, FOE26, FOS26, FOS35, FOS41, FOS44

Ground truth lookup always uses the **bare building stem** (e.g. `FOE13_ground_truth.csv`),
so a single GT file covers all subdirectory variants of the same building.

**Comparison policy (current default):**
- Prefer canonical 2024 data from `ground_truth/ground-truth.csv`
- Use parsed per-building files only as fallback
- Convert measured 2024 energy to annual EUI using `building_registry.json`
- Compare simulation against that **annual EUI** basis

**Comparison basis:** use the parsed CSV's annual **adjusted EUI** basis, matching `eui_adj_kwh_m2` summed across 2024 months.

**Run IDs:** In batch mode, each IDF gets a unique run_id:
- Top-level IDF: `FOE13`
- Subdirectory IDF: `A1_H_L__FOE13` (variant prefix + double underscore + stem)

Output folders follow the run_id pattern, e.g. `outputs/A1_H_L__FOE13/`.

## Pre-flight: Registry Validation

`simulate.py` now performs registry pre-flight automatically for each building before simulation.
If the building is missing from `building_registry.json` or has no `floor_area_m2`, it attempts auto-population first.

If you need to run it manually, use:

```bash
cd /Users/ye/nus-energy
python3 /Users/ye/.openclaw/workspace-simulationagent/skills/nus-registry/scripts/generate_registry.py \
  --idfs idfs/ --out building_registry.json
```

This extracts `floor_area_m2` from IDF Zone geometry and merges into the registry.
- ✅ Floor area found → registry updated, EUI will be calculated
- ⚠️ Not found → `floor_area_m2: null`, EUI shows as `—`, flag in Slack notification

**Policy:** warn-and-proceed. Never block simulation for a missing registry entry.
This means future buildings should auto-register during the workflow instead of failing EUI reporting later.

## Workflows

All commands must be run from `SKILL_DIR`:

```bash
cd /Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate
```

### EPW Resolution (Nimbus integration)

`simulate.py` automatically resolves the best available EPW via `resolve_epw()`:

1. **Permanent default**: `/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw` is now set as the permanent default for all simulation runs unless specifically overridden (currently substitutes for Changji Airport as instructed, update if a proper Changji EPW is provided)
2. **Nimbus calibrated EPW** — if `--month YYYY-MM` is supplied and `/Users/ye/nus-energy/weather/calibrated/{YYYY-MM}_site_calibrated.epw` exists, it is used automatically
3. **CLI override** — `--epw <path>` explicitly overrides all auto-resolution
4. **Fallback** — if no calibrated or CLI EPW is found, still uses the default above

```bash
# With Nimbus calibrated EPW (recommended when Nimbus has run for the month)
python scripts/simulate.py --idf /Users/ye/nus-energy/idfs/FOE13.idf --month 2024-08

# Force a specific EPW (bypasses Nimbus)
python scripts/simulate.py --idf /Users/ye/nus-energy/idfs/FOE13.idf \
    --epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw
```

### Run a single building

```bash
python scripts/simulate.py --idf /Users/ye/nus-energy/idfs/FOE13.idf
```

Output lands in: `/Users/ye/nus-energy/outputs/FOE13/`
- `outputs/FOE13/prepared/FOE13_prepared.idf` — tropical-adjusted IDF
- `outputs/FOE13/simulation/FOE13out.mtr` — raw EnergyPlus meter output (ESO format, monthly J)
- `outputs/FOE13/simulation/FOE13out.err` — EnergyPlus error log
- `outputs/FOE13/parsed/FOE13_monthly.csv` — clean 12-row monthly summary

### Run a specific variant

```bash
python scripts/simulate.py --idf /Users/ye/nus-energy/idfs/A1_H_L/FOE13.idf
```

Output lands in: `/Users/ye/nus-energy/outputs/FOE13/` (same as baseline — use batch mode for variant separation)

### Run all 318 IDFs (recursive, all variants)

```bash
# Serial (safe, default)
python scripts/simulate.py --idf-dir /Users/ye/nus-energy/idfs/

# Parallel — 8 workers (recommended for full batch)
python scripts/simulate.py --idf-dir /Users/ye/nus-energy/idfs/ --workers 8

# Parallel with MAPE
python scripts/simulate.py --idf-dir /Users/ye/nus-energy/idfs/ --workers 8 \
  --gt-dir /Users/ye/nus-energy/ground_truth/parsed/

# Resume a partial batch — skip IDFs whose parsed CSV already exists
python scripts/simulate.py --idf-dir /Users/ye/nus-energy/idfs/ --workers 8 \
  --gt-dir /Users/ye/nus-energy/ground_truth/parsed/ --skip-existing
```

Summary CSV written to: `outputs/simulation_summary.csv`

Each IDF gets a unique output folder:
- `outputs/FOE13/` — top-level IDF
- `outputs/A1_H_L__FOE13/` — subdirectory variant

**Workers guidance:** Each worker spawns a full EnergyPlus process (~200–400 MB RAM each).
8 workers = ~4× speedup on an 8-core machine. Don't exceed core count.

### Run with MAPE calculation (buildings with ground truth)

When `--gt-dir /Users/ye/nus-energy/ground_truth/parsed/` is supplied, `simulate.py` now prefers the canonical 2024 wide table automatically and uses the parsed per-building files only as fallback.

```bash
python scripts/simulate.py --idf /Users/ye/nus-energy/idfs/FOE13.idf --gt-dir /Users/ye/nus-energy/ground_truth/parsed/
```

> Note: The ground truth CSV must first be parsed from wide format to per-building CSVs.
> Use the helper script owned by AnomalyAgent: `nus-groundtruth/scripts/prepare_ground_truth.py`

### Parse ground truth into per-building CSVs

```bash
cd /Users/ye/nus-energy
python3 /Users/ye/.openclaw/workspace-anomalyagent/skills/nus-groundtruth/scripts/prepare_ground_truth.py
```

This reads `/Users/ye/nus-energy/ground_truth/ground-truth.csv` and writes:
- `/Users/ye/nus-energy/ground_truth/parsed/{BUILDING}_ground_truth.csv`
  with columns: `month` (YYYY-MM), `measured_kwh`

## Meter Output Format

EnergyPlus 23 outputs meter data as **ESO format** (`.mtr`), **not** a CSV.
- Raw file: `outputs/{BUILDING}/simulation/{BUILDING}out.mtr`
- `parse_results()` in `simulate.py` produces a clean monthly CSV from this
- Monthly CSV parsing and anomaly detection is handled downstream by **AnomalyAgent**

## Reading Monthly Output

After a simulation, load the parsed CSV:

```python
import pandas as pd
df = pd.read_csv("outputs/FOE13/parsed/FOE13_monthly.csv")
# Columns: month, month_name, electricity_facility_kwh, cooling_electricity_kwh,
#          other_electricity_kwh, carbon_tco2e, eui_kwh_m2
# eui_kwh_m2 is populated automatically from building_registry.json (floor_area_m2)
# Annual EUI = df["eui_kwh_m2"].sum()
```

## EUI (Energy Use Intensity)

- Unit: **kWh/m²/year** (annual sum of monthly `eui_kwh_m2`)
- Calculated using `floor_area_m2` from `building_registry.json`
- Included in `simulation_summary.csv` as `annual_eui_kwh_m2`
- If the building is not in the registry, `eui_kwh_m2` will be `null`


## Post-Simulation: Slack Report

After every simulation (single or batch), send a Slack notification to **#openclaw-alerts** with:
- Building name
- Annual electricity (kWh)
- Annual EUI (kWh/m²/yr) + BCA Green Mark tier
- MAPE and calibration status (if ground truth available)
- Carbon footprint (tCO₂e/yr)

Use the **"Simulation Results"** template in the `nus-notify` skill.

For **batch runs**, send one consolidated message covering all buildings, grouping by calibration status.

## Checking Simulation Errors

EnergyPlus error file is at: `outputs/{BUILDING}/simulation/{BUILDING}out.err`

Look for lines starting with `** Fatal **` or `** Severe **`.

```bash
grep -E "Fatal|Severe" outputs/FOE13/simulation/FOE13out.err
```

## Ground Truth Comparison Policy

`calculate_mape()` now follows this policy:
1. Prefer the canonical wide file `ground_truth/ground-truth.csv`
2. Use the building's explicit **2024** monthly row when present
3. Otherwise use parsed per-building ground truth and select **2024-only** rows if available
4. Only average across years when no 2024 rows exist

This avoids mixed-year month averaging when a clean 2024 comparison basis is available.

## MAPE Calculation

The `calculate_mape()` function in `simulate.py` returns:
```json
{
  "building": "FOE13",
  "mape": 18.3,
  "cvrmse": 24.1,
  "calibrated": false,
  "n_months": 12,
  "comparison": "<DataFrame with per-month breakdown>"
}
```

ASHRAE Guideline 14 pass criteria (as implemented in code):
- **CVRMSE ≤ 15%** — must pass
- **NMBE ≤ ±5%** — must pass
- MAPE is logged as informational only; not used for `calibrated=True`

## Inline Python Usage

To run a simulation programmatically from within an agent:

```python
import sys

SKILL_DIR = "/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate"
sys.path.insert(0, f"{SKILL_DIR}/scripts")
from simulate import prepare_idf, run_simulation, parse_results, calculate_mape
from pathlib import Path

building = "FOE13"
idf_path  = f"/Users/ye/nus-energy/idfs/{building}.idf"  # or a variant subdir path
epw_path  = "/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw"
output_dir = "/Users/ye/nus-energy/outputs"

prepared   = prepare_idf(idf_path, epw_path, output_dir)
sim_result = run_simulation(prepared, epw_path, output_dir)
parsed     = parse_results(sim_result, output_dir)

# MAPE (metered buildings only)
gt_path = f"/Users/ye/nus-energy/ground_truth/parsed/{building}_ground_truth.csv"
if Path(gt_path).exists():
    result = calculate_mape(parsed["monthly_df"], gt_path, building)
    print(f"MAPE: {result['mape']}%  CVRMSE: {result['cvrmse']}%  Calibrated: {result['calibrated']}")
```

## Interpreting Results

| MAPE | Status | Action |
|---|---|---|
| CVRMSE ≤ 15% and NMBE ≤ ±5% | ✅ Calibrated | No action needed |
| CVRMSE 15–30% or NMBE 5–10% | ⚠️ Warning | Pass to DiagnosisAgent |
| CVRMSE > 30% or NMBE > 10% | 🔴 Critical | Immediate DiagnosisAgent + Slack alert |

## ⛔ ABSOLUTE PROHIBITION — Cooling Setpoints

**Cooling setpoints must NEVER be changed by any agent, at any stage, for any reason.**

- There is no NUS-wide 23°C setpoint policy. IDF setpoints reflect each building's actual operational settings.
- Do NOT apply any "pre-flight correction", "NUS policy fix", or any other modification to cooling setpoints.
- Do NOT hallucinate setpoint changes that were never written. If the IDF was not modified, do not claim it was.
- If setpoints appear unusual, flag it in the Slack notification — do not touch them.
- Violations invalidate the calibration result. Any run that changed setpoints must be re-run from the original IDF.

**Allowed calibration variables (Chisel only, after Slack approval):**
1. Infiltration_ACH
2. Equipment_W_per_m2
3. Lighting_W_per_m2

## Known IDF Adjustments Applied by `simulate.py`

| Adjustment | Behaviour |
|---|---|
| Cooling setpoint | **Preserved as-is — NEVER modified. See prohibition above.** |
| Infiltration < 0.5 ACH | Auto-raised to 0.5 ACH (tropical baseline); values ≥ 0.5 left unchanged |
| Missing ZoneControlTypeSchedule | Auto-created as `SCHEDULE:CONSTANT` with value 4 (DualSetpoint) |
| Ground temperatures | Set to 28°C year-round (Singapore baseline) |
| MRT ZoneAveraged → EnclosureAveraged | Only applied when EnergyPlus ≥ 24 is detected |
| VERSION stamp | Only updated to match installed EP version when EP ≥ 25 is detected |

> **Current install:** EnergyPlus 23-1-0. The MRT and VERSION patches are **skipped** for EP23.

## Common Errors

| Error | Likely Cause | Fix |
|---|---|---|
| `IDD file not found` | EnergyPlus not at `/Applications/EnergyPlus-23-1-0/` | Update `CONFIG['energyplus_dir']` in `scripts/simulate.py` |
| `No IDF files found` | Wrong `idf_dir` | Check `CONFIG['idf_dir']` or pass `--idf-dir` |
| `Not valid for this zone` | Missing thermostat schedule | Already auto-fixed; if persists, check IDF manually |
| Simulation timeout >3600s | Something very wrong | Check .err file for FATAL |
| Empty meter output | Output:Meter not injected | Re-run prepare_idf step |

## Output File Structure

```
/Users/ye/nus-energy/outputs/
  simulation_summary.csv          ← batch run summary (all IDFs; has run_id, building, variant columns)
  FOE13/                          ← top-level IDF (run_id = "FOE13")
    prepared/
      FOE13_prepared.idf
    simulation/
      FOE13out.mtr
      FOE13out.err
    parsed/
      FOE13_monthly.csv
      FOE13_mape_comparison.csv   ← if GT available
  A1_H_L__FOE13/                  ← subdirectory variant (run_id = "A1_H_L__FOE13")
    prepared/
      FOE13_prepared.idf
    simulation/
      FOE13out.mtr
      FOE13out.err
    parsed/
      A1_H_L__FOE13_monthly.csv
      A1_H_L__FOE13_mape_comparison.csv
  calibration_log.md              ← RecalibrationAgent writes here (at $NUS_PROJECT_DIR/calibration_log.md)
```
