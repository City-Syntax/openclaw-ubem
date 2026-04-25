---
name: nus-registry
description: Generate or refresh the NUS building registry (building_registry.json) by scanning all IDF files. Use when IDF files change, when a new building is added, or when asked about building metadata, floor areas, zone counts, or IDF parameters. Also used for pre-flight registry validation before simulation. Owned by SimulationAgent (Forge).
metadata: {"openclaw": {"emoji": "🏛️", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Registry Skill

## Scripts

| Script | Purpose |
|---|---|
| `scripts/generate_registry.py` | Full registry builder — merges IDF params with static metadata → `building_registry.json` |
| `scripts/extract_idf_params.py` | **Standalone IDF parameter extractor** — robust eppy-based extraction, designed to scale to 300+ buildings |
| `scripts/enrich_registry_shp.py` | **Shapefile enricher** — merges NUS campus geometry (WWR, floors, archetype, height) from `QGISFIle/MasterFile_241127.dbf` into `building_registry.json` |

## Trigger phrases
"regenerate the registry", "update building registry", "add new building",
"what is the floor area of FOE6", "how many zones does FOS46 have",
"refresh registry after IDF change", "building_registry.json is stale"

## What it does
Scans every `.idf` file in `$NUS_PROJECT_DIR/idfs/` and extracts:
- `zone_count` — number of Zone objects
- `floor_area_m2_idf` — sum of all zone floor areas (m²)
- `cooling_setpoint_c` — first CoolingSetpoint schedule value
- `infiltration_ach` — first InfiltrationAch value
- `lighting_w_m2` — Lights Watts_per_Zone_Floor_Area
- `equipment_w_m2` — ElectricEquipment Watts_per_Zone_Floor_Area
- `people_density_per_m2` — People density field

Static metadata (full_name, faculty, type, HVAC system, occupancy, green mark target)
is defined in `BUILDING_META` inside the script — update there when metadata changes.

## Run — pre-flight (single building, fast)

Use this before every simulation to ensure the building has a registry entry with floor area.
Idempotent — no-op if entry already exists and floor_area_m2 is non-null.

```bash
# By building stem (auto-finds IDF in idfs/, including subdirectories)
NUS_PROJECT_DIR=/Users/ye/nus-energy \
  python3 {SKILL_DIR}/scripts/generate_registry.py --building FOE5

# By explicit IDF path (use when simulating a subdirectory variant)
NUS_PROJECT_DIR=/Users/ye/nus-energy \
  python3 {SKILL_DIR}/scripts/generate_registry.py \
    --idf /Users/ye/nus-energy/idfs/A1_L_L/FOE5.idf
```

Exit code: `0` if registry is good, `1` if floor area unavailable (warn-and-proceed: don't block simulation).

## Run — full batch registry rebuild

Scans **all IDF files recursively** (top-level + all subdirectory variants).
For buildings with multiple variants (e.g. `idfs/FOE5.idf` and `idfs/A1_L_L/FOE5.idf`),
the top-level IDF takes precedence.

```bash
NUS_PROJECT_DIR=/Users/ye/nus-energy \
  python3 {SKILL_DIR}/scripts/generate_registry.py
```

```bash
# Preview without writing
NUS_PROJECT_DIR=/Users/ye/nus-energy \
  python3 {SKILL_DIR}/scripts/generate_registry.py --dry-run
```

## Run — standalone IDF parameter extractor

```bash
# Single building
python3 {SKILL_DIR}/scripts/extract_idf_params.py /path/to/FOE13.idf --pretty

# All buildings in a directory → JSON + CSV
python3 {SKILL_DIR}/scripts/extract_idf_params.py \
  --dir $NUS_PROJECT_DIR/idfs \
  --out $NUS_PROJECT_DIR/idf_params.json \
  --csv --pretty

# Custom IDD path
python3 {SKILL_DIR}/scripts/extract_idf_params.py \
  --dir $NUS_PROJECT_DIR/idfs \
  --idd /Applications/EnergyPlus-23-1-0/Energy+.idd \
  --out $NUS_PROJECT_DIR/idf_params.json
```

### What `extract_idf_params.py` extracts per building

| Field | Description |
|---|---|
| `zone_count` | Number of Zone objects |
| `floor_area_m2` | Sum of all Zone.Floor_Area × multiplier (m²) |
| `total_volume_m3` | Sum of all Zone.Volume × multiplier (m³) |
| `lighting_w_m2` | Mean Lights Watts_per_Zone_Floor_Area |
| `equipment_w_m2` | Mean ElectricEquipment Watts_per_Zone_Floor_Area |
| `people_per_m2` | Mean People_per_Floor_Area |
| `infiltration_ach` | Mean Air_Changes_per_Hour (AirChanges/Hour method) |
| `infiltration_m3s_m2` | Mean Flow_Rate_per_Floor_Area (Flow/Area method) |
| `cooling_setpoint_c` | Cooling setpoint, resolved through Schedule:Constant/Compact |
| `heating_setpoint_c` | Heating setpoint, resolved through Schedule:Constant/Compact |

## Output
- `$NUS_PROJECT_DIR/building_registry.json` — 23 buildings with full metadata + IDF-extracted params
- Adds `has_ground_truth: true` for the 5 metered buildings: FOE6, FOE9, FOE13, FOE18, FOS43, FOS46

## Run — shapefile enricher

```bash
NUS_PROJECT_DIR=/Users/ye/nus-energy \
  python3 {SKILL_DIR}/scripts/enrich_registry_shp.py
```

```bash
# Preview without writing
NUS_PROJECT_DIR=/Users/ye/nus-energy \
  python3 {SKILL_DIR}/scripts/enrich_registry_shp.py --dry-run
```

Adds these `shp_*` fields to each building entry (matched by building ID):

| Field | Source | Use |
|---|---|---|
| `shp_name_2` | `Name_2` | Human-readable building name |
| `shp_archetype` | `Archetype` | Faculty / Research / Lecture Theatre / Ancillary / Residences |
| `shp_floors_ag` | `floors_ag` | Above-ground floor count |
| `shp_floors_bg` | `floors_bg` | Below-ground floor count |
| `shp_floor_height` | `floor_hei` | Avg floor-to-floor height (m) |
| `shp_ag_height` | `ag_height` | Above-ground building height (m) |
| `shp_wwr_pct` | `WWR (%)` | Window-to-Wall Ratio — key calibration input for RecalibrationAgent |

Safe: existing fields are never removed. Run after `generate_registry.py` when the shapefile changes.

## When to re-run
- After `patch_idf.py` changes parameters (setpoint, ACH, etc.)
- After any IDF file is replaced or added
- After a new building is onboarded
- After the shapefile is updated (`enrich_registry_shp.py` only)

## Adding a new building
1. Add its IDF to `$NUS_PROJECT_DIR/idfs/` (top-level or subdirectory)
2. Optionally add static metadata to `BUILDING_META` in `scripts/generate_registry.py` (full_name, faculty, type, etc.)
3. Run pre-flight: `python3 {SKILL_DIR}/scripts/generate_registry.py --building <STEM>`
   — this is sufficient for simulation to proceed with EUI
4. Re-run full batch scan at next convenient time to refresh all entries

## Key Paths
| What | Path |
|---|---|
| Script | `SKILL_DIR/scripts/generate_registry.py` |
| IDF source | `$NUS_PROJECT_DIR/idfs/` |
| Output | `$NUS_PROJECT_DIR/building_registry.json` |
| Static metadata | `BUILDING_META` dict inside the script |
