---
name: nus-slack-server
description: Run and manage the NUS Energy Assistant Slack bot server (Socket Mode). Handles inbound @Energy_assistant mentions and DMs in Slack, routes questions to QueryAgent, and posts answers in thread. Use when starting/stopping the server, checking its status, or diagnosing Slack query failures.
metadata: {"openclaw": {"always": false}}
---

# nus-slack-server — NUS Slack Query Server Skill

Runs `slack_server.py` — a Slack Socket Mode bot that listens for `@Energy_assistant` mentions and DMs, routes questions to `QueryAgent`, and replies in-thread.

This is **inbound** (Slack → pipeline). The `nus-notify` skill handles **outbound** (pipeline → Slack).

---

## Scripts

| Script | Purpose |
|---|---|
| `slack_server.py` | Slack Socket Mode bot — inbound queries + calibration approval handling |
| `pipeline_trigger.py` | External pipeline trigger — kicks off Phases 3→7 for a building without needing a Slack message |

```
{SKILL_DIR}/scripts/slack_server.py
{SKILL_DIR}/scripts/pipeline_trigger.py
```

## Triggering the pipeline externally

Any agent (or manual test) can kick off the post-simulation pipeline for a building
without a Slack @Energy_assistant command by calling `pipeline_trigger.py`:

```bash
# If Slack server is running with PIPELINE_HTTP_PORT set:
PIPELINE_HTTP_PORT=8765 python3 {SKILL_DIR}/scripts/pipeline_trigger.py --building FOE24

# Direct mode (works even without HTTP endpoint):
python3 {SKILL_DIR}/scripts/pipeline_trigger.py --building FOE24 --no-http

# Skip calibration loop (go straight to Report + Carbon scenarios):
python3 {SKILL_DIR}/scripts/pipeline_trigger.py --building FOE24 --no-http --skip-calibration
```

The trigger will:
1. Check ASHRAE metrics for the building using **CVRMSE** and **NMBE** as the primary quantified error metrics
2. If needs calibration → post approval request to Slack + write pending entry
3. If calibrated (or accepted baseline per operator instruction) → run Carbon scenarios (Compass) first, then Report (Ledger), then Signal notification
4. Slack notifications should stay concise and explicitly label intervention result sources (`simulated` or `estimated`)

---

## Dependencies

```bash
pip3 install slack-bolt --break-system-packages
```

`openclaw_agents` is bundled alongside `slack_server.py` — no separate install needed.
`slack_server.py` does `sys.path.insert(0, Path(__file__).parent)` so it finds the package automatically.

## Package Structure

```
scripts/
├── slack_server.py
└── openclaw_agents/
    ├── __init__.py
    └── agents.py          ← QueryAgent, AgentMessage, PipelineState
```

### QueryAgent flow
1. `slack_server.py` instantiates `QueryAgent()`
2. `QueryAgent.run()` calls `query.py` to load live pipeline data
3. Data is injected into Oracle's system prompt
4. Anthropic API call (`claude-sonnet-4-6`) generates the answer
5. Answer returned to `slack_server.py` → posted in Slack thread

### Environment variables for QueryAgent

| Variable | Description | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API key for Oracle | required |
| `NUS_PROJECT_DIR` | Project data root | `/Users/ye/nus-energy` |
| `QUERY_AGENT_MODEL` | Model override | `claude-sonnet-4-6` |
| `QUERY_SCRIPT_PATH` | Path to `query.py` | `/Users/ye/.openclaw/workspace-queryagent/skills/nus-query/scripts/query.py` |

---

## Environment Variables (required)

| Variable | Description |
|---|---|
| `SLACK_BOT_TOKEN` | Bot OAuth token — starts with `xoxb-` |
| `SLACK_APP_TOKEN` | App-level token (Socket Mode) — starts with `xapp-` |

Set these in your environment or via `.env` before starting.

---

## Starting the Server

```bash
SLACK_BOT_TOKEN=xoxb-... SLACK_APP_TOKEN=xapp-... python3 {SKILL_DIR}/scripts/slack_server.py
```

Or in background (persistent):

```bash
nohup python3 {SKILL_DIR}/scripts/slack_server.py > /tmp/slack_server.log 2>&1 &
echo $! > /tmp/slack_server.pid
```

---

## Stopping the Server

```bash
kill $(cat /tmp/slack_server.pid)
```

---

## Checking Status

```bash
# Is the process running?
pgrep -f slack_server.py && echo "RUNNING" || echo "STOPPED"

# Tail logs
tail -50 /tmp/slack_server.log
```

