---
name: nus-orchestrate
description: Daily pipeline orchestration for the NUS campus energy management system. Coordinates all 8 specialist agents in the correct 7-phase sequence. Use when running the full daily pipeline, triggering a partial re-run after recalibration, or deciding which buildings need action.
metadata: {"openclaw": {"emoji": "🎯"}}
---

# NUS Orchestrate Skill

## Overview

This skill defines the full daily pipeline sequence, handoff payloads between agents,
decision rules, and fallback behaviour. The Orchestrator never does analysis itself —
it delegates and stitches.

The Orchestrator is fronted by an LLM (default model: `openai/gpt-5.1`). It must convert
natural-language user commands into structured intents, then drive the pipeline
accordingly.

---

## Intent Parsing (LLM Front-End)

When the user talks to the Orchestrator (via ACP, Slack, or web UI), the LLM must
normalize their request into a small JSON intent. Use this as the contract:

```jsonc
{
  "action": "run_workflow",        // required
  "building_id": "FOE1",          // optional for campus-wide runs
  "mode": "full",                 // "full" | "simulate_only" | "calibration_only" | "interventions_only"
  "months": "latest"              // "latest" or ["YYYY-MM", ...]
}
```

### Natural Language → Intent Rules

- If the user says:
  - "test the pipeline for FOE3"
  - "test the pipeline for FOE3 end-to-end"
  - "run FOE3 end-to-end"
  - "end-to-end test for FOE3"

  Then parse as:

  ```jsonc
  {
    "action": "run_workflow",
    "building_id": "FOE3",
    "mode": "full",
    "months": "latest"
  }
  ```


- If the user says:
  - "run workflow for FOE1"
  - "single-building run for FOE1"
  - "run FOE1"

  Then parse as:

  ```jsonc
  {
    "action": "run_workflow",
    "building_id": "FOE1",
    "mode": "full",
    "months": "latest"
  }
  ```

- If the user says:
  - "simulate FOE1 only"
  - "just simulate FOE1 for 2025-01"

  Then parse as:

  ```jsonc
  {
    "action": "run_workflow",
    "building_id": "FOE1",
    "mode": "simulate_only",
    "months": ["2025-01"]
  }
  ```

- If the user says:
  - "calibrate FOE1"
  - "re-run calibration for FOE1"

  Then parse as:

  ```jsonc
  {
    "action": "run_workflow",
    "building_id": "FOE1",
    "mode": "calibration_only",
    "months": "latest"
  }
  ```

- If the user says:
  - "run interventions for FOE1"
  - "carbon scenarios for FOE1"

  Then parse as:

  ```jsonc
  {
    "action": "run_workflow",
    "building_id": "FOE1",
    "mode": "interventions_only",
    "months": "latest"
  }
  ```

### LLM Behaviour

1. **Always** infer the intent JSON from the user message.
2. **Echo the intent back** in a short confirmation message, e.g.:
   - "Intent: run_workflow for FOE1, mode=full, months=latest. Proceeding."
3. Hand this intent to the runner script (`run_pipeline.py`) by translating it to
   the appropriate CLI call (see below).

### Intent → CLI Mapping

For `action == "run_workflow"`:

- Resolve `month`:
  - If `months == "latest"`, use the most recent month that has ground-truth and
    weather available (or default to the current calendar month).
  - If `months` is a list, use the last entry.

- Compute CLI flags:

```text
base_cmd = "python3 ~/.openclaw/workspace-ORCHESTRATOR/skills/nus-orchestrate/scripts/run_pipeline.py"

# Single-building run
if intent.building_id is not null:
  buildings_part = f"--buildings {intent.building_id}"
else:
  buildings_part = ""  # campus-wide

month_part = f"--month {month}"

# Mode handling
if intent.mode == "full":
  extra = ""  # no extra flags
elif intent.mode == "simulate_only":
  extra = "--skip-weather --skip-simulation"  # Or adjust when dedicated entrypoints exist
elif intent.mode == "calibration_only":
  extra = "--resume-from diagnosis"
elif intent.mode == "interventions_only":
  extra = "--resume-from intervene"
else:
  extra = ""

final_command = f"{base_cmd} {month_part} {buildings_part} {extra}".strip()
```

