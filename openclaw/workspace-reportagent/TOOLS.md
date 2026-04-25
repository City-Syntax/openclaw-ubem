# TOOLS.md — ReportAgent

## Project

| Item | Value |
|---|---|
| NUS_PROJECT_DIR | `/Users/ye/.openclaw` |
| Matched buildings | FOE6, FOE13, FOE18, FOS43, FOS46 |

## Data sources to read

| File | What it contains |
|---|---|
| `outputs/{B}/parsed/{B}_monthly.csv` | Simulated monthly kWh |
| `outputs/{B}/{B}_calibration_metrics.json` | MAPE, MBE, CVRMSE, pass/fail |
| `outputs/{B}/{B}_ground_truth.csv` | Measured monthly kWh |
| `calibration_log.md` | Full iteration history |
| `building_registry.json` | Floor areas for EUI calculation |

## Output paths

| Output | Path |
|---|---|
| Per-building PDF | `reports/{BUILDING}/{BUILDING}_report.pdf` |
| Campus summary PDF | `reports/NUS_Campus_Summary_Report.pdf` |

## Constants to cite in every report

| Constant | Value | Source |
|---|---|---|
| Grid carbon factor | 0.4168 kgCO2e/kWh | EMA Singapore 2023 |
| Electricity tariff | SGD 0.28/kWh | SP Group commercial ~2024 |
| BCA Gold threshold | 115 kWh/m²/year | BCA Green Mark 2021 |
| BCA Platinum threshold | 85 kWh/m²/year | BCA Green Mark 2021 |
| CVRMSE limit | ≤ 15% | Current detector gate |
| NMBE limit | ≤ ±5% | Current detector gate |

## Scripts

| Script | Purpose |
|---|---|
| `report.py --building {B}` | Per-building PDF |
| `report.py --campus` | Campus summary PDF |
