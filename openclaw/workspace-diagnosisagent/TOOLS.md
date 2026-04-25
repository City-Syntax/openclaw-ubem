# TOOLS.md — DiagnosisAgent

## Project

| Item | Value |
|---|---|
| NUS_PROJECT_DIR | `/Users/ye/.openclaw` |
| Matched buildings | FOE6, FOE13, FOE18, FOS43, FOS46 |

## Data sources to read

| File | What it contains |
|---|---|
| `outputs/{B}/{B}_calibration_metrics.json` | MAPE, MBE, CVRMSE, ratio, pass/fail |
| `outputs/{B}/parsed/{B}_monthly.csv` | Monthly simulated kWh — reveals seasonal bias |
| `outputs/{B}/{B}_ground_truth.csv` | Measured kWh — compare month by month |
| `building_registry.json` | Floor area, building type |
| `parameter_bounds.json` | IDF baseline values + safe ranges |
| `calibration_log.md` | Previous iterations — never repeat a rejected change |

## Priority fix order (always try in this sequence)

1. `Cooling_Setpoint_C`: 25.0 → 23.0 (non-lab) or 22.0 (lab)
2. `Infiltration_ACH`: 6.0 → 0.5 (modern sealed buildings)
3. `Equipment_W_per_m2`: adjust based on building type
4. `Lighting_W_per_m2`: adjust if still over-predicting after steps 1–3

## Building type reference

| Building | Type | Equipment W/m2 target |
|---|---|---|
| FOE6 | Engineering faculty — mixed lab/office | 20–35 |
| FOE13 | Engineering faculty — mixed lab/office | 20–35 |
| FOE18 | Engineering faculty — mixed lab/office | 20–35 |
| FOS43 | Science faculty — wet/dry labs | 35–55 |
| FOS46 | Science faculty — wet/dry labs | 35–55 |
