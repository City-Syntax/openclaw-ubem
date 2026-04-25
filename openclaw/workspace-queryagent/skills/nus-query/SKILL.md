---
name: nus-query
description: Answer ad-hoc questions about NUS campus energy performance by reading pipeline output data. Use for questions about MAPE, calibration status, energy intensity, carbon footprint, BCA benchmarks, cost savings, and building comparisons. Always ground answers in real data files.
metadata: {"openclaw": {"always": false}}
---

# nus-query — NUS Energy Query Skill

Reads pipeline outputs and answers freeform questions from the facilities team.

---

## Data Paths

| Data | Path |
|---|---|
| Monthly simulated kWh | `/Users/ye/nus-energy/outputs/{building}/parsed/{building}_monthly.csv` |
| Ground truth meter | `/Users/ye/nus-energy/ground_truth/` |
| Building registry | `/Users/ye/nus-energy/building_registry.json` |
| Parameter bounds | `/Users/ye/nus-energy/parameter_bounds.json` |
| Calibrated IDF | `/Users/ye/nus-energy/outputs/{building}/prepared/{building}_prepared.idf` |

Buildings with ground truth (MAPE-capable): **FOE6, FOE9, FOE13, FOE18, FOS43, FOS46**

---

## Query Script

Use `{SKILL_DIR}/scripts/query.py` for data-heavy questions requiring CSV reads, aggregation, or cross-building comparison.

```bash
python3 {SKILL_DIR}/scripts/query.py --question "which building has highest MAPE"
python3 {SKILL_DIR}/scripts/query.py --building FOE13 --metric mape
python3 {SKILL_DIR}/scripts/query.py --summary  # campus-wide overview
```

---

## Answer Patterns

### Single building status
```
FOE13 — MAPE 18.3% ⚠️
Simulated: 245 kWh/m²/year | Measured: 201 kWh/m²/year
Last simulation: 2026-03-19
Calibration status: recalibration pending
```

### Cross-building MAPE ranking
```
MAPE ranking (5 calibrated buildings):
1. FOE6   — 9.2%  ✅
2. FOS46  — 11.4% ✅
3. FOS43  — 14.8% ✅
4. FOE18  — 16.1% ⚠️
5. FOE13  — 18.3% ⚠️
```

### Campus energy intensity
```
Campus average: 198 kWh/m²/year (23 buildings simulated)
BCA Gold threshold: 115 kWh/m²/year
Gap to Gold: 83 kWh/m²/year avg — significant HVAC optimisation needed
```

### Carbon / cost estimate
```
FOE13 annual consumption: ~2,450,000 kWh
Carbon: ~1,021 tCO2e/year (@ 0.4168 kgCO2e/kWh)
Cost: ~SGD 686,000/year (@ SGD 0.28/kWh)
```

---

## Handling Missing Data

- If a building's `_monthly.csv` doesn't exist → "No simulation output found for {building}. Run Forge first."
- If ground truth is missing for a building → "MAPE unavailable — {building} has no meter data."
- If outputs dir is empty → "Pipeline has not run yet for this building."

Never fabricate numbers. Always check file existence before reading.

---

## Domain Constants (always apply)

| Constant | Value |
|---|---|
| Grid carbon factor | 0.4168 kgCO2e/kWh |
| Electricity tariff | SGD 0.28/kWh |
| BCA Platinum | ≤85 kWh/m²/year |
| BCA Gold Plus | ≤100 kWh/m²/year |
| BCA Gold | ≤115 kWh/m²/year |
| BCA Certified | ≤130 kWh/m²/year |
| MAPE target | <15% (ASHRAE G14) |
| CVRMSE threshold | ≤30% |
| MBE threshold | ≤±10% |
