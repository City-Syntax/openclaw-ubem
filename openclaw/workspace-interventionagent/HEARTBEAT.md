# HEARTBEAT.md — CarbonAgent

CarbonAgent does not run on the standard 30-minute heartbeat.
It runs on-demand only, triggered by the orchestrator or Ye directly.

---

## Trigger conditions

- **Post-calibration** — after Chisel completes a recalibration run and updates an IDF
- **Post-simulation** — after Forge re-simulates a building following a patch
- **Manual** — Ye asks "carbon scenarios for BIZ8", "what's the reduction potential for FOE13", or "run carbon analysis"
- **Batch** — Ye asks "carbon analysis for all buildings"

## On trigger

1. Read `outputs/{building}/parsed/{building}_monthly.csv` for the carbon baseline
2. Read the calibrated IDF at `outputs/{building}/calibrated/{building}.idf` (fall back to raw IDF if not present)
3. Extract IDF fingerprint (8 intervention parameters)
4. Run scenario engine (see SOUL.md)
5. Write results to `outputs/{building}/carbon/{building}_carbon_scenarios.json`
6. Return structured result to orchestrator
7. Signal 📣 posts summary to #openclaw-alerts if reduction > 20% found

## Conditions to skip

- `outputs/{building}/parsed/` does not exist → reply: "No parsed data for {building}. Run `nus-parse` first."
- IDF not found → reply: "No IDF found for {building}. Check `NUS_PROJECT_DIR`."
- Already ran for this building today and IDF has not changed → reply: "Carbon scenarios for {building} are current (run {timestamp}). Reply FORCE to re-run."

---

HEARTBEAT_OK if no trigger condition is met.
