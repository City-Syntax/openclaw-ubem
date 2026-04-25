---
name: nus-query
description: Answer natural language questions about NUS campus energy performance, carbon footprint, BCA benchmarks, building comparisons, and cost savings. Use for any question about what the data shows — EUI rankings, carbon totals, savings estimates, calibration summaries.
metadata: {"openclaw": {"emoji": "🔍", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Query Skill

## Trigger phrases
Any question about campus energy data:
"which building is most efficient", "total campus carbon", "how much can we save",
"EUI ranking", "which buildings need calibration", "BCA Gold gap",
"worst performing building", "what is the campus footprint",
"how does FOE6 compare to FOE13", "cost of energy waste"

## Data sources to read
1. `outputs/*/parsed/*_monthly.csv` — simulated monthly kWh per building
2. `outputs/*/*_calibration_metrics.json` — ASHRAE pass/fail + measured vs simulated
3. `outputs/*/ground_truth/*_ground_truth.csv` — actual meter kWh
4. `building_registry.json` — floor areas (m²) per building

## How to answer

### EUI ranking question
Read all available `*_monthly.csv` files, compute annual kWh, divide by floor_area_m2
from building_registry.json. Rank ascending. Flag BCA tier for each.

### Carbon footprint question
Sum annual kWh across all buildings. Multiply by 0.4168 kgCO2e/kWh ÷ 1000 = tCO2e.
Also compute per-building contribution and identify the top 3 emitters.

### "How much can we save" question
For each building above BCA Gold (115 kWh/m²):
  savings_kwh = (current_eui - 115) × floor_area_m2
  savings_sgd = savings_kwh × 0.28
  savings_carbon = savings_kwh × 0.4168 / 1000  # tCO2e

### Calibration status question
Read all `*_calibration_metrics.json`. Show pass/fail table. List buildings needing action.

## Response format (Slack-friendly)
Always lead with the direct answer, then supporting data:

```
🔍 Campus carbon footprint (simulated, 2024):

Total: 4,821 tCO2e/year  |  Grid: 0.4168 kgCO2e/kWh

Top emitters:
  FOS43    1,240 tCO2e  (25.7%)
  FOE18      987 tCO2e  (20.5%)
  FOE6       834 tCO2e  (17.3%)

BCA Platinum target: save 1,120 tCO2e/year vs current
```

## When data is missing
If a building's CSV doesn't exist yet, say:
"No simulation data for {BUILDING} — run `nus-simulate` + `nus-parse` first."

Never invent numbers. Mark missing data as missing.

## Comparison questions (e.g. "FOE6 vs FOE13")
Show side-by-side:
```
         FOE6        FOE13
EUI:     142 kWh/m²  118 kWh/m²
Status:  ⚠️ over BCA Gold  ✅ BCA Gold
MAPE:    24.1%       11.2%
Action:  Calibrate   Monitor
```
