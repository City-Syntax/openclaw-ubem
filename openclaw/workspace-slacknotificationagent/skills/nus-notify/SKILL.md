---
name: nus-notify
description: Format and route NUS energy system notifications to Slack. Two message types only — calibration approval requests and final intervention results. All other pipeline events stay in agent logs.
metadata: {"openclaw": {"always": false}}
---

# nus-notify — NUS Slack Notification Skill

Handles outbound Slack notifications for the NUS energy management pipeline.
Transport is handled by the `slack` skill. This skill defines **what to send, how to format it, and where to route it**.

## ⚠️ Two message types only

| Type | Trigger | Channel |
|---|---|---|
| Calibration approval request | Before any IDF write | `#openclaw-alerts` |
| Final intervention results | After Compass completes all scenarios | `#openclaw-alerts` (+ `#private` if >40% reduction) |

Everything else (simulation progress, anomaly alerts, errors, calibration status, daily summaries) **stays in agent logs — do not post to Slack**.

---

## Payload Schema

```json
{
  "type": "calibration_request | intervention_results",
  "building": "FOE13",
  "data": { ... }
}
```

---

## Message Templates

### Calibration Approval Request
**Minimal — no parameter names, no values, no diffs, no predicted metrics.**
```
{BUILDING} 🔧 Recalibration needed (iteration {N})
Reply "approve" or "reject".
```

### Final Intervention Results
**Concise — one building per message, top result only.**
```
{BUILDING} ⚡ Intervention scenarios complete
Best: {top_scenario} — {reduction}% reduction ({tco2e} tCO₂e/yr)
Reply "details" for full breakdown.
```
- If uncalibrated baseline: append `⚠ Uncalibrated — treat savings as indicative`

---

## Formatting Rules (always apply)

1. One fact per line — facilities team reads on mobile
2. Never use markdown tables
3. Max 6 lines per message
4. Numbers always have units: %, kWh/m²/yr, tCO₂e/yr

---

## Sending via `slack` skill

```json
{
  "action": "send",
  "channel": "slack",
  "to": "#openclaw-alerts",
  "message": "<formatted message here>"
}
```

For `#private` (>40% reduction potential or 4h escalation):
```json
{
  "action": "send",
  "channel": "slack",
  "to": "#private",
  "message": "<formatted message here>"
}
```

---

## Approval Flow

**Calibration changes require Slack approval before any file is written. Hard gate.**

When posting a calibration approval request:
1. Post to `#openclaw-alerts` (minimal format above)
2. Write entry to `/tmp/nus_pending_approvals.json`:
   ```json
   {
     "<thread_ts>": {
       "building": "FOE6",
       "iteration": 1,
       "sets": ["Infiltration_ACH=0.5", "Equipment_W_per_m2=10.0"],
       "channel": "#openclaw-alerts",
       "posted_at": <unix_timestamp>,
       "status": "pending"
     }
   }
   ```
   Use the `ts` from the Slack `chat_postMessage` response as the key. Merge — do not overwrite other entries.
3. `slack_server.py` intercepts replies:
   - `"approve"` → runs `patch_idf.py`, re-triggers Forge, marks `"approved"`
   - `"reject"` → marks `"rejected"`, posts deferral confirmation
   - No reply after **4 hours** → escalation watchdog posts to `#private`; pipeline waits indefinitely

**Signal must never infer approval.** Only an explicit `"approve"` reply in Slack unblocks the IDF write.

---

## Status Icons

| Icon | Meaning |
|---|---|
| ✅ | Calibrated / all good |
| ⚠️ | Warning — needs attention |
| 🔴 | Critical |
| 🔧 | Recalibration |
| ⚡ | Intervention / energy action |

---

## Do NOT do

- Do not post simulation progress, anomaly alerts, errors, or status updates
- Do not interpret or modify the data payload
- Do not make calibration decisions
- Do not fabricate numbers
- Do not post to both channels for the same event (route once, correctly)
