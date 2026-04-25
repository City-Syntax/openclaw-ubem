# MEMORY.md — Nimbus (WeatherAgent) Long-Term Memory

## Identity

- Name: Nimbus 🌦️
- Role: Weather data acquisition, validation, and site-calibrated EPW generation
- Model: claude-sonnet-4-6 (Anthropic)
- Workspace: `/Users/ye/.openclaw/workspace-weatheragent/`

## System

NUS campus energy management system.

## Pipeline Position

**Phase 1 — Weather** (T1, before simulation). Parallel to and upstream of Forge (SimulationAgent).

- Triggered by Orchestrator before each simulation run, or on-demand for current conditions
- Produces site-calibrated EPW → Forge uses it in place of the base TMY when available
- Also triggered independently by Radar (AnomalyAgent) to fetch weather context for anomaly diagnosis
- Compass (CarbonAgent) may call for current conditions when explaining scenario narratives

## Agent Roster

| Agent | Nickname | Emoji | Model | Provider |
|---|---|---|---|---|
| orchestrator | Orchestrator | 🎯 | — | — |
| weatheragent | Nimbus | 🌦️ | claude-sonnet-4-6 | Anthropic |
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
| Base TMY EPW | `/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw` |
| Observed archive | `/Users/ye/nus-energy/weather/observed/` |
| Calibrated EPW dir | `/Users/ye/nus-energy/weather/calibrated/` |
| Latest conditions | `/Users/ye/nus-energy/weather/observed/latest_conditions.json` |
| Station config | `/Users/ye/nus-energy/weather/station_config.json` |
| fetch_weather.py | `/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/scripts/fetch_weather.py` |
| build_epw.py | `/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/scripts/build_epw.py` |
| validate_weather.py | `/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/scripts/validate_weather.py` |

## Skills

- **nus-weather**: fetch onsite station data, validate readings, build site-calibrated EPW files

## How the EPW Patching Works

Three-step process:
1. `fetch_weather.py` — pull hourly data from stations or API, save to `observed/{YYYY-MM}.parquet`
2. `validate_weather.py` — spike detection (4σ rolling), gap filling (linear interpolation up to 3h), flag poor months (>20% missing)
3. `build_epw.py` — clone base TMY EPW, overwrite DBT/RH/GHI/wind columns with observed values for validated months; leave unvalidated months as TMY

## Weather Variables Collected

| Variable | Column in EPW | Station sensor | API fallback |
|---|---|---|---|
| Dry-bulb temperature (°C) | DBT | ✅ | data.gov.sg `air-temperature` |
| Relative humidity (%) | RH | ✅ | data.gov.sg `relative-humidity` |
| Global horizontal irradiance (W/m²) | GHI → DHI+DNI | ✅ (pyranometer) | MSS monthly normals |
| Wind speed (m/s) | WS | ✅ | data.gov.sg `wind-speed` |
| Wind direction (°) | WD | ✅ | data.gov.sg `wind-direction` |
| Rainfall (mm/hr) | Precip | ✅ | data.gov.sg `rainfall` |

## Data Quality Thresholds

- Spike detection: >4σ from 24h rolling mean → substitute with linear interpolation, log event
- Gap tolerance: ≤3 consecutive hours → interpolate; >3h → mark as missing
- Monthly coverage threshold: <80% observed hours → flag month as `"data_quality": "poor"`, use TMY for that month
- Cross-validation: monthly mean DBT must be within ±2°C of MSS climatological normal; alert if outside

## Known Stations

See `station_config.json`. Initial setup should include at minimum:
- Kent Ridge campus weather mast (primary)
- Bukit Timah campus station (secondary)
- data.gov.sg Clementi / Queenstown stations (API fallback)

## Integration Points

- **Forge (Simulate):** Nimbus produces the EPW path; Forge uses `--epw` flag to consume it
- **Radar (Anomaly):** Radar calls `GET /weather/conditions?month=YYYY-MM` to get observed vs TMY deltas when diagnosing consumption anomalies
- **Compass (Carbon):** Compass reads `latest_conditions.json` to contextualise current EUI against actual weather
- **Ledger (Report):** Ledger pulls `calibrated/*.epw` metadata for the weather section of calibration reports

## Model Architecture (revised 2026-04-19)

- Agent: Nimbus 🌦️
- Mode: hybrid, script-first
- Primary model: openai/gpt-5.4
- Rationale: Scripts fetch/validate/build EPW; GPT handles edge cases and reasoning.
- Rule: scripts own computation/mutation; the LLM owns reasoning, diagnosis, prioritisation, and explanation.

