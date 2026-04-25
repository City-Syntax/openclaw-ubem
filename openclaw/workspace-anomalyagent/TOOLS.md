# TOOLS.md — AnomalyAgent

## Project

| Item | Value |
|---|---|
| NUS_PROJECT_DIR | `/Users/ye/nus-energy` |
| GT buildings | 23 total across 5 archetypes (see SOUL.md for full list) |

## Data sources to read

| File | What it contains |
|---|---|
| `outputs/{B}/{B}_calibration_metrics.json` | MAPE, MBE, CVRMSE, pass/fail |
| `outputs/{B}/parsed/{B}_monthly.csv` | Monthly simulated kWh |
| `building_registry.json` | Floor areas for EUI calculation |

## Anomaly report output

```
outputs/anomaly_report_{YYYY-MM-DD}.json
```

## ASHRAE Guideline 14 thresholds (pass/fail gate)

| Metric | Threshold | Role |
|---|---|---|
| CV(RMSE) | ≤ 15% | Pass/fail |
| NMBE | within ±5% | Pass/fail |

## Run fresh metrics before scanning

```bash
cd /Users/ye/nus-energy
python3 prepare_ground_truth.py
```
