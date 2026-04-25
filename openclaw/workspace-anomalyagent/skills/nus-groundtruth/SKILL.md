---
name: nus-groundtruth
description: Compare simulated energy against canonical NUS ground-truth meter data and compute ASHRAE Guideline 14 calibration metrics (CVRMSE, NMBE) for all GT buildings across 5 archetypes. Produces per-building verification CSVs and per-archetype calibration aggregates used to propagate parameters to non-GT buildings. Use when asked about calibration status, ASHRAE pass/fail, or how well the model matches reality.
metadata: {"openclaw": {"emoji": "📐", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Ground Truth Skill

## Trigger phrases
"calibration status", "ASHRAE metrics", "how accurate is the model", "does FOE6 pass",
"compare simulated vs measured", "CVRMSE for FOE13", "which buildings fail CVRMSE",
"prepare ground truth", "run calibration check", "archetype calibration"

---

## GT Building Coverage (22 buildings across 5 archetypes)

| Archetype | GT Buildings | Description |
|---|---|---|
| A1_H_L | FOE6, FOE13, FOE18, FOS43, FOS46 | Medium-rise academic, high WWR |
| A1_L_L | FOE1, FOE3, FOE5, FOE15, FOE24, FOE26, FOS26 | Medium-rise academic, low WWR |
| A1_M_H | FOS35, FOS41, FOS44 | Medium-rise academic, medium WWR, lab/high equipment |
| A1_M_L | FOE11, FOE12, FOE16, FOE19, FOE20, FOE23 | Medium-rise academic, medium WWR, lecture/office |
| A5 | FOE10 | Podium/ancillary |

Non-GT archetypes (A2, A3, A4, A6) have no meter data — they inherit calibrated parameters
from their nearest GT archetype as defined in `parameter_bounds.json → archetype_profiles`.

---

## Run

```bash
NUS_PROJECT_DIR=/Users/ye/nus-energy python3 /Users/ye/.openclaw/workspace-anomalyagent/skills/nus-groundtruth/scripts/prepare_ground_truth.py
```

This script:
1. Reads canonical meter data from `$NUS_PROJECT_DIR/ground_truth/ground-truth.csv`
2. Filters to overlapping 2024 months
3. Loads parsed simulation CSVs from `$NUS_PROJECT_DIR/outputs/{BUILDING}/parsed/`
4. Converts measured energy to EUI using `building_registry.json`, then compares measured vs simulated on the annual adjusted-total EUI basis
5. Saves outputs per building to `$NUS_PROJECT_DIR/ground_truth/{BUILDING}/`
6. Saves archetype aggregate JSON to `$NUS_PROJECT_DIR/ground_truth/{ARCHETYPE}/{ARCHETYPE}_calibration_aggregate.json`

---

## Output files

### Per building (`ground_truth/{BUILDING}/`)
- `{BUILDING}_ground_truth.csv` — canonical 2024 monthly meter time-series (month, measured_kwh)
- `{BUILDING}_verification.csv` — averaged measured vs simulated, per-month error + ASHRAE flag
- `{BUILDING}_calibration_metrics.json` — CVRMSE/NMBE, ASHRAE pass/fail, archetype label

### Per archetype (`ground_truth/{ARCHETYPE}/`)
- `{ARCHETYPE}_calibration_aggregate.json` — avg CVRMSE/NMBE across all GT buildings, representative building

### Ground truth time-series (`ground_truth/parsed/`)
- `{BUILDING}_ground_truth.csv` — one row per month extracted from canonical 2024 ground truth
- `ground-truth.csv` is the canonical comparison source

---

## Verification CSV columns
| Column | Description |
|---|---|
| `building` | Building ID |
| `archetype` | Archetype folder (e.g. A1_H_L) |
| `month` | Month number (1–12) |
| `month_name` | Jan–Dec |
| `measured_avg_kwh` | Measured annual EUI for overlapping 2024 months |
| `simulated_kwh` | Simulated annual adjusted-total EUI on the same basis |
| `error_kwh` | simulated − measured (kWh) |
| `error_pct` | (simulated − measured) / measured × 100 |
| `abs_error_pct` | Absolute error % |
| `ashrae_flag` | `OK` (<10%), `WARN` (10–20%), `FAIL` (>20%) |

---

## Comparison Policy

- Use `ground-truth.csv` as the calibration source
- Restrict comparison to overlapping 2024 months only
- Convert measured monthly kWh to EUI using `building_registry.json -> floor_area_m2`
- Compare measured vs simulated using the same adjusted-total EUI basis (`eui_adj_kwh_m2`)
- Reduce the monthly series to an annual comparison point before computing the gate metrics

## ASHRAE Guideline 14 thresholds
| Metric | Pass (must) | Notes |
|--------|-------------|-------|
| CVRMSE | ≤ 15% (monthly) | Primary pass/fail criterion |
| NMBE   | ≤ ±5% (monthly) | Primary pass/fail criterion |
| MAPE   | informational only | Reported but not used for pass/fail |

---

## Output format — report as a summary table

```
📐 ASHRAE Guideline 14 — Calibration Status

Archetype    Building   CVRMSE   NMBE      Status
A1_H_L       FOE6       41.3%   +40.5%   ⚠️ CALIBRATE
A1_H_L       FOE13       9.4%    -2.8%   ✅ PASS
...
── A1_H_L aggregate:  CVRMSE=X.X%  NMBE=±X.X%  ⚠️ CALIBRATE
```

---

## Calibration propagation logic

After calibrating GT buildings in an archetype:
1. Compute archetype aggregate (average metrics, representative building)
2. For non-GT buildings in the **same archetype**: apply the same calibrated
   parameter values (from `parameter_bounds.json → archetype_profiles`)
3. For non-GT archetypes (A2/A3/A4/A6): use the `_inherit_from` archetype's
   calibrated parameters as a starting point

The calibrated targets per archetype are maintained in `$NUS_PROJECT_DIR/parameter_bounds.json`
under `archetype_profiles`. RecalibrationAgent updates these after each convergence.

---

## Diagnosis rules (apply after computing metrics)

- **ratio > 1.20** (over-predicts >20%):
  1. Correct cooling setpoint 25°C → 23°C (highest confidence)
  2. Reduce Infiltration ACH 6.0 → 0.5
  3. Check Equipment_W_m2 vs archetype profile in parameter_bounds.json

- **ratio < 0.80** (under-predicts >20%):
  1. Increase Equipment_W_m2 (especially for labs: check archetype)
  2. Extend HVAC operating hours
  3. Check if building has 24/7 lab base load

- **CVRMSE > 15% but ratio near 1.0**: seasonal mismatch → check occupancy schedule

If any building fails, offer to invoke `nus-calibrate` with recommended fixes.
Always require human approval before any IDF change.

---

## Weather context from Nimbus (anomaly diagnosis)

When flagging anomalies, enrich the diagnosis with observed weather context from Nimbus:

```python
import json
from pathlib import Path

latest = Path("/Users/ye/nus-energy/weather/observed/latest_conditions.json")
if latest.exists():
    conditions = json.loads(latest.read_text())
    dbt   = conditions["conditions"].get("DBT")    # °C — actual dry-bulb temp
    rh    = conditions["conditions"].get("RH")     # % — actual relative humidity
    source = conditions.get("source", "unknown")
```

Also check the quality report for the month under investigation:
```python
quality_report = Path(f"/Users/ye/nus-energy/weather/observed/{month}_quality_report.json")
if quality_report.exists():
    report = json.loads(quality_report.read_text())
    dbt_delta = report["variables"].get("DBT", {}).get("delta_c")  # observed vs MSS normal
    quality   = report.get("data_quality")  # "good" | "drift_alert" | "poor"
```

**Use in anomaly narrative**: if observed DBT is >2°C above MSS normal for the month, flag
it as a likely contributor to elevated cooling consumption (reduces false-positive calibration triggers).
