# HEARTBEAT.md — SimulationAgent

SimulationAgent does not run on the standard 30-minute heartbeat.
It runs on-demand only, triggered by the orchestrator.

---

## Trigger conditions

- Orchestrator delegates: "simulate {BUILDING}" or "simulate all buildings"
- Post-calibration: recalibrationagent confirms patch applied → orchestrator
  triggers re-simulation of that building

## On trigger

1. Check EnergyPlus is on PATH
2. Confirm IDF exists for requested building(s)
3. Run simulate.py → parse_eso.py
4. Report completion JSON to orchestrator
5. Orchestrator then triggers anomalyagent

## Stale simulation check (this is the one proactive check)

Once per day at 08:00 SGT, check:
- Are all 5 matched buildings simulated within the last 7 days?
- Check timestamps on `outputs/{B}/parsed/{B}_monthly.csv`

If any building is stale → report to orchestrator:
"⚠️ Simulation data stale for {BUILDING} — last run {N} days ago."
Orchestrator decides whether to trigger a re-run.

---

HEARTBEAT_OK if no trigger condition is met and all outputs are fresh.
