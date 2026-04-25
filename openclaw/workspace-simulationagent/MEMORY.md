# MEMORY.md - Forge (SimulationAgent) Long-Term Memory

## Identity

- Name: Forge ⚡
- Role: EnergyPlus simulation runner — run the numbers, return output, move on
- Model: llama3.1:8b (Ollama, local at http://127.0.0.1:11434)
- Workspace: `/Users/ye/.openclaw/workspace-simulationagent/`

## System

NUS campus energy management system.

## Pipeline Position

**Phase 2 — Simulation** (T3). Entry point of pipeline.
- Receives IDF files + EPW weather as inputs
- Runs EnergyPlus at monthly resolution
- Outputs simulated energy CSV → Radar (Detection)
- GT CSV also flows directly to Radar alongside simulation output
- Re-runs during Phase 4 Diagnosis loop when Chisel applies approved IDF changes

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

## Key Paths

| What | Path |
|---|---|
| Project root | `/Users/ye/nus-energy` |
| simulate.py | `/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/scripts/simulate.py` |
| IDF files | `/Users/ye/nus-energy/idfs/` (also `/Users/ye/Documents/idf/`) |
| Weather file | `/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw` |
| EnergyPlus | `/Applications/EnergyPlus-23-1-0/` |
| Outputs | `/Users/ye/nus-energy/outputs/` |
| Ground truth (canonical) | `/Users/ye/nus-energy/ground_truth/ground-truth.csv` |
| Ground truth (parsed) | `/Users/ye/nus-energy/ground_truth/parsed/{BUILDING}_ground_truth.csv` |
| parse_ground_truth.py | `/Users/ye/nus-energy/parse_ground_truth.py` |

## Skills

- **nus-simulate**: prepare IDF, run EnergyPlus, parse results, calculate MAPE
- **nus-parse**: read and display already-generated monthly CSVs

## How simulate.py Works

Three main steps — all in one script:
1. `prepare_idf()` — apply tropical adjustments, inject output meters, save prepared IDF
2. `run_simulation()` — call EnergyPlus subprocess, write output to `outputs/{BUILDING}/simulation/`
3. `parse_results()` — parse meter CSV → clean 12-row monthly CSV at `outputs/{BUILDING}/parsed/`

Optionally: `calculate_mape()` — compare annual EUI vs ground truth, returns MAPE + CVRMSE + NMBE

## Calibration Thresholds (ASHRAE Guideline 14)

Evaluated by Radar (Detection, Phase 3):
- **CV(RMSE) ≤ 15%**
- **NMBE ≤ ±5%**
- Pass → Report | Fail → Diagnosis loop (Lens → Chisel → human approval)

## Buildings with Ground Truth (can calculate MAPE)

FOE6, FOE9, FOE13, FOE18, FOS43, FOS46, FOE1, FOE3, FOE5, FOE10, FOE11, FOE12, FOE15, FOE16, FOE19, FOE20, FOE23, FOE24, FOE26, FOS26, FOS35, FOS41, FOS44

All other buildings: simulation only, no ground-truth error metrics.

## IDF Adjustments Applied at Prep Time

- Infiltration < 0.5 ACH → raised to 0.5 (values ≥ 0.5 untouched)
- Missing ZoneControlTypeSchedule → auto-created as SCHEDULE:CONSTANT value 4
- Ground temperatures → set to 28°C year-round
- MRT ZoneAveraged → EnclosureAveraged: **only if EP ≥ 24** (current EP is 23 → skipped)
- VERSION stamp update: **only if EP ≥ 25** (current EP is 23 → skipped)
- Cooling setpoints: **NOT changed** — RecalibrationAgent (Chisel) handles those

## EnergyPlus Output Columns

After `parse_results()`, the monthly CSV has:
- `building`, `month_label`, `electricity_facility_kwh`, `eui_kwh_m2` (NaN until floor area known)

## Model Architecture (revised 2026-04-19)

- Agent: Forge ⚡
- Mode: script-first
- Primary model: ollama/llama3.1:8b
- Rationale: EnergyPlus execution and parsing are script-dominant.
- Rule: scripts own computation/mutation; the LLM owns reasoning, diagnosis, prioritisation, and explanation.

