# MEMORY.md - Long-Term Memory

## About Ye

- Building an **energy management system** with a multi-agent architecture
- Timezone: Asia/Singapore (GMT+8)

## Pipeline Workflow (revised 2026-03-31)

7-phase pipeline — see `WORKFLOW.md` for full details.

**Phases:**
1. **Input** — GT CSV, building registry JSON, IDF files, campus shapefile
   - 1a: **Weather** — Nimbus 🌦️ (T1) fetches → validates → builds site-calibrated EPW; Forge uses it via `--month`; falls back to base TMY if unavailable
   - 1b: **Registry validation** — Forge pre-flight checks building_registry.json, auto-populates if missing
2. **Simulation** — Forge ⚡ (T3), EnergyPlus at monthly resolution
3. **Detection** — Radar 🔍 (T2), CV(RMSE) < 15% & NMBE < 5% decision gate
4. **Diagnosis loop** — Lens 🩺 (T2) → Chisel 🔧 (T3) → Slack human approval → Oracle 🔮 → Forge (loop); rejected → Report
5. **Report** — Ledger 📊 (T3), archives to Calibration_log.md
6. **Notification** — Signal 📣 (T2) → decision gate: decarbonisation needed?
7. **Intervention** — Compass 🧭 (T2, shallow/medium/deep scenarios) + Oracle 🔮 (T2, Slack Q&A)

**Trust tiers:**
- T1: Orchestrator 🎯 (workflow brain, exception handler)
- T2: Lens 🩺, Chisel 🔧, Oracle 🔮 (reasoning + hybrid)
- T3: Nimbus 🌦️, Forge ⚡, Radar 🔍, Ledger 📊, Signal 📣, Compass 🧭 (script-first/script-only)

## Agent Roster

| Tier | Agent | Nickname | Emoji | Model | Mode |
|---|---|---|---|---|---|
| T1 | orchestrator | Orchestrator | 🎯 | anthropic/claude-sonnet-4-6 | LLM-heavy orchestration |
| T2 | diagnosisagent | Lens | 🩺 | openai/gpt-5.4 | LLM-heavy diagnosis |
| T2 | recalibrationagent | Chisel | 🔧 | openai/gpt-5.4 | Hybrid, script-first |
| T2 | queryagent | Oracle | 🔮 | openai/gpt-5.4 | LLM-heavy Q&A |
| T3 | weatheragent | Nimbus | 🌦️ | ollama/llama3.1:8b | Script-first |
| T3 | simulationagent | Forge | ⚡ | ollama/llama3.1:8b | Script-only |
| T3 | anomalyagent | Radar | 🔍 | ollama/llama3.1:8b | Script-first |
| T3 | reportagent | Ledger | 📊 | ollama/llama3.1:8b | Script-first |
| T3 | slacknotificationagent | Signal | 📣 | ollama/llama3.1:8b | Script-only |
| T3 | interventionagent | Compass | 🧭 | ollama/llama3.1:8b | Script-first |

## Three-Tier Agent System

The NUS energy system follows a **three-tier operating model**:

1. **T1 — Orchestration**
   - Workflow brain, exception handler, planner
   - Agent: Orchestrator 🎯 (claude-sonnet-4-6)

2. **T2 — Reasoning (LLM-heavy / Hybrid)**
   - LLMs own diagnosis, calibration suggestion, Q&A, and interpretation
   - Agents: Lens 🩺, Chisel 🔧, Oracle 🔮 (all gpt-5.4)
   - Chisel: LLM suggests; scripts enforce bounds and write IDF patches

3. **T3 — Execution (script-first / script-only)**
   - Deterministic scripts own simulation, metrics, parsing, transport, and scenarios
   - LLMs used only for anomaly summaries, error explanation, or polished narrative
   - Agents: Nimbus 🌦️, Forge ⚡, Radar 🔍, Ledger 📊, Signal 📣, Compass 🧭 (all ollama)

## Model Providers

- **Anthropic** → `anthropic/claude-sonnet-4-6` for Orchestrator (T1 workflow brain)
- **OpenAI** → `openai/gpt-5.4` for T2 reasoning agents (Lens, Chisel, Oracle)
- **Ollama (local)** → `ollama/llama3.1:8b` for all T3 script-first/script-only agents

## WeatherAgent (Nimbus 🌦️) — nus-weather skill

- Workspace: `/Users/ye/.openclaw/workspace-weatheragent/`
- Skill: `/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/`
- Scripts: `skills/nus-weather/scripts/` — `fetch_weather.py`, `validate_weather.py`, `build_epw.py`
- Pipeline position: **T3, Phase 1a** — runs before Forge, produces calibrated EPW
- Data sources (priority):
  1. **NUS localized station API** — `MET_E1A` via `your API` (integrated 2026-04-06)
  2. NUS onsite stations (REST/CSV-push)
  3. data.gov.sg (Clementi S121)
  4. Base TMY fallback
