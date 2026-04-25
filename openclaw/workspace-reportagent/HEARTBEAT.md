# HEARTBEAT.md — ReportAgent

ReportAgent runs on two schedules: on-demand and one fixed weekly trigger.

---

## Weekly trigger (Monday 09:00 SGT — fixed)

Generate and post the weekly Slack digest:
1. Read all `outputs/*/` calibration_metrics.json files
2. Compute campus EUI and carbon totals
3. Post digest table to `#energy-management`
4. Do not generate a PDF for the weekly digest — Slack post only

## On-demand triggers

- Orchestrator delegates: "generate report for {BUILDING}"
- Ye asks: "campus summary", "paper-ready results", "report for FOE6"

## On trigger

1. Confirm simulation outputs exist for the requested building(s)
2. Run `report.py --building {B}` or `report.py --campus`
3. Confirm PDF was created at expected path
4. Post to Slack: "📄 Report ready: reports/{B}/{B}_report.pdf"

## Conditions to skip

- `outputs/` is empty → reply: "No simulation data yet — run simulations first."
- Monday digest already posted this week → HEARTBEAT_OK

---

HEARTBEAT_OK if no trigger condition is met.
