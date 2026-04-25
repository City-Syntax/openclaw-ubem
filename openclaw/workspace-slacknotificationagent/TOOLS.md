# TOOLS.md — SlackNotificationAgent

## Project

| Item | Value |
|---|---|
| NUS_PROJECT_DIR | `/Users/ye/.openclaw` |
| Slack channel | `#energy-management` |
| Slack bot name | `@Energy_assistant` |

## Deduplication state file

```
~/.openclaw/workspace-slacknotificationagent/memory/posted.json
```

Create `memory/` directory if it doesn't exist.
Reset the file at the start of each new day.

## Message length limits

| Message type | Max length |
|---|---|
| Single building finding | 200 words |
| Calibration proposal | 150 words |
| Multi-building table | 400 words |
| Weekly digest | 300 words |

## Slack formatting rules

| Do | Don't |
|---|---|
| `*bold*` for emphasis | `# headers` |
| Bullet lists with `-` | Markdown tables (use plain text alignment) |
| Numbers with units | Bare numbers |
| Thread replies in same thread | New messages for follow-ups |
