---
name: nus-intervene
description: Run targeted EnergyPlus intervention simulations for NUS buildings. Patches IDF parameters (cooling setpoint, lighting, equipment loads), re-simulates, and reports actual kWh/carbon savings vs baseline. Use when asked to propose or simulate interventions for a folder or set of buildings.
metadata: {"openclaw": {"always": false}}
---

# nus-intervene — NUS Carbon Intervention Simulator

Patches IDF copies with energy-saving parameter changes, re-runs EnergyPlus, and reports real simulated savings vs baseline.

## Script

```
{SKILL_DIR}/scripts/intervene.py
```

## What It Does

1. Reads current parameter values from each IDF
2. Applies conservative, physics-grounded adjustments:
   - Cooling setpoint +1°C → ~3–6% cooling savings
   - Lighting −15% W/m² → LED retrofit
   - Equipment −10% W/m² → smart scheduling
3. Writes patched IDFs to `outputs/<building>/intervention/idfs/` (originals untouched)
4. Re-runs EnergyPlus via `simulate.py`
5. Compares baseline vs intervention monthly CSVs → actual % savings

## Usage

```bash
python3 {SKILL_DIR}/scripts/intervene.py --folder A1_H_L --target-pct 5
python3 {SKILL_DIR}/scripts/intervene.py --folder A1_H_L --target-pct 5 --buildings FOE6,FOE13
python3 {SKILL_DIR}/scripts/intervene.py --folder A1_H_L --target-pct 5 --dry-run
```

## Environment Variables

| Variable | Description | Default |
|---|---|---|
| `NUS_PROJECT_DIR` | Project data root | `/Users/ye/nus-energy` |
| `SIMULATE_SCRIPT` | Path to simulate.py | workspace-simulationagent path |

## Output

Per building:
- Actual simulated % reduction
- kWh saved, tCO₂e saved, SGD saved
- Parameter changes applied (from → to)

Portfolio total across all buildings in the folder.
