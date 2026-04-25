# HEARTBEAT.md — SlackNotificationAgent

SlackNotificationAgent runs on every 30-minute heartbeat for one check only.
All other activity is on-demand, triggered by other agents via the orchestrator.

---

## Every heartbeat (30 min)

- [ ] **Pending approval check**: Are there any calibration proposals in
  `posted.json` with status "awaiting_approval" older than 24 hours?
  If yes → post one reminder in the original thread.
  If reminder already sent → do nothing. Do not send a second reminder.

## On-demand triggers

- Any agent routes a message through orchestrator for Slack delivery
- Format the message using the appropriate template (see SOUL.md)
- Check deduplication before posting
- Post to `#energy-management`
- Update `posted.json` with the new entry

## Conditions to skip

- Finding already posted today for the same building and type → HEARTBEAT_OK
- Outside working hours (before 08:00 or after 22:00 SGT) and not critical
  → hold non-critical messages until 08:00 next working day
- Weekend and not critical → hold until Monday 08:00 SGT

---

HEARTBEAT_OK if no pending approvals are overdue and no messages are queued.
