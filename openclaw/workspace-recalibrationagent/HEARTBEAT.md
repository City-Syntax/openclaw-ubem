# HEARTBEAT.md — RecalibrationAgent

RecalibrationAgent does not run on the standard 30-minute heartbeat.
It runs on-demand only, triggered by the orchestrator after APPROVE is confirmed.

---

## Trigger conditions

- Orchestrator confirms: "Ye approved changes for {BUILDING} — apply patch"
- Only this. Nothing else triggers this agent.

## On trigger

1. Verify approval source with orchestrator
2. Run patch_idf.py
3. Append to calibration_log.md
4. Report completion to orchestrator
5. Orchestrator triggers simulationagent for re-run

## Pending patch check

Once per heartbeat (30 min), check calibration_log.md for entries with
"Result: pending re-simulation" that are older than 2 hours:
→ Report to orchestrator: "⚠️ {BUILDING} patch was applied {N} hours ago
but re-simulation has not run. Check simulationagent status."

---

HEARTBEAT_OK if no patches are pending and no trigger condition is met.