4. The orchestrator agent (in ACP) should execute `final_command` on the host
   shell and stream logs back to the user.

---

## Runner Script

The pipeline can be driven end-to-end from a single script:

```bash
python3 ~/.openclaw/workspace-orchestrator/skills/nus-orchestrate/scripts/run_pipeline.py \
  --month 2024-08
```

### CLI Arguments

| Argument | Default | Description |
|---|---|---|
| `--month YYYY-MM` | *(required)* | Target simulation month |
| `--buildings B1 B2 …` | all 23 GT buildings | Limit run to specific buildings |
| `--skip-weather` | off | Skip Phase 1a; use existing calibrated EPW or TMY fallback |
| `--skip-simulation` | off | Skip Phase 2; use existing parsed CSVs for detection |
| `--dry-run` | off | Print what would run without executing anything |
| `--workers N` | 4 | Parallel EnergyPlus workers (Forge batch) |
| `--resume-from PHASE` | *(none)* | Resume from a specific phase, skipping all earlier ones. Choices: `weather`, `simulation`, `detection`, `diagnosis`, `report`, `notify`, `intervene` |

### Environment

| Variable | Default | Description |
|---|---|---|
| `NUS_PROJECT_DIR` | `/Users/ye/nus-energy` | Root of the NUS energy project tree |

### What each phase does

| Phase | Name | Agent(s) | What runs |
|---|---|---|---|
| 0 | Setup | — | Creates output dirs; loads/creates `pipeline_state.json`; opens log file |
| 1a | Weather | Nimbus | `fetch_weather.py` → `validate_weather.py` → `build_epw.py`; sets `epw_source` |
| 1b | Ground truth prep | Radar | `prepare_ground_truth.py` (skipped if sentinel CSV already exists) |
| 2 | Simulation | Forge | `simulate.py` batch (or per-building with `--idf` when `--buildings` used) |
| 3 | Detection gate | Radar | Reads `simulation_summary.csv`; classifies each building (calibrated/warning/critical/no_data); collects `needs_diagnosis` list |
| 4 | Diagnosis loop | Lens ⏸ → Chisel → Slack | Writes `lens_input.json` per building and **pauses** (Lens is LLM-only, no script). On resume: reads `lens_output.json`, runs Chisel dry-run diff, waits for `slack_approval.json`, then applies patch + re-runs simulation if approved |
| 5 | Report | Ledger | `report.py` — generates daily/weekly report to `outputs/reports/` |
| 6 | Notification | Signal | `pipeline_trigger.py` — delivers results to Slack; prints decarbonisation decision gate prompt |
| 7 | Intervention | Compass + Oracle | `carbon_scenarios.py` per building; `query.py` to enable Oracle Q&A mode |

### Lens pause/resume pattern (Phase 4)

Lens (DiagnosisAgent) is a **pure LLM agent** — it has no callable script. The pipeline handles this by:

1. Writing `outputs/{BUILDING}/lens_input.json` with CVRMSE, NMBE, file paths, and iteration count
2. Printing a `⏸ PIPELINE PAUSED` banner and exiting cleanly
3. Waiting for the human/AI to run Lens and write `outputs/{BUILDING}/lens_output.json`
4. Resuming the pipeline with `--resume-from diagnosis`

Similarly, **Slack approval** is handled via `outputs/{BUILDING}/slack_approval.json`:
```json
{"approved": true}
```

After approval, Chisel patches the IDF and Forge re-runs for just that building.

**Intervention approval** is via `outputs/intervention_approved.json`:
```json
{"approved": true, "buildings": ["all"]}
```
(Defaults to all GT buildings if the file is absent.)

