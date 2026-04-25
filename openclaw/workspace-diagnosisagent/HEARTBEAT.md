# HEARTBEAT.md — DiagnosisAgent

DiagnosisAgent does not run on the standard 30-minute heartbeat.
It runs on-demand only, triggered by the orchestrator after anomalyagent
flags a building.

---

## Trigger conditions

- Orchestrator delegates: "diagnose {BUILDING} — {anomaly summary}"
- Ye asks directly: "why is FOE6 over-predicting?" or "calibrate FOE6"

## On trigger

1. Load evidence for the flagged building (see SOUL.md Step 1)
2. Form hypothesis and compute confidence score
3. If confidence ≥ 0.70 → post proposal to Slack and wait for APPROVE/REJECT
4. If confidence < 0.70 → post uncertainty message, request OED data
5. After APPROVE → pass to recalibrationagent via orchestrator
6. After re-simulation → read new metrics and report delta

## Pending approval tracking

If a proposal is waiting for APPROVE and no response after 24h:
Post one reminder: "🧪 Pending approval for {BUILDING}: {change summary}.
Reply APPROVE or REJECT."
Then wait — do not re-propose or escalate further.

---

HEARTBEAT_OK if no trigger condition is met and no proposals are pending.
