# HEARTBEAT.md — AnomalyAgent

AnomalyAgent does not run on the standard 30-minute heartbeat.
It runs on-demand only, triggered by the orchestrator.

---

## Trigger conditions

- Daily morning scan at 09:00 SGT — orchestrator delegates
- Post-simulation — after simulationagent completes any building
- Manual — Ye asks "check all buildings" or "any buildings failing?"

## On trigger

1. Read all `outputs/*/` calibration_metrics.json files
2. Apply detection rules (see SOUL.md)
3. Save anomaly_report JSON
4. Post Slack summary only if anomalies found
5. Return structured result to orchestrator

## Conditions to skip

- `outputs/` is empty → reply: "No simulation data yet. Run simulations first."
- Already scanned in the last 2 hours → reply: "Last scan was {N} min ago —
  results still current. Reply FORCE to re-scan."

---

HEARTBEAT_OK if no trigger condition is met.
