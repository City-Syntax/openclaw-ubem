# TOOLS.md — CarbonAgent

## Project

| Item | Value |
|---|---|
| `NUS_PROJECT_DIR` | `/Users/ye/nus-energy` |
| Workspace | `/Users/ye/.openclaw/workspace-carbonagent/` |
| All-buildings IDF dir | `$NUS_PROJECT_DIR/idfs/` |
| Calibrated IDF dir | `$NUS_PROJECT_DIR/outputs/{building}/calibrated/` |
| Parsed monthly CSV | `$NUS_PROJECT_DIR/outputs/{building}/parsed/{building}_monthly.csv` |
| Carbon output dir | `$NUS_PROJECT_DIR/outputs/{building}/carbon/` |
| Carbon output file | `$NUS_PROJECT_DIR/outputs/{building}/carbon/{building}_carbon_scenarios.json` |
| Building registry | `$NUS_PROJECT_DIR/building_registry.json` |

## Constants

| Constant | Value | Source |
|---|---|---|
| Singapore grid emission factor | `0.4168 kgCO2e/kWh` | EMA 2023 |
| Cooling fraction (IdealLoads) | `0.55` | Estimated, SG climate |
| BCA Green Mark Gold EUI | `115 kWh/m²/year` | BCA 2021 |
| NUS carbon neutrality target | `2030` | NUS Sustainability |
| Interaction discount (multi-lever) | `0.15` (15%) | Engineering rule of thumb |

## Intervention parameter targets (from IDF)

| # | IDF object | Field | Typical current | Typical target |
|---|---|---|---|---|
| 1 | `Schedule:Constant` → `Zone_nCooling_SP_Sch` | `value` | 25 °C | 26–27 °C occupied / 28 °C unoccupied |
| 2 | `Lights` | `LightingPowerDensity` | 9 W/m² | 5–6 W/m² |
| 3 | `Daylighting:Controls` | (new object) | not present | inject per zone |
| 4 | `WindowProperty:ShadingControl` | (new object) | not present | inject per window |
| 5 | `ZoneHVAC:IdealLoadsAirSystem` | (system swap) | IdealLoads | real VRF/chiller object |
| 6 | `Generator:Photovoltaic` | (new object) | not present | inject for roof surfaces |
| 7 | `Material` (outer roof/wall layer) | `SolarAbsorptance` | 0.7 | 0.25 (roof) / 0.4 (wall) |
| 8 | Wall `Construction` | add vegetation layer | not present | inject `Material:RoofVegetation`-style layer |

## Patch complexity legend

| Level | Meaning | Who handles it |
|---|---|---|
| `param_patch` | Change a single field value | Chisel 🔧 (auto) |
| `new_object` | Inject a new IDF object block | Chisel 🔧 (with template) |
| `system_swap` | Replace entire HVAC subsystem | Manual / out of scope |

## Data sources to read

| File | What it contains |
|---|---|
| `outputs/{B}/parsed/{B}_monthly.csv` | Monthly simulated kWh + carbon |
| `outputs/{B}/calibrated/{B}.idf` | Post-calibration IDF (preferred) |
| `idfs/{B}.idf` | Raw baseline IDF (fallback) |
| `building_registry.json` | Floor areas (m²), building type, year built |
| `outputs/{B}/{B}_calibration_metrics.json` | MAPE, CVRMSE — confirms simulation quality |

## Carbon output format

```
outputs/{building}/carbon/{building}_carbon_scenarios.json
```

Full schema defined in SOUL.md.

## Python dependencies

```
pandas, pathlib, json, re
```

No additional installs required — uses system Python 3.9.
