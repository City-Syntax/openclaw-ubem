# USER.md - About Your Human

- **Name:** Ye
- **What to call them:** Ye
- **Pronouns:** _(unknown)_
- **Timezone:** Asia/Singapore (GMT+8)

## Context

Working on an **energy management system** with a multi-agent architecture at NUS campus.

| Tier | Agent | Nickname | Emoji | Model | Mode | Role |
|---|---|---|---|---|---|---|
| T1 | orchestrator | Orchestrator | 🎯 | anthropic/claude-sonnet-4-6 | LLM-heavy | Workflow brain, exception handler, planner |
| T2 | diagnosisagent | Lens | 🩺 | openai/gpt-5.4 | LLM-heavy | Explain why model misses threshold; root cause analysis |
| T2 | recalibrationagent | Chisel | 🔧 | openai/gpt-5.4 | Hybrid, script-first | LLM suggests; scripts enforce bounds and apply IDF patches |
| T2 | queryagent | Oracle | 🔮 | openai/gpt-5.4 | LLM-heavy | Answers queries; interprets energy data and sim results |
| T3 | weatheragent | Nimbus | 🌦️ | ollama/llama3.1:8b | Script-first | Scripts fetch/validate/build via localized API; LLM for anomalies and fallback |
| T3 | simulationagent | Forge | ⚡ | ollama/llama3.1:8b | Script-only | Run EnergyPlus, parse outputs, write monthly results; LLM for error explanation only |
| T3 | anomalyagent | Radar | 🔍 | ollama/llama3.1:8b | Script-first | CVRMSE/NMBE deterministic; LLM only for borderline interpretation or handoff |
| T3 | reportagent | Ledger | 📊 | ollama/llama3.1:8b | Script-first | Script + templates primary; LLM for polished narrative only |
| T3 | slacknotificationagent | Signal | 📣 | ollama/llama3.1:8b | Script-only | Slack delivery to #openclaw-alerts and #private |
| T3 | interventionagent | Compass | 🧭 | ollama/llama3.1:8b | Script-first | Carbon reduction scenarios via scripted calculators |

## Integrations

- Slack: **#openclaw-alerts** and **#private**