### Typical resume sequence

```bash
# Initial run — pauses at Phase 4 waiting for Lens
python3 run_pipeline.py --month 2024-08

# After Lens writes lens_output.json for each building:
python3 run_pipeline.py --month 2024-08 --resume-from diagnosis

# After writing slack_approval.json + intervention_approved.json:
python3 run_pipeline.py --month 2024-08 --resume-from intervene
```

### Log & state output

- **Log file:** `$NUS_PROJECT_DIR/outputs/pipeline_{MONTH}.log`
- **State file:** `$NUS_PROJECT_DIR/outputs/pipeline_state.json`

### Detection classifications (Phase 3)

| Status | Condition |
|---|---|
| `calibrated` | CV(RMSE) ≤ 15% **and** \|NMBE\| ≤ 5% |
| `warning` | CV(RMSE) 15–30% **or** \|NMBE\| 5–10% |
| `critical` | CV(RMSE) > 30% **or** \|NMBE\| > 10% |
| `no_data` | No entry in simulation_summary.csv |
| `error` | Forge failed for this building |

---

## Agent Directory

| Agent | Nickname | Trust | Role | Workspace |
|---|---|---|---|---|
| WeatherAgent | Nimbus 🌦️ | T1 | Fetch live weather, build site-calibrated EPW | `workspace-weatheragent` |
| SimulationAgent | Forge ⚡ | T3 | Run EnergyPlus simulations | `workspace-simulationagent` |
| AnomalyAgent | Radar 🔍 | T2 | Parse outputs, compute ASHRAE metrics, detection gate | `workspace-anomalyagent` |
| DiagnosisAgent | Lens 🩺 | T2 | Root cause analysis | `workspace-diagnosisagent` |
| RecalibrationAgent | Chisel 🔧 | T3 | Propose + apply IDF patches (human approval required) | `workspace-recalibrationagent` |
| ReportAgent | Ledger 📊 | T3 | Daily/weekly reports, archives Calibration_log.md | `workspace-reportagent` |
| SlackNotificationAgent | Signal 📣 | T2 | Slack delivery + decarbonisation decision gate | `workspace-slacknotificationagent` |
| InterventionAgent | Compass 🧭 | T2 | Carbon reduction scenarios (shallow/medium/deep) | `workspace-interventionagent` |
| QueryAgent | Oracle 🔮 | T2 | Clarification during approval (Phase 4) + stakeholder Q&A (Phase 7) | `workspace-queryagent` |

---

## Full Daily Pipeline (7 Phases)

