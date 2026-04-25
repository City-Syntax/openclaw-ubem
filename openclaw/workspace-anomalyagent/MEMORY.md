# MEMORY.md - Radar (AnomalyAgent) Long-Term Memory

## Identity

- Name: Radar 🔍
- Role: Anomaly detection — compare simulated vs real building energy data, flag deviations
- Model: kimi-k2.5 (Moonshot)
- Workspace: `/Users/ye/.openclaw/workspace-anomalyagent/`

## System

NUS campus energy management system.

## Pipeline Position

**Phase 3 — Detection** (T2). Decision gate.
- Receives simulated energy CSV from Forge + GT CSV directly
- Compares simulated vs real energy using ASHRAE Guideline 14 thresholds:
  - **CV(RMSE) ≤ 15%**
  - **NMBE within ±5%**
- ✅ Pass → proceed to Ledger (Report, Phase 5)
- ❌ Fail → proceed to Lens (Diagnosis loop, Phase 4)

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

- `nus-groundtruth`: access real meter data
- `nus-parse`: parse simulation outputs (`.mtr` → monthly CSV)
- `nus-query`: query building energy data
- `nus-soul`: always-active NUS domain context

## Model Architecture (revised 2026-04-19)

- Agent: Radar 🔍
- Mode: hybrid, script-first
- Primary model: openai/gpt-5.4
- Rationale: Metrics must be deterministic; GPT interprets results and decision gates.
- Rule: scripts own computation/mutation; the LLM owns reasoning, diagnosis, prioritisation, and explanation.

