# TOOLS.md — SimulationAgent

## Project

| Item | Value |
|---|---|
| NUS_PROJECT_DIR | `/Users/ye/.openclaw` |
| GT buildings | 23 total — see nus-soul SKILL.md for full list |
| All buildings | 23 IDF files in `idfs/` |

## EnergyPlus

```bash
# Confirm on PATH before any run
which energyplus

# If not found, add to shell:
export PATH="/Applications/EnergyPlus-23-1-0:$PATH"

# Check version
energyplus --version
```

Expected version: EnergyPlus 23.1.0

## Key paths

| Path | Purpose |
|---|---|
| `idfs/{BUILDING}.idf` | Input IDF for simulation |
| `weather/SGP_Singapore.486980_IWEC.epw` | Singapore TMY weather file |
| `outputs/{BUILDING}/simulation/` | Raw EnergyPlus output |
| `outputs/{BUILDING}/simulation/eplusout.err` | Error log — check on failure |
| `outputs/{BUILDING}/parsed/{BUILDING}_monthly.csv` | Parsed monthly kWh |

## Scripts

| Script | Purpose |
|---|---|
| `simulate.py` | Runs EnergyPlus for one or all buildings |
| `parse_eso.py` | Parses .mtr output into monthly CSV |

## Parse output columns

`month, month_name, electricity_facility_kwh, cooling_electricity_kwh,
other_electricity_kwh, carbon_tco2e`

Grid factor: 0.4168 kgCO2e/kWh (EMA Singapore 2023)
Cooling fraction estimate: 55% of total electricity
