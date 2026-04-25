# MEMORY.md — Compass (CarbonAgent) Long-Term Memory

## Identity

- Name: Compass 🧭
- Role: Carbon scenario analysis — reads building energy data and IDF files, proposes ranked carbon reduction scenarios
- Model: claude-sonnet-4-6 (Anthropic)
- Workspace: `/Users/ye/.openclaw/workspace-compass/`

## System

NUS campus energy management system. ~300 buildings. All-electric campus (Scope 2 only for most buildings).

## Pipeline Position

**Phase 7 — Intervention** (T2). Triggered by Signal's decarbonisation decision gate.
- Activated after Signal (Phase 6) determines decarbonisation is required
- Generates decarbonisation strategy for affected buildings
- Classifies scenarios into three types:
  - 🟢 **Shallow** — low-effort, quick wins (setpoints, scheduling)
  - 🟡 **Medium** — moderate retrofit / operational changes (LED, glazing)
  - 🔴 **Deep** — major retrofit or infrastructure (HVAC, PV, envelope)
- Runs concurrently with Oracle (Slack Q&A during stakeholder review)
- Outputs feed into Ledger and Signal for final reporting/delivery

```
Signal 📣 → [decarb gate] → Compass 🧭 (scenarios) + Oracle 🔮 (Q&A) → Ledger 📊 → Signal 📣
```

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

- `nus-intervention`: full carbon scenario engine — baseline, fingerprint, interventions, shallow/medium/deep scenarios

## Scenario Tiers

Three tiers output directly from `carbon_scenarios.py`:

## Intervention policy (updated 2026-04-12)
- Use **simulation by default** wherever the intervention is patchable in the IDF.
- Use **estimation only when simulation is not feasible**.
- Always label each intervention/scenario source explicitly (`simulated` or `estimated`).

| Script ID | Tier | Interventions | Capex |
|---|---|---|---|
| `shallow` | 🟢 Shallow | Cooling setpoint + cool paint | Zero |
| `medium` | 🟡 Medium | Shallow + LED + shading + PV + green wall | Low–Medium |
| `deep` | 🔴 Deep | All 8 (including dimming + HVAC upgrade) | High |

Notify Signal 📣 immediately if `shallow` or `medium` scenario > 40% reduction potential.
- `nus-soul`: always-active NUS domain context (Singapore grid factor, BCA benchmarks, NUS targets)

## Key Constants

- Singapore grid emission factor: **0.4168 kgCO2e/kWh** (EMA 2023)
- BCA Green Mark Gold EUI benchmark: **115 kWh/m²/year**
- NUS carbon neutrality target: **2030**
- Cooling fraction estimate (IdealLoads buildings): **55% of total electricity**

## Known Building Notes

### BIZ8
- 18 zones, all IdealLoads HVAC (all-electric, Scope 2 only)
- Cooling setpoint: flat 25 °C across all zones, no occupancy schedule
- LPD: 9 W/m² (ASHRAE 90.1 CZ1 baseline)
- EPD: 12 W/m² (high — likely dense computing/AV)
- Infiltration: 6 ACH (very high — envelope leakage is a key lever)
- Solar absorptance: 0.7 on all surfaces (direct cool paint target)
- Glazing: SHGC=0.23 (already low), U=2.84, VT=0.253 (low — limits dimming benefit)
- Wall insulation: ~R=0.15 only (thin Mineral Wool) — green wall more impactful here
- No PV, no daylighting controls, no shading control, no vegetation modelled
- 331 shading surfaces present (surrounding building geometry, not controllable blinds)
- Intervention 5 (HVAC) flagged as simulation_limitation — IdealLoads cannot model real COP

## Lessons Learned

- Always check HVAC type first — IdealLoads is a simulation proxy, not a real system. Flag before reporting HVAC savings.
- EPD schedule in BIZ8 uses UUIDs as day schedule names — this is normal OpenStudio output.
- Intervention 3 (dimming) requires new IDF objects, not just parameter patches — flag for Chisel scope.
- Interaction discount (~15%) must be applied when combining multiple demand-side levers.

## Model Architecture (revised 2026-04-19)

- Agent: Compass 🧭
- Mode: llm-heavy
- Primary model: openai/gpt-5.4
- Rationale: Scenario reasoning and tradeoff analysis.
- Rule: scripts own computation/mutation; the LLM owns reasoning, diagnosis, prioritisation, and explanation.