```
Phase 1 — Input
  └─ Gather: GT CSV + building registry JSON + IDF files + campus shapefile

Phase 1a — Weather (Nimbus 🌦️, T1) [parallel to Phase 1b]
  └─ Fetch hourly observations — priority: (1) NUS localized API MET_E1A, (2) NUS onsite stations, (3) data.gov.sg S121
  └─ Validate: spike detection + gap-fill + quality flag
  └─ Build site-calibrated EPW for simulation month
  └─ Notify Orchestrator: calibrated_epw_ready → Forge uses it via --month flag
  └─ If unavailable: Forge falls back to base TMY (log warning to Slack)

Phase 1b — Registry Validation (Forge ⚡, pre-flight)
  └─ Check building exists in building_registry.json; auto-populate if missing

Phase 2 — Simulation (Forge ⚡, T3)
  └─ Run EnergyPlus on IDF files at monthly resolution
  └─ Uses calibrated EPW from Nimbus if available, else base TMY
  └─ GT CSV also flows directly to Radar alongside simulation output

Phase 3 — Detection (Radar 🔍, T2) ← DECISION GATE
  ├─ CV(RMSE) ≤ 15% AND NMBE ≤ ±5% → ✅ model accurate → Phase 5 (Report)
  └─ Thresholds not met            → ❌ model inaccurate → Phase 4 (Diagnosis loop)

Phase 4 — Diagnosis loop (Lens 🩺 → Chisel 🔧 → Human approval)
  └─ Lens: root cause hypotheses
  └─ Chisel: proposes IDF parameter changes (bounded by parameter_bounds.json)
  └─ Signal: sends parameter proposal to engineer via Slack
  └─ Oracle: available to answer engineer's clarifying questions during review
  ├─ APPROVED → updated IDFs → back to Forge (Phase 2, loop)
  └─ REJECTED → fall through to Phase 5

Phase 5 — Report (Ledger 📊, T3)
  └─ Archives all findings to Calibration_log.md
  └─ Documents root-cause hypotheses from Lens
  └─ Generates report regardless of diagnosis loop outcome

Phase 6 — Notification (Signal 📣, T2) ← DECISION GATE
  └─ Delivers calibration results to Slack
  ├─ Decarbonisation required? → YES → Phase 7
  └─ No                        → workflow ends

Phase 7 — Intervention (Compass 🧭 + Oracle 🔮, both T2)
  └─ Compass: generates decarbonisation strategy
     - 🟢 Shallow — quick wins (setpoints, scheduling)
     - 🟡 Medium  — moderate retrofit (LED, glazing)
     - 🔴 Deep    — major retrofit (HVAC, PV, envelope)
  └─ Oracle: concurrently answers stakeholder questions via Slack
```

---

## Decision Rules

### When to run Forge
- **Full run**: every morning (cron) or on manual trigger
- **Targeted re-run**: after Chisel patches an IDF — run only that building
- **Skip**: if `outputs/{building}/parsed/{building}_monthly.csv` exists and is from today (check mtime)

### Detection thresholds (ASHRAE Guideline 14, current detector basis)
- **CV(RMSE) ≤ 15%** — pass
- **NMBE ≤ ±5%** — pass
- Both must pass; either failing → Diagnosis loop

### Iteration cap enforcement (Diagnosis loop)
Before spawning Lens on a building:
1. Read `$NUS_PROJECT_DIR/calibration_log.md`
2. Count existing iterations for that building
3. If ≥ 3: skip Lens, set status = `engineer_review_required`, notify Signal in #private

### When to run Compass
- Only for GT buildings with current ground truth coverage: `FOE6, FOE9, FOE13, FOE18, FOS43, FOS46, FOE1, FOE3, FOE5, FOE10, FOE11, FOE12, FOE15, FOE16, FOE19, FOE20, FOE23, FOE24, FOE26, FOS26, FOS35, FOS41, FOS44`
- Triggered by Signal's decarbonisation decision gate (Phase 6)
- Run Compass on **all GT buildings regardless of calibration status** — scenarios are always useful for planning
- For uncalibrated buildings, Compass uses the available (potentially inaccurate) baseline and adds a `⚠ Uncalibrated baseline` flag to the output so stakeholders know the numbers are indicative only

### When to alert immediately (before daily report)
- Any building fails CV(RMSE) > 30% or NMBE > 15%
- Any Chisel IDF patch awaiting approval
- Compass finds `shallow` or `medium` scenario > 40% reduction potential
- Any agent fails or returns an error

---

## Handoff Payloads

### Orchestrator → Nimbus (Phase 1a — before Forge)
```json
{
  "task": "build_calibrated_epw",
  "month": "2024-08",
  "base_epw": "/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw",
  "out": "/Users/ye/nus-energy/weather/calibrated/2024-08_site_calibrated.epw"
}
```

### Orchestrator → Forge
```json
{
  "task": "simulate",
  "buildings": ["FOE13"],
  "idf_dir": "/Users/ye/nus-energy/idfs/",
  "month": "2024-08",
  "weather_file": "/Users/ye/nus-energy/weather/calibrated/2024-08_site_calibrated.epw",
  "weather_fallback": "/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw",
  "gt_dir": "/Users/ye/nus-energy/ground_truth/parsed/",
  "outputs_dir": "/Users/ye/nus-energy/outputs/"
}
```

