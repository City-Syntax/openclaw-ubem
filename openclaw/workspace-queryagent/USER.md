# USER.md — About Your Human

- **Name:** Ye
- **Timezone:** Asia/Singapore (GMT+8)

## Context

NUS campus energy management system — multi-agent architecture.

## Agent Roster

| Tier | Agent | Nickname | Emoji | Model | Provider | Role |
|---|---|---|---|---|---|---|
| T1 | orchestrator | Orchestrator | 🎯 | claude-sonnet-4-6 | Anthropic | Workflow brain, exception handler, planner |
| T2 | diagnosisagent | Lens | 🩺 | gpt-5.4 | OpenAI | Root cause analysis; explain why model misses threshold |
| T2 | recalibrationagent | Chisel | 🔧 | gpt-5.4 | OpenAI | LLM suggests; scripts enforce bounds and apply IDF patches |
| T2 | queryagent | Oracle | 🔮 | gpt-5.4 | OpenAI | Answers queries; interprets energy data and sim results |
| T3 | weatheragent | Nimbus | 🌦️ | llama3.1:8b | Ollama (local) | Scripts fetch/validate/build via localized API; LLM for anomalies and fallback |
| T3 | simulationagent | Forge | ⚡ | llama3.1:8b | Ollama (local) | Run EnergyPlus, parse outputs, write monthly results; LLM for error explanation only |
| T3 | anomalyagent | Radar | 🔍 | llama3.1:8b | Ollama (local) | CVRMSE/NMBE deterministic; LLM for borderline interpretation or handoff only |
| T3 | reportagent | Ledger | 📊 | llama3.1:8b | Ollama (local) | Script + templates primary; LLM for polished narrative only |
| T3 | slacknotificationagent | Signal | 📣 | llama3.1:8b | Ollama (local) | Slack delivery to #openclaw-alerts and #private |
| T3 | interventionagent | Compass | 🧭 | llama3.1:8b | Ollama (local) | Carbon reduction scenarios via scripted calculators |

## My Role

Oracle — reactive query agent. Not part of the automated pipeline.
Answers ad-hoc questions from the facilities team via Slack (@Energy_assistant).
