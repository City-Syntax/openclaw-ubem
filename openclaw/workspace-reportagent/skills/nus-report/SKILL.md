---
name: nus-report
description: Generate per-building PDF calibration reports or the campus-wide summary PDF for the Applied Energy paper. Use when asked to generate a report, write up findings, create a calibration log, or produce paper-ready results.
metadata: {"openclaw": {"emoji": "📄", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Report Skill

## Trigger phrases
"generate report for FOE6", "write up the calibration", "campus summary report",
"create PDF", "paper-ready results", "Applied Energy report",
"what changed between iterations", "summarise all changes", "calibration log"

## Script location
`{SKILL_DIR}/scripts/report.py`

The script uses relative paths (`outputs/`, `reports/`, `openclaw_agents/building_registry.json`),
so it **must be run from `$NUS_PROJECT_DIR`**.

## Commands

### Per-building report
```bash
cd $NUS_PROJECT_DIR
python3 {SKILL_DIR}/scripts/report.py --building {BUILDING}
```
Output: `$NUS_PROJECT_DIR/reports/{BUILDING}/{BUILDING}_report.pdf`

### Campus summary report (for Applied Energy paper)
```bash
cd $NUS_PROJECT_DIR
python3 {SKILL_DIR}/scripts/report.py --campus
```
Output: `$NUS_PROJECT_DIR/reports/NUS_Campus_Summary_Report.pdf`

### Open after generation (macOS)
```bash
cd $NUS_PROJECT_DIR
python3 {SKILL_DIR}/scripts/report.py --building {BUILDING} --open
```

## Workflow rules (updated 2026-04-12)
- **Reported EUI** means **total EUI including cooling electrical equivalent**.
- **Base electrical EUI** may appear only as a secondary breakdown.
- **Primary calibration error metrics** are **CVRMSE** and **NMBE**. MAPE is secondary context only.
- Before building a per-building PDF, refresh intervention results first when Compass is available.
- Rebuild per-building chart PNGs fresh for each PDF generation to avoid stale figures.
- Intervention sections in reports must label sources explicitly (`simulated` or `estimated`).

## What each report contains
**Per-building:**
- Simulation vs measured monthly kWh chart
- ASHRAE Guideline 14 metrics table (CVRMSE, NMBE; MAPE secondary)
- Calibration iteration history (from calibration_log.md)
- BCA Green Mark EUI benchmark comparison using reported EUI incl. cooling
- Intervention scenario summary with explicit source labels
- Recommended next actions

**Campus summary:**
- All 5 matched buildings side-by-side
- Campus-wide carbon footprint
- Aggregate ASHRAE pass/fail
- BCA savings potential table
- Methods section suitable for Applied Energy submission

## Calibration log summary
To show what changed across all iterations without generating a full PDF:
```bash
cat $NUS_PROJECT_DIR/reports/{BUILDING}/{BUILDING}_calibration_log.md
```

Then summarise in Slack format:
```
📄 Calibration history — FOE6

Iteration 1 (2026-03-14):
  Infiltration_ACH: 6.0 → 0.5  (approver: @ye)
  Result: CVRMSE 31.2% | NMBE -18.4%  ⚠️ still above threshold

Iteration 2 (2026-03-15):
  Equipment_W_per_m2: 12.0 → 20.0  (approver: @ye)
  Result: CVRMSE 11.2% | NMBE -2.1%  ✅ PASS
```

## Paper-ready output standards
All numbers in reports must:
- Include units (kWh, kWh/m², tCO2e, W/m², °C, ACH)
- Cite sources (EMA 2023 for grid factor, ASHRAE Guideline 14 for thresholds, BCA 2021 for benchmarks)
- Use passive voice in narrative sections
- Report uncertainty where available