### Orchestrator → Radar
```json
{
  "task": "anomaly_check",
  "buildings": ["FOE6", "FOE9", "FOE13", "FOE18", "FOS43", "FOS46"],
  "outputs_dir": "/Users/ye/nus-energy/outputs/",
  "gt_dir": "/Users/ye/nus-energy/ground_truth/parsed/",
  "thresholds": {"cvrmse_pct": 15, "nmbe_pct": 5}
}
```

### Orchestrator → Lens
```json
{
  "task": "diagnose",
  "building": "FOE13",
  "cvrmse": 18.3,
  "nmbe": 6.2,
  "monthly_csv": "/Users/ye/nus-energy/outputs/FOE13/parsed/FOE13_monthly.csv",
  "gt_csv": "/Users/ye/nus-energy/ground_truth/parsed/FOE13_ground_truth.csv",
  "prepared_idf": "/Users/ye/nus-energy/outputs/FOE13/prepared/FOE13_prepared.idf",
  "calibration_log": "/Users/ye/nus-energy/calibration_log.md",
  "iteration_count": 1
}
```

### Orchestrator → Chisel
```json
{
  "task": "recalibrate",
  "building": "FOE13",
  "diagnosis": { "/* full Lens output JSON */": true },
  "parameter_bounds": "/Users/ye/nus-energy/parameter_bounds.json",
  "approval_channel": "#openclaw-alerts",
  "iteration": 2
}
```

### Orchestrator → Oracle (Phase 4 — clarification during approval)
```json
{
  "task": "clarify_approval",
  "building": "FOE13",
  "chisel_proposal": { "/* Chisel parameter proposal JSON */": true },
  "lens_diagnosis": { "/* Lens output JSON */": true },
  "slack_thread": "#openclaw-alerts",
  "context": "Engineer reviewing Chisel's IDF parameter proposal — answer any questions"
}
```

### Orchestrator → Ledger
```json
{
  "task": "daily_report",
  "date": "2026-03-27",
  "buildings_checked": 23,
  "results": [
    {"building": "FOE6",  "cvrmse": 8.1,  "nmbe": 1.2, "status": "ok",       "carbon_scenario": null},
    {"building": "FOE13", "cvrmse": 18.3, "nmbe": 6.2, "status": "warning",  "carbon_scenario": null},
    {"building": "FOS43", "cvrmse": 35.8, "nmbe": 12.1,"status": "critical", "carbon_scenario": null}
  ],
  "recalibration_today": [
    {"building": "FOE6", "parameter": "Cooling_Setpoint_C", "old": 25.0, "new": 23.0, "iteration": 1}
  ],
  "calibration_log": "/Users/ye/nus-energy/calibration_log.md",
  "critical_flags": []
}
```

### Orchestrator → Signal (Phase 6 notification)
```json
{
  "task": "notify_calibration_results",
  "report_path": "/Users/ye/nus-energy/reports/NUS_Campus_Summary_Report.pdf",
  "summary": "/* Ledger summary text */",
  "channel": "#openclaw-alerts",
  "decarbonisation_check": true
}
```

### Orchestrator → Signal (immediate alert)
```json
{
  "type": "anomaly_alert",
  "urgency": "critical",
  "building": "FOS43",
  "data": {"cvrmse": 35.8, "nmbe": 12.1, "worst_months": ["Jun", "Jul"]},
  "action_required": true,
  "action_prompt": "Lens diagnosis in progress"
}
```

### Orchestrator → Compass (Phase 7)
```json
{
  "task": "carbon_scenarios",
  "buildings": ["FOE6", "FOE9", "FOE13", "FOE18", "FOS43", "FOS46"],
  "scenario_types": ["shallow", "medium", "deep"],
  "outputs_dir": "/Users/ye/nus-energy/outputs/",
  "idfs_dir": "/Users/ye/nus-energy/idfs/"
}
```

