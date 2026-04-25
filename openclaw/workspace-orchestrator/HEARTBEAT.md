# HEARTBEAT.md — OrchestratorAgent

OpenClaw runs this checklist every 30 minutes.
The orchestrator's job during heartbeat is pipeline hygiene — not domain analysis.
Domain checks are delegated to the appropriate agent.

---

## Every heartbeat

- [ ] **Stale pipeline check**: Is any building stuck in `pipeline_state.json`
  with status "running" for more than 60 minutes?
  If yes → post once to Slack: "⚠️ {BUILDING} pipeline has been running for
  {N} min with no result. May have stalled — check `openclaw.log`."
  Then clear that entry from pipeline_state.json.

- [ ] **Pending approvals**: Are there any entries in `calibration_log.md`
  with status "pending re-simulation"?
  If yes → post once: "🔧 Pending re-simulation for {BUILDING} after
  approved calibration change. Reply SIMULATE to proceed or SKIP to defer."

## Daily (once per day, 09:00 SGT)

- [ ] **Morning scan**: Delegate to `anomalyagent`:
  "Run daily anomaly scan across all 5 matched buildings."
  Do not post to Slack yourself — anomalyagent handles its own output.

## Weekly (Monday only)

- [ ] **Weekly digest trigger**: Delegate to `reportagent`:
  "Generate weekly campus energy digest for Slack."
  Stay silent — reportagent posts the digest directly.

## Conditions to skip

- If a pipeline is currently running for any building → HEARTBEAT_OK
- If today is Saturday or Sunday → skip daily scan, run weekly digest only on Monday
- If it is before 08:00 or after 22:00 SGT → HEARTBEAT_OK
