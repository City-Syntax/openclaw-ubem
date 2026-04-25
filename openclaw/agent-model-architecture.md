# Agent Model Architecture (revised 2026-04-19)

This system now follows a **script-first, GPT-5.4 where reasoning matters** architecture.

## Design rule

- **Scripts own computation, simulation, parsing, file mutation, and transport**
- **LLMs own reasoning, diagnosis, prioritisation, explanation, and exception handling**
- Do not use a strong LLM where deterministic scripts are safer and cheaper

## Recommended agent setup

| Agent | Nickname | Mode | Primary model | Why |
|---|---|---|---|---|
| orchestrator | Orchestrator 🎯 | llm-heavy | openai/gpt-5.4 | Workflow planning, routing, recovery, exception handling |
| weatheragent | Nimbus 🌦️ | hybrid, script-first | openai/gpt-5.4 | Weather scripts do fetch/validate/build; GPT handles edge cases and reasoning |
| anomalyagent | Radar 🔍 | hybrid, script-first | openai/gpt-5.4 | Metrics must be deterministic; GPT interprets results and decision gates |
| diagnosisagent | Lens 🩺 | llm-heavy | openai/gpt-5.4 | Root-cause analysis is a reasoning-heavy task |
| simulationagent | Forge ⚡ | script-first | ollama/llama3.1:8b | EnergyPlus execution and parsing are script-dominant |
| recalibrationagent | Chisel 🔧 | hybrid, script-first | openai/gpt-5.4 | GPT proposes changes; scripts enforce bounds and patch IDFs |
| reportagent | Ledger 📊 | script-first / light-llm | moonshot/kimi-k2.5 | Good enough for structured templated reporting |
| slacknotificationagent | Signal 📣 | script-first | ollama/llama3.1:8b | Transport and short templates do not need a strong model |
| queryagent | Oracle 🔮 | llm-heavy | openai/gpt-5.4 | User-facing reasoning and explanation quality matter |
| interventionagent (carbonagent) | Compass 🧭 | llm-heavy | openai/gpt-5.4 | Scenario reasoning and tradeoff analysis |

## Practical interpretation

### Script-first agents
These should rely primarily on tools/scripts, not free-form reasoning:
- Forge
- Signal
- Ledger
- large parts of Nimbus

### Hybrid agents
These should use scripts for deterministic work and GPT-5.4 only for interpretation/proposal:
- Radar
- Chisel
- Nimbus

### LLM-heavy agents
These are the main places where GPT-5.4 adds the most value:
- Orchestrator
- Lens
- Oracle
- Compass

## Safety note

For high-consequence actions like IDF modification:
- GPT may propose
- scripts must validate bounds and apply changes
- never let narrative reasoning bypass deterministic safeguards