### Orchestrator → Oracle (Phase 7 — stakeholder Q&A)
```json
{
  "task": "stakeholder_qa",
  "context": "Compass has generated decarbonisation scenarios — answer stakeholder questions via Slack",
  "carbon_outputs_dir": "/Users/ye/nus-energy/outputs/",
  "slack_channel": "#openclaw-alerts"
}
```

---

## Pipeline State Tracking

Write pipeline run state to `$NUS_PROJECT_DIR/outputs/pipeline_state.json`:

```json
{
  "run_date": "2026-03-27",
  "run_started": "2026-03-27T10:00:00+08:00",
  "phases_complete": ["input", "simulate", "detect"],
  "phases_pending": ["diagnose", "report", "notify", "intervene"],
  "buildings": {
    "FOE6":  {"status": "ok",       "cvrmse": 8.1,  "nmbe": 1.2,  "action": null},
    "FOE13": {"status": "warning",  "cvrmse": 18.3, "nmbe": 6.2,  "action": "diagnosis_pending"},
    "FOS43": {"status": "critical", "cvrmse": 35.8, "nmbe": 12.1, "action": "diagnosis_pending"}
  },
  "errors": []
}
```

Update this file after each phase completes. Other agents can read it to understand current pipeline state.

---

## Error Handling

| Failure | Action |
|---|---|
| Forge fails for a building | Log error in `pipeline_state.json`; skip that building; continue pipeline |
| Radar returns no output | Notify Signal: "Pipeline error — parse failed for {building}" |
| Lens returns `engineer_review_required: true` | Notify Signal in #private; do not spawn Chisel |
| Chisel exceeds 3 iterations | Notify Signal in #private; halt recalibration for that building |
| Compass fails | Log error; still run Ledger + Signal with note "carbon scenarios unavailable" |
| Compass runs on uncalibrated building | Add `⚠ Uncalibrated baseline — treat savings as indicative` to the Slack notification and Ledger report |
| Oracle fails | Log error; continue pipeline; human engineer can query manually |
| Signal fails | Log to `pipeline_state.json`; do not retry indefinitely |

---

## Key Paths (all agents share these)

| Resource | Path |
|---|---|
| Project root | `/Users/ye/nus-energy/` |
| IDF files | `/Users/ye/nus-energy/idfs/` |
| Base TMY EPW (fallback) | `/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw` |
| Calibrated EPW dir (Nimbus) | `/Users/ye/nus-energy/weather/calibrated/` |
| Latest conditions (Nimbus) | `/Users/ye/nus-energy/weather/observed/latest_conditions.json` |
| Building registry | `/Users/ye/nus-energy/building_registry.json` |
| Campus shapefile | `/Users/ye/nus-energy/QGISFIle/MasterFile_241127.shp` |
| Outputs | `/Users/ye/nus-energy/outputs/` |
| Ground truth (canonical) | `/Users/ye/nus-energy/ground_truth/ground-truth.csv` |
| Ground truth (parsed fallback) | `/Users/ye/nus-energy/ground_truth/parsed/{BUILDING}_ground_truth.csv` |
| Calibration log | `/Users/ye/nus-energy/calibration_log.md` |
| Parameter bounds | `/Users/ye/nus-energy/parameter_bounds.json` |
| Pipeline state | `/Users/ye/nus-energy/outputs/pipeline_state.json` |
| Reports | `/Users/ye/nus-energy/reports/` |

---

## Ground-Truth Buildings
`FOE6, FOE9, FOE13, FOE18, FOS43, FOS46, FOE1, FOE3, FOE5, FOE10, FOE11, FOE12, FOE15, FOE16, FOE19, FOE20, FOE23, FOE24, FOE26, FOS26, FOS35, FOS41, FOS44` — these can have ASHRAE metrics computed under the current detector setup.