---

## What It Handles

### @Energy_assistant mentions (any channel)
User types in Slack:
```
@Energy_assistant which building is most energy efficient?
@Energy_assistant what is the campus carbon footprint?
@Energy_assistant which buildings need urgent calibration?
@Energy_assistant how much money can we save hitting BCA Gold?
```
Bot replies in-thread with answer from QueryAgent.

### Direct Messages to the bot
User sends a DM directly to `@Energy_assistant`. Bot replies in the same DM thread.

---

## How It Works

1. Slack sends event via WebSocket (Socket Mode — no public URL needed)
2. `slack_server.py` receives `app_mention` or `message` (DM) event
3. Strips the `<@UXXXXXXX>` mention prefix from the text
4. Posts "🔍 Checking campus energy data..." as acknowledgement in thread
5. Calls `QueryAgent.run()` with the question
6. Posts the answer in the same thread

---

## Calibration Approval Flow

When Signal posts a calibration approval request to Slack, it writes an entry to the **pending approvals file** (`/tmp/nus_pending_approvals.json` by default, overrideable via `NUS_PENDING_APPROVALS` env var).

### Pending approvals file schema
```json
{
  "<thread_ts>": {
    "building": "FOE6",
    "iteration": 1,
    "sets": ["Infiltration_ACH=0.5", "Equipment_W_per_m2=10.0"],
    "channel": "#openclaw-alerts",
    "posted_at": 1743300000,
    "status": "pending"
  }
}
```
`status` transitions: `"pending"` → `"approved"` | `"rejected"`

### How the server handles replies in pending-approval threads

1. Any reply in the thread is intercepted **before** normal routing.
2. `"approve"` → marks entry as `"approved"`, runs `patch_idf.py` (write mode) with `--approver <user_id>`, then re-triggers Forge simulation.
3. `"reject"` → marks entry as `"rejected"`, posts deferral confirmation.
4. Any other message → reminds the user that approval is pending; does **not** route to QueryAgent.

### Environment variable for patch script
| Variable | Default |
|---|---|
| `PATCH_IDF_SCRIPT` | `/Users/ye/.openclaw/workspace-recalibrationagent/skills/nus-calibrate/scripts/patch_idf.py` |
| `NUS_PENDING_APPROVALS` | `/tmp/nus_pending_approvals.json` |
| `APPROVAL_ESCALATION_S` | `14400` (4h in seconds) — override to change escalation delay |
| `SLACK_PRIVATE_CHANNEL` | `#private` — channel to post escalation alerts |

**Signal 📣 is responsible for writing the pending approvals file** when it posts a calibration approval request. The slack_server only reads and resolves entries.

## Deduplication

The server tracks processed message IDs in memory (set of up to 500 IDs). Restarting the server clears this — safe to do, Slack won't re-deliver old events.

---

## Slack App Setup (one-time)

The Slack app must have:
- **Socket Mode** enabled (under Settings → Socket Mode)
- **App-Level Token** with `connections:write` scope (for Socket Mode)
- **Bot Token Scopes**: `app_mentions:read`, `chat:write`, `im:history`, `im:write`, `channels:history`
- **Event Subscriptions** (via Socket Mode): subscribe to `app_mention` and `message.im`
- Bot invited to relevant channels: `/invite @Energy_assistant` in **both `#openclaw-alerts` and `#private`**
- ⚠️ **Critical:** the bot must be a member of `#openclaw-alerts` to receive thread reply events (approval "approve"/"reject" replies). If it is not in the channel, Socket Mode will not deliver thread messages and calibration approvals will never fire. Run `/invite @Energy_assistant` in both channels before starting the server.

---

## Relationship to Other Skills

| Skill | Direction | Purpose |
|---|---|---|
| `nus-slack-server` | Inbound (Slack → pipeline) | Handle queries from facilities team |
| `nus-notify` | Outbound (pipeline → Slack) | Send alerts, reports, approval requests |
| `slack` | Transport | Raw send/read/react/pin |

---

## Troubleshooting

| Symptom | Check |
|---|---|
| Bot not responding | `pgrep -f slack_server.py` — is process running? |
| `SLACK_BOT_TOKEN not set` | Export env vars before starting |
| `QueryAgent error` | Is `openclaw_agents` importable? Check Python path |
| Duplicate replies | Dedup cache; restart server to clear |
| Socket disconnect | Socket Mode auto-reconnects; check `slack_server.log` |
