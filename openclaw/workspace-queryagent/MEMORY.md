# MEMORY.md — Oracle (QueryAgent) Long-Term Memory

## Identity

- Name: Oracle 🔮
- Role: Answer ad-hoc energy questions from the facilities team, grounded in pipeline output data
- Model: claude-sonnet-4-6 (Anthropic)
- Workspace: `/Users/ye/.openclaw/workspace-queryagent/`

## System

NUS campus energy management system — 23 buildings, 6 with ground-truth meter data (all A1_H_L archetype).

## Pipeline Position

**Phase 4 + Phase 7** (T2). Participates at two points:
- **Phase 4 (Diagnosis loop):** Answers clarifying questions from the human engineer during the Slack approval review of Chisel's proposed IDF changes
- **Phase 7 (Intervention):** Simultaneously available to answer stakeholder questions and clarify decarbonisation requirements via Slack, while Compass generates scenarios
- Also reactive/conversational: triggered by Slack @Energy_assistant mentions (via nus-slack-server on Signal)

## Data Sources (read-only)

| Source | Path | Contains |
|---|---|---|
| Monthly simulated kWh | `/Users/ye/nus-energy/outputs/{building}/parsed/{building}_monthly.csv` | Monthly energy per building |
| Ground truth meter | `/Users/ye/nus-energy/ground_truth/` | Real meter data, FOE6/FOE9/FOE13/FOE18/FOS43/FOS46 |
| Building registry | `/Users/ye/nus-energy/building_registry.json` | All 23 buildings, metadata |
| Parameter bounds | `/Users/ye/nus-energy/parameter_bounds.json` | IDF parameter ranges |
| Calibrated IDF | `/Users/ye/nus-energy/outputs/{building}/prepared/{building}_prepared.idf` | Latest calibrated IDF |

## NUS Domain Facts

- 23 buildings in IDF portfolio
- 23 with ground truth across 5 archetypes: FOE6, FOE9, FOE13, FOE18, FOS43, FOS46, FOE1, FOE3, FOE5, FOE10, FOE11, FOE12, FOE15, FOE16, FOE19, FOE20, FOE23, FOE24, FOE26, FOS26, FOS35, FOS41, FOS44
- Canonical meter data range for comparison: Jan 2024 – Dec 2024
- Simulation year: 2024
- Grid carbon factor: 0.4168 kgCO2e/kWh
- Electricity tariff: ~SGD 0.28/kWh
- BCA Green Mark 2021: Platinum ≤85, Gold Plus ≤100, Gold ≤115, Certified ≤130 kWh/m²/year
- ASHRAE Guideline 14: CVRMSE ≤15%, NMBE ≤±5% — must pass under current detector basis

## Agent Roster

| Agent | Nickname | Emoji | Model | Provider |
|---|---|---|---|---|
| orchestrator | Orchestrator | 🎯 | — | — |
| anomalyagent | Radar | 🔍 | kimi-k2.5 | Moonshot |
| diagnosisagent | Lens | 🩺 | claude-sonnet-4-6 | Anthropic |
| simulationagent | Forge | ⚡ | llama3.1:8b | Ollama |
| recalibrationagent | Chisel | 🔧 | kimi-k2.5 | Moonshot |
| reportagent | Ledger | 📊 | kimi-k2.5 | Moonshot |
| slacknotificationagent | Signal | 📣 | llama3.1:8b | Ollama |
| queryagent | Oracle | 🔮 | claude-sonnet-4-6 | Anthropic |

## Skills

| Skill | Purpose |
|---|---|
| `nus-query` | Read and interpret pipeline output data to answer questions |
| `nus-soul` | Always-active NUS domain context |

## Model Architecture (revised 2026-04-19)

- Agent: Oracle 🔮
- Mode: llm-heavy
- Primary model: openai/gpt-5.4
- Rationale: User-facing reasoning and explanation quality matter.
- Rule: scripts own computation/mutation; the LLM owns reasoning, diagnosis, prioritisation, and explanation.

