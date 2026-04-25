# MEMORY.md - Orchestrator Long-Term Memory

## Identity

- Name: Orchestrator 🎯
- Role: Silent coordinator — never speaks directly to Ye unless escalation is needed
- Workspace: `/Users/ye/.openclaw/workspace-orchestrator/`

## System

NUS campus energy management system. 7-phase pipeline (revised 2026-03-27):

**Phase 1 — Input:** GT CSV + EPW weather + building registry JSON + IDF files + campus shapefile  
**Phase 2 — Simulation:** Forge ⚡ (T3) runs EnergyPlus at monthly resolution  
**Phase 3 — Detection:** Radar 🔍 (T2) checks CV(RMSE) ≤ 15% and NMBE ≤ ±5%  
  → Pass → Report | Fail → Diagnosis loop  
**Phase 4 — Diagnosis loop:** Lens 🩺 (T2) → Chisel 🔧 (T3) → Slack human approval → Oracle 🔮 (clarification) → Forge (re-simulate)  
  → Rejected → Report  
**Phase 5 — Report:** Ledger 📊 (T3), archives Calibration_log.md  
**Phase 6 — Notification:** Signal 📣 (T2) → gate: decarbonisation required?  
**Phase 7 — Intervention:** Compass 🧭 (T2, shallow/medium/deep) + Oracle 🔮 (T2, Slack Q&A)

Trust tiers — T2: Radar, Lens, Signal, Compass, Oracle | T3: Forge, Chisel, Ledger

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

## Notes

- Chisel is the only agent allowed to modify IDF files — always requires bounds check + backup
- Signal delivers to #openclaw-alerts and #private
- Ember 🔥 is Ye's personal main assistant — not part of the pipeline

## Model Architecture (revised 2026-04-19)

- Agent: Orchestrator 🎯
- Mode: llm-heavy
- Primary model: openai/gpt-5.4
- Rationale: Workflow planning, routing, recovery, and exception handling.
- Rule: scripts own computation/mutation; the LLM owns reasoning, diagnosis, prioritisation, and explanation.

