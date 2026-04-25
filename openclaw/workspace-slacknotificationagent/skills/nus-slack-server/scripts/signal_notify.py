"""
signal_notify.py — Signal final intervention results notification.

Posts the final intervention results message to #openclaw-alerts after
Compass has completed carbon scenarios. This is ONE of the two permitted
Slack message types (the other being the calibration approval request).

Usage:
    python3 signal_notify.py --building FOE1
    python3 signal_notify.py --building FOE1 --calibrated
    python3 signal_notify.py --building FOE1 --calibrated --dry-run
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error
from pathlib import Path

# Load .env if tokens not already in environment
_DOTENV = Path(__file__).parent.parent.parent.parent / ".env"  # workspace-slacknotificationagent/.env
if _DOTENV.exists():
    for _line in _DOTENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
BOT_TOKEN       = os.getenv("SLACK_BOT_TOKEN", "")
ALERTS_CHANNEL  = os.getenv("SLACK_ALERTS_CHANNEL", "C0ALEU5MCG0")


# ── Load carbon scenarios ─────────────────────────────────────────────────

def load_scenarios(building: str) -> list:
    p = NUS_PROJECT_DIR / "outputs" / building / "carbon" / f"{building}_carbon_scenarios.json"
    if not p.exists():
        return []
    try:
        return json.loads(p.read_text()).get("scenarios", [])
    except Exception:
        return []


# ── Build message ─────────────────────────────────────────────────────────

def build_message(building: str, scenarios: list, calibrated: bool) -> str:
    icon = "✅" if calibrated else "⚠️"
    lines = [f"{building} {icon} Intervention scenarios complete"]

    tier_map = {"shallow": "🟢 Shallow", "medium": "🟡 Medium", "deep": "🔴 Deep"}
    for s in scenarios:
        sid   = (s.get("id") or "").lower()
        label = tier_map.get(sid, s.get("label") or sid)
        pct   = s.get("reduction_pct")
        if pct is not None:
            lines.append(f"{label}: {pct:.0f}% reduction")

    if scenarios:
        best     = max(scenarios, key=lambda s: s.get("reduction_pct") or 0)
        best_lbl = best.get("label") or best.get("id") or "—"
        co2      = best.get("co2_saved_tco2e") or best.get("carbon_saved_tco2e")
        co2_str  = f" — {co2:.0f} tCO₂e/yr saved" if co2 else ""
        lines.append(f"Best: {best_lbl}{co2_str}")

    if not calibrated:
        lines.append("⚠ Uncalibrated — treat savings as indicative")

    return "\n".join(lines)


# ── Post to Slack ─────────────────────────────────────────────────────────

def post_slack(channel: str, text: str) -> bool:
    if not BOT_TOKEN:
        print(f"  [Slack] No SLACK_BOT_TOKEN — cannot post.")
        return False
    payload = {"channel": channel, "text": text}
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {BOT_TOKEN}",
            "Content-Type": "application/json; charset=utf-8",
        },
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if resp.get("ok"):
            return True
        print(f"  [Slack] Error: {resp.get('error')}")
        return False
    except Exception as e:
        print(f"  [Slack] HTTP error: {e}")
        return False


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Signal final intervention results notification")
    parser.add_argument("--building",   required=True)
    parser.add_argument("--calibrated", action="store_true", default=False)
    parser.add_argument("--dry-run",    action="store_true", default=False)
    parser.add_argument("--channel",    default=ALERTS_CHANNEL)
    args = parser.parse_args()

    scenarios = load_scenarios(args.building)
    if not scenarios:
        print(f"  ⚠ No carbon scenarios found for {args.building} — skipping Signal.")
        sys.exit(0)

    msg = build_message(args.building, scenarios, args.calibrated)

    if args.dry_run:
        print(f"\n[DRY-RUN] Would post to {args.channel}:")
        for line in msg.splitlines():
            print(f"  {line}")
        print()
        sys.exit(0)

    ok = post_slack(args.channel, msg)
    if ok:
        print(f"  ✅ Signal: final results posted for {args.building}")
    else:
        print(f"  ❌ Signal: failed to post for {args.building}")
        sys.exit(1)


if __name__ == "__main__":
    main()
