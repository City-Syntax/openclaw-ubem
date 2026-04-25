---
name: nus-parse
description: Parse EnergyPlus .mtr output into monthly energy CSV for a NUS building. Use after simulation completes, or when asked to extract monthly kWh, cooling energy, or carbon from simulation results.
metadata: {"openclaw": {"emoji": "📊", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Parse Skill

## Trigger phrases
"parse FOE6", "extract monthly energy", "get kWh for CLB6", "parse all buildings",
"what does the simulation output show", "parse .mtr file"

## Steps

### Single building
```bash
cd $NUS_PROJECT_DIR
python3 /Users/ye/.openclaw/workspace-anomalyagent/skills/nus-parse/scripts/parse_eso.py --building {BUILDING} --outputs outputs
```

### All 6 GT buildings (common workflow)
```bash
cd $NUS_PROJECT_DIR
for b in FOE6 FOE13 FOE18 FOS43 FOS46; do
  python3 /Users/ye/.openclaw/workspace-anomalyagent/skills/nus-parse/scripts/parse_eso.py --building $b --outputs outputs
done
```

## What scripts/parse_eso.py produces
- `outputs/{BUILDING}/parsed/{BUILDING}_monthly.csv`
- Columns: month, month_name, electricity_facility_kwh, cooling_electricity_kwh, other_electricity_kwh, carbon_tco2e

## Constants used (do not change)
- Grid factor: 0.4168 kgCO2e/kWh (EMA Singapore 2023)
- Cooling fraction estimate: 55% of total electricity

## Output format to report back
After parsing, show a compact table:
```
📊 {BUILDING} — monthly energy parsed

Month   kWh (total)   Cooling kWh   Carbon tCO2e
Jan       45,210        24,865         7.54
Feb       42,100        23,155         7.02
...
TOTAL    523,400       287,870        87.30
EUI: {annual_kwh / floor_area_m2:.1f} kWh/m²  |  BCA benchmark: Gold = 115 kWh/m²
```

If the .mtr file doesn't exist, say:
"No simulation output found for {BUILDING}. Run `nus-simulate` first."

## After parsing all 6 GT buildings
Automatically chain to `nus-groundtruth` to compute ASHRAE calibration metrics.