- Station config: `/Users/ye/nus-energy/weather/station_config.json`
- Output: `/Users/ye/nus-energy/weather/calibrated/{YYYY-MM}_site_calibrated.epw`
- Latest conditions JSON: `/Users/ye/nus-energy/weather/observed/latest_conditions.json` (read by Radar, Compass)
- Forge picks up calibrated EPW automatically via `--month YYYY-MM` flag → `resolve_epw()` in simulate.py
- If no calibrated EPW exists for the month, Forge logs a warning and uses base IWEC TMY

## Agent Workspaces

Each agent has its own workspace under `/Users/ye/.openclaw/workspace-<agentname>/`
with its own IDENTITY.md, SOUL.md, AGENTS.md, HEARTBEAT.md, and skills.

## Integrations

- Slack connected to: **#openclaw-alerts** and **#private**

## Code Organisation Convention

**Scripts always live inside the skill that owns them, under a `scripts/` subdirectory.**

```
workspace-<agent>/skills/<skill-name>/
├── SKILL.md
└── scripts/
    └── <script>.py
```

- Data files (IDFs, ground truth, outputs) stay in `/Users/ye/nus-energy/` (project dir)
- Scripts reference project data via `NUS_PROJECT_DIR` env var
- Skills reference scripts as `{SKILL_DIR}/scripts/<script>.py`
- No scripts floating at project root or directly in the skill dir

Current script locations:
| Script | Skill | Agent |
|---|---|---|
| `simulate.py` | nus-simulate/scripts/ | simulationagent |
| `parse_eso.py` | nus-parse/scripts/ | anomalyagent |
| `prepare_ground_truth.py` | nus-groundtruth/scripts/ | anomalyagent |
| `patch_idf.py` | nus-calibrate/scripts/ | recalibrationagent |
| `generate_registry.py` | nus-registry/scripts/ | simulationagent |
| `report.py` | nus-report/scripts/ | reportagent |
| `slack_server.py` | nus-slack-server/scripts/ | slacknotificationagent |

## SlackNotificationAgent (Signal 📣) — Skills

| Skill | Purpose | Direction |
|---|---|---|
| `slack` | Raw transport | both |
| `nus-notify` | NUS message templates, channel routing, payload schema | outbound |
| `nus-slack-server` | Socket Mode bot for inbound @Energy_assistant queries | inbound |
| `nus-soul` | Always-active NUS domain context | context |

## Calibration Status (updated 2026-04-24)

**20/23 GT buildings calibrated (87.0%)**

Calibrated: FOE1, FOE5, FOE6, FOE9, FOE10, FOE11, FOE12, FOE13, FOE15, FOE16, FOE19, FOE20, FOE23, FOE24, FOE26, FOS26, FOS35, FOS41, FOS44, FOS46

Unresolved (structural IDF/metering issues):
- FOE18: IDF floor area 4x too large vs metered area
- FOE3: server/data center hidden loads not in IDF geometry
- FOS43: lab loads beyond max parameter bounds

**FOS26 note:** District-cooled. Was incorrectly failing — GT meter includes chilled water equivalent. Correct comparison basis is `eui_adj`. CVRMSE 5.89%, NMBE -2.44% ✅. Tagged `district_cooled=true` in building_registry.json.

### Seasonal schedule patching (2026-04-24)
- Script: `/Users/ye/nus-energy/patch_seasonal_schedule.py`
- Method: GT monthly scale factors → per-month Schedule:Day:Hourly (uniform fraction = base_avg * scale) + level correction (adjust W/m2 to match GT annual mean)
- Equipment_W_per_m2 max raised to 80 W/m2 (from 60) for high-intensity A1_M_H lab buildings

## Calibration Rules (updated 2026-04-25)

**Slack notifications: TWO types only — calibration approval requests and final intervention results. Nothing else.**
- No progress updates, no simulation status, no intermediate results to Slack
- Keep all Slack messages concise and token-minimal

**Approval message:** `{BUILDING} 🔧 Recalibration needed (iteration N) — Reply "approve" or "reject".`
- No diffs, no parameter names, no values, no predicted metrics in the approval message
- Human replies "approve" → Chisel writes patch; "reject" → fall through to Report
- No reply after 4h → watchdog posts escalation to #private; pipeline waits indefinitely
- No iteration cap — keep iterating (with approval each time) until CVRMSE ≤ 15% AND NMBE ≤ ±5%
- Stop only when all calibration parameters have hit bounds without convergence → flag for human review

**Final intervention results message:** posted after Compass completes all scenarios — concise summary only (building, top scenario, % reduction)



**Cooling setpoint policy:**
- **Calibration:** do **not** use Cooling_Setpoint_C as a calibration variable.
- **Intervention:** Cooling_Setpoint_C may be used as a scenario lever for operational or carbon reduction analysis.
- There is no NUS-wide universal setpoint policy. Building setpoints should reflect actual operations and be handled separately from calibration.

