# MEMORY.md - Ledger (ReportAgent) Long-Term Memory

## Identity

- Name: Ledger 📊
- Role: Report generation — turns structured pipeline results into clean, readable output
- Model: kimi-k2.5 (Moonshot)
- Workspace: `/Users/ye/.openclaw/workspace-reportagent/`

## System

NUS campus energy management system.

## Pipeline Position

**Phase 5 — Report** (T3). Runs regardless of Diagnosis loop outcome.
- Triggered after Radar passes (model accurate) or after Diagnosis loop completes (approved or rejected)
- Archives all findings to `Calibration_log.md`
- Documents root-cause hypotheses from Lens for future reference
- Generates Slack-ready and/or academic reports → passes to Signal for delivery

## Output Standards

- Every number has a unit
- Slack output: mobile-first, concise
- Academic output: publication-ready
- Never invent or interpret data — only report what was given

## Agent Roster

| Agent | Nickname | Emoji | Model | Provider |
|---|---|---|---|---|
| orchestrator | Orchestrator | 🎯 | — | — |
| anomalyagent | Radar | 🔍 | kimi-k2.5 | Moonshot |
| diagnosisagent | Lens | 🩺 | claude-sonnet-4-6 | Anthropic |
| simulationagent | Forge | ⚡ | llama3.1:8b | Ollama (local) |
| recalibrationagent | Chisel | 🔧 | kimi-k2.5 | Moonshot |
| reportagent | Ledger | 📊 | kimi-k2.5 | Moonshot |
| slacknotificationagent | Signal | 📣 | llama3.1:8b | Ollama (local) |
| queryagent | Oracle | 🔮 | claude-sonnet-4-6 | Anthropic |
| carbonagent | Compass | 🧭 | claude-sonnet-4-6 | Anthropic |

## Skills

- nus-report: report generation
- nus-groundtruth: access ground truth meter data

## Script Locations

| Script | Path |
|---|---|
| `report.py` | `~/.openclaw/workspace-reportagent/skills/nus-report/scripts/report.py` |

## Running report.py

The script uses relative paths (`outputs/`, `reports/`, `openclaw_agents/building_registry.json`),
so it **must be run from `$NUS_PROJECT_DIR`** (`/Users/ye/nus-energy`):

```bash
# Per-building
cd $NUS_PROJECT_DIR
python3 ~/.openclaw/workspace-reportagent/skills/nus-report/scripts/report.py --building FOE6

# Campus summary
cd $NUS_PROJECT_DIR
python3 ~/.openclaw/workspace-reportagent/skills/nus-report/scripts/report.py --campus
```

Outputs:
- Per-building: `$NUS_PROJECT_DIR/reports/{BUILDING}/{BUILDING}_report.pdf`
- Campus: `$NUS_PROJECT_DIR/reports/NUS_Campus_Summary_Report.pdf`

## Reporting rules (updated 2026-04-12)

- **Reported EUI** means **total EUI including cooling electrical equivalent**.
- **Base electrical EUI** may appear as a secondary breakdown only.
- **Primary calibration error metrics are CVRMSE and NMBE.** MAPE is secondary context only, not the lead metric in reports or workflow messaging.
- Intervention results should be **simulation-based by default wherever patchable**. Use estimation only when simulation is not feasible.
- Reports must label intervention sources explicitly (`simulated` or `estimated`).
- Per-building report generation must refresh intervention JSON first when available, then rebuild the PDF.
- Per-building PDF generation must clear cached chart PNGs before rebuild to avoid stale figures.

## Input Dependencies

report.py reads from (all relative to `$NUS_PROJECT_DIR`):
- `outputs/{building}/parsed/{building}_monthly.csv` — from Radar (AnomalyAgent)
- `outputs/{building}/parsed/{building}_mape_comparison.csv` — from Radar
- `outputs/{building}/{building}_calibration_log.json` — from Chisel (RecalibrationAgent)
- `outputs/{building}/{building}_pipeline_state_*.json` — pipeline state
- `openclaw_agents/building_registry.json` — floor areas

## Python Dependencies

`reportlab`, `matplotlib`, `pandas`, `numpy` — must be installed via pip3

## Model Architecture (revised 2026-04-19)

- Agent: Ledger 📊
- Mode: script-first / light-llm
- Primary model: moonshot/kimi-k2.5
- Rationale: Structured templated reporting, low reasoning pressure.
- Rule: scripts own computation/mutation; the LLM owns reasoning, diagnosis, prioritisation, and explanation.

