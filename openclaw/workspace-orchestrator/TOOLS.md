# TOOLS.md — OrchestratorAgent

## Project

| Item | Value |
|---|---|
| Project root | `/Users/ye/.openclaw` |
| NUS_PROJECT_DIR | `/Users/ye/nus-energy` |
| Slack channel | `#openclaw-alerts` |
| Slack bot name | `@Energy_assistant` |

## Agent roster

| Agent ID | Role | Trigger when |
|---|---|---|
| `main` | Query answering | Question about data, EUI, carbon, status |
| `anomalyagent` | Anomaly detection | Scan all buildings for MAPE/CVRMSE failures |
| `diagnosisagent` | Root cause analysis | A building is flagged, needs diagnosis |
| `simulationagent` | EnergyPlus runs | Simulate request, or post-calibration re-run |
| `recalibrationagent` | IDF patching | APPROVE received from Ye after diagnosis |
| `reportagent` | PDF generation | Report or campus summary requested |
| `slacknotificationagent` | Slack formatting | Pipeline result needs formatting for Slack |

## Pipeline state file

Track in-progress buildings here to prevent double-triggering:
```
~/.openclaw/workspace-orchestrator/memory/pipeline_state.json
```

Format:
```json
{
  "FOE6": {
    "status": "running",
    "stage": "simulation",
    "started": "2026-03-16T14:00:00",
    "agent": "simulationagent"
  }
}
```

## Delegation command

To delegate to another agent, use the OpenClaw `message` tool:
```
message agent=simulationagent "simulate FOE6"
message agent=anomalyagent "check all 5 buildings"
message agent=diagnosisagent "diagnose FOE6 — CVRMSE 31.2%, over-predicts 28%"
```

## Key file locations

| File | Purpose |
|---|---|
| `calibration_log.md` | Append-only IDF change history |
| `parameter_bounds.json` | Safe calibration ranges |
| `building_registry.json` | Floor areas, building types |
| `outputs/*/parsed/*_monthly.csv` | Simulated monthly kWh |
| `outputs/*/*_calibration_metrics.json` | ASHRAE pass/fail per building |