**Calibration variables (in priority order):**
1. Infiltration_ACH
2. Equipment_W_per_m2
3. Lighting_W_per_m2

## Pipeline: Registry Validation (Phase 1b)

Added 2026-03-30. Before simulation, Forge runs a pre-flight registry check:
1. Check building exists in `building_registry.json`
2. If missing → auto-populate via `generate_registry.py` (extracts floor area from IDF Zone geometry)
3. Warn-and-proceed policy — simulation never blocked; EUI shows `—` if floor area unavailable
4. Flag missing registry entries in Slack notification

Script: `workspace-simulationagent/skills/nus-registry/scripts/generate_registry.py`

## SimulationAgent (Forge ⚡) — nus-simulate skill

- Skill lives at: `/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/`
- EnergyPlus 23.1.0 at `/Applications/EnergyPlus-23-1-0/` — confirmed working
- Python deps installed: `eppy`, `pandas`, `numpy` (system Python 3.9 via pip3)
- EP23 outputs meter data as **`.mtr`** (ESO format), not `Meter.csv`
- `simulate.py` writes the authoritative rich monthly CSV directly to `outputs/{BUILDING}/parsed/{BUILDING}_monthly.csv`
- Do **not** overwrite that monthly CSV with the older standalone `parse_eso.py` output; that parser is lossy and drops district-cooling / adjusted-EUI fields
- For district-cooled buildings, downstream reports and intervention visualisations must use `cooling_elec_adj_kwh` (or `cooling_thermal_kwh / COP`) for cooling bars and adjusted EUI, not `cooling_elec_kwh`
- Ground truth data lives at `/Users/ye/nus-energy/ground_truth/`
- **Shared outputs dir**: `/Users/ye/nus-energy/outputs/` — all agents read/write here
- **23 GT buildings** across 5 archetypes — all have ground-truth meter data in `raw_meter_data.csv`
  - A1_H_L (6): FOE6, FOE9, FOE13, FOE18, FOS43, FOS46
  - A1_L_L (7): FOE1, FOE3, FOE5, FOE15, FOE24, FOE26, FOS26
  - A1_M_H (3): FOS35, FOS41, FOS44
  - A1_M_L (6): FOE11, FOE12, FOE16, FOE19, FOE20, FOE23
  - A5 (1):     FOE10
- Non-GT archetypes (A2, A3, A4, A6) inherit calibrated params from nearest GT archetype via `parameter_bounds.json → archetype_profiles`
- Calibration is archetype-based: calibrate GT buildings per archetype → propagate to all buildings in same archetype
- **318 IDF files** total under `/Users/ye/nus-energy/idfs/` (recursive, nested variant subdirs like A1_H_L, A2_H_H, etc.)
- Batch runner updated to `rglob("*.idf")` — unique `run_id` = `{variant}__{stem}` for subdirs, plain stem for top-level
- Ground truth lookup always uses bare stem regardless of variant
- `NUS_PROJECT_DIR` = `/Users/ye/nus-energy` (corrected 2026-03-19; was wrongly set to `/Users/ye/.openclaw`)
- SKILL.md updated 2026-03-19 to reflect correct paths; `simulate.py` CONFIG and `parse_eso.py` defaults updated to point to shared outputs dir

## InterventionAgent (Compass 🧭) — nus-intervention skill

- Workspace: `/Users/ye/.openclaw/workspace-interventionagent/`
- Skill lives at: `/Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/`
- Script: `scripts/carbon_scenarios.py`
- Runs on **all GT buildings regardless of calibration status** — scenarios are always useful for planning
- Uncalibrated buildings get a `⚠ Uncalibrated baseline — treat savings as indicative` flag in output/Slack
- Reads: `outputs/{BUILDING}/parsed/{BUILDING}_monthly.csv` + IDF (calibrated preferred)
- Writes: `outputs/{BUILDING}/carbon/{BUILDING}_carbon_scenarios.json`
- Scores 8 interventions; assembles 5 standard scenarios (quick_wins, efficiency_push, deep_retrofit, solar_bridge, net_zero)
- Uses Singapore grid factor: 0.4168 kgCO2e/kWh
- Pipeline position: **Forge ⚡ → Compass 🧭 → Ledger 📊**
- If quick_wins or efficiency_push > 40% reduction potential → notify Signal 📣 immediately

## Communication Preferences

- **Slack replies: keep concise** — short answers to save tokens
- **Quantify simulation/calibration error with CVRMSE and NMBE as primary metrics.** Do not use MAPE as the primary error summary in agent or workflow messaging; MAPE may appear only as secondary context if needed.
- **Intervention analysis rule:** use simulation by default wherever patchable; use estimation only when simulation is not feasible; always label the source explicitly.

## Identity

- My name is **Ember** 🔥
- Named 2026-03-18
