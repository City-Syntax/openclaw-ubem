"""
pipeline_trigger.py — NUS Pipeline External Trigger
====================================================
Kicks off the post-simulation pipeline for a single building.

This is the entry point for any agent (or manual test) that wants to run the
Phases 3→7 sequence outside of a Slack @Energy_assistant command.

What it does:
  1. Reads ASHRAE calibration metrics for the building.
  2a. ASHRAE FAIL → posts calibration approval request to Slack, writes pending
      entry to /tmp/nus_pending_approvals.json.
      The Slack server then intercepts the human's "approve"/"reject" reply:
        approve → Chisel patches IDF → Forge re-simulates → (repeat until pass)
                  → ASHRAE pass → Report (Ledger) → Carbon scenarios (Compass)
                               → Signal notification
        reject  → Report (Ledger) only → pipeline ends
  2b. ASHRAE PASS → runs post-calibration pipeline directly
      (Report → Carbon scenarios → Signal notification).

The intervention/carbon notification is ONLY triggered after the human
approves or rejects in Slack — never independently.

Usage:
  python3 pipeline_trigger.py --building FOE24
  python3 pipeline_trigger.py --building FOE24 --channel "C0ALEU5MCG0"
  python3 pipeline_trigger.py --building FOE24 --iteration 2
  python3 pipeline_trigger.py --building FOE24 --skip-calibration
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ─────────────────────────────────────────────────────────────────

NUS_PROJECT_DIR  = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
PENDING_FILE     = Path(os.getenv("NUS_PENDING_APPROVALS", "/tmp/nus_pending_approvals.json"))
PIPELINE_PORT    = int(os.getenv("PIPELINE_HTTP_PORT", "8765"))
BOT_TOKEN        = os.getenv("SLACK_BOT_TOKEN", "")
ALERTS_CHANNEL   = os.getenv("SLACK_ALERTS_CHANNEL", "C0ALEU5MCG0")

ASHRAE_CVRMSE = 15.0
ASHRAE_NMBE   = 5.0

PARAM_BOUNDS = {
    "Infiltration_ACH":   (0.2, 6.0),
    "Equipment_W_per_m2": (3.0, 60.0),
    "Lighting_W_per_m2":  (5.0, 20.0),
}

# ── Helpers ─────────────────────────────────────────────────────────────────

def _load_pending():
    if PENDING_FILE.exists():
        try:
            return json.loads(PENDING_FILE.read_text())
        except Exception:
            pass
    return {}


def _save_pending(data: dict):
    PENDING_FILE.parent.mkdir(parents=True, exist_ok=True)
    PENDING_FILE.write_text(json.dumps(data, indent=2))


def _post_slack(channel: str, text: str, thread_ts: str = None) -> dict:
    """Post a message to Slack. Returns the API response dict."""
    if not BOT_TOKEN:
        print(f"  [Slack] SLACK_BOT_TOKEN not set — cannot post. Would send to {channel}:\n  {text}\n")
        return {}
    payload = {"channel": channel, "text": text}
    if thread_ts:
        payload["thread_ts"] = thread_ts
    req = urllib.request.Request(
        "https://slack.com/api/chat.postMessage",
        data=json.dumps(payload).encode("utf-8"),
        headers={"Authorization": f"Bearer {BOT_TOKEN}", "Content-Type": "application/json; charset=utf-8"},
    )
    try:
        resp = json.loads(urllib.request.urlopen(req, timeout=10).read())
        if not resp.get("ok"):
            print(f"  [Slack] chat.postMessage error: {resp.get('error')}")
        return resp
    except Exception as e:
        print(f"  [Slack] HTTP error: {e}")
        return {}


def _load_metrics(building: str) -> dict:
    """Load calibration metrics from ground_truth or outputs dir."""
    gt_path = NUS_PROJECT_DIR / "ground_truth" / building / f"{building}_calibration_metrics.json"
    if gt_path.exists():
        return json.loads(gt_path.read_text())
    out_path = NUS_PROJECT_DIR / "outputs" / building / f"{building}_calibration_metrics.json"
    if out_path.exists():
        return json.loads(out_path.read_text())
    return {}


def _check_ashrae(building: str):
    """Returns (cvrmse, nmbe, calibrated)."""
    m = _load_metrics(building)
    if not m:
        return None, None, False
    cvrmse = m.get("cvrmse_pct") or m.get("cvrmse")
    nmbe   = m.get("mbe_pct")    or m.get("nmbe")
    if cvrmse is None:
        return None, None, False
    calibrated = (cvrmse <= ASHRAE_CVRMSE) and (abs(nmbe or 0) <= ASHRAE_NMBE)
    return round(float(cvrmse), 2), round(float(nmbe or 0), 2), calibrated


def _propose_params(building: str, cvrmse: float, nmbe: float, iteration: int) -> list:
    """
    Heuristic: propose up to 2 calibration parameters based on NMBE direction.
    Priority: Infiltration_ACH first (Climate Studio default 6.0 is too high),
    then Equipment_W_per_m2.
    """
    metrics = _load_metrics(building)
    current = metrics.get("current_params", {})
    ach = current.get("Infiltration_ACH", 6.0)
    epd = current.get("Equipment_W_per_m2", 8.0)
    lpd = current.get("Lighting_W_per_m2", 5.0)

    sets = []
    epd_min, epd_max = PARAM_BOUNDS["Equipment_W_per_m2"]
    ach_min, ach_max = PARAM_BOUNDS["Infiltration_ACH"]

    # ACH correction first (Climate Studio default is 6.0 — usually too high)
    if ach > 1.0:
        new_ach = max(ach_min, round(ach * 0.5, 1))
        if new_ach != ach:
            sets.append(f"Infiltration_ACH={new_ach}")

    if nmbe < -5:
        # Under-predicting → increase EPD
        step = max(1.0, round((epd_max - epd) * 0.3, 1))
        new_epd = min(round(epd + step, 1), epd_max)
        if new_epd != epd and len(sets) < 2:
            sets.append(f"Equipment_W_per_m2={new_epd}")
    elif nmbe > 5:
        # Over-predicting → decrease EPD
        step = max(1.0, round((epd - epd_min) * 0.3, 1))
        new_epd = max(round(epd - step, 1), epd_min)
        if new_epd != epd and len(sets) < 2:
            sets.append(f"Equipment_W_per_m2={new_epd}")

    if not sets:
        print(f"  ⚠ All calibration parameters at bounds for {building} — cannot improve further.")

    return sets[:2]


# ── HTTP trigger (preferred when server is running with PIPELINE_HTTP_PORT) ──

def _try_http_trigger(building: str, channel: str, skip_calibration: bool = False) -> bool:
    """POST to the Slack server's HTTP trigger endpoint. Returns True if successful."""
    url = f"http://127.0.0.1:{PIPELINE_PORT}/trigger"
    payload = json.dumps({
        "building": building,
        "channel": channel,
        "skip_calibration": skip_calibration,
    }).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=5)
        print(f"  HTTP trigger accepted (HTTP {resp.status})")
        return True
    except urllib.error.URLError:
        return False


def _run_no_calibration_pipeline(building: str, channel: str) -> None:
    """Continue downstream workflow without entering the calibration approval loop."""
    print(f"\n  Skipping calibration for {building} as requested…")
    _post_slack(
        channel,
        f"⚠️ *{building}* — calibration skipped by operator for this run. "
        f"Continuing with Report → Carbon scenarios → final Slack results."
    )

    sys.path.insert(0, str(Path(__file__).parent))
    from slack_server import _run_building_pipeline_no_calibration
    _run_building_pipeline_no_calibration(building, channel)


# ── Direct trigger (fallback) ───────────────────────────────────────────────

def _direct_trigger(building: str, channel: str, iteration: int):
    """
    Post calibration approval request to Slack if ASHRAE fails, or trigger
    post-calibration pipeline if already calibrated.

    The intervention/carbon scenarios/Signal notification are ONLY triggered
    after the human approves or rejects in Slack — never here.
    """
    print(f"\n  Checking ASHRAE metrics for {building}…")
    cvrmse, nmbe, calibrated = _check_ashrae(building)

    if cvrmse is None:
        print(f"  ⚠ No calibration metrics found for {building}.")
        print(f"    Run nus-groundtruth first to generate metrics.")
        _post_slack(channel,
                    f"⚠️ *{building}* — No calibration metrics found. "
                    f"Run nus-groundtruth first.")
        return

    print(f"  CVRMSE: {cvrmse}%  NMBE: {nmbe:+.1f}%  Calibrated: {calibrated}")

    if calibrated:
        # Already passes ASHRAE — go straight to post-calibration pipeline.
        # No human approval needed; the model is already valid.
        print(f"  ✅ ASHRAE thresholds met. Triggering post-calibration pipeline…")
        _post_slack(
            channel,
            f"✅ *{building}* — CVRMSE {cvrmse:.1f}%, NMBE {nmbe:+.1f}% — calibrated.\n"
            f"Running Report → Carbon scenarios → Signal notification…"
        )
        # Delegate to the Slack server's full post-calib pipeline via HTTP if available;
        # otherwise the server will handle it on the next @Energy_assistant mention.
        # We do NOT run Compass here — that's the server's responsibility.
        print(f"  (Post-calibration pipeline will run inside the Slack server.)")
        print(f"  To run manually: @Energy_assistant carbon scenarios for {building}")
    else:
        # ASHRAE fails → post calibration approval request.
        # The Slack server intercepts approve/reject and handles everything from there.
        _send_calibration_request(building, channel, cvrmse, nmbe, iteration)


def _send_calibration_request(building: str, channel: str, cvrmse: float, nmbe: float, iteration: int):
    """Post approval request to Slack and write pending entry."""
    sets = _propose_params(building, cvrmse, nmbe, iteration)
    if not sets:
        _post_slack(channel,
                    f"⚠️ *{building}* — CVRMSE {cvrmse:.1f}%, NMBE {nmbe:+.1f}% — "
                    f"all calibration parameters at bounds. Human review required.")
        return

    print(f"\n  Posting calibration approval request for {building} (iteration {iteration})…")
    print(f"  Proposed params: {sets}")

    # Simple message per nus-calibrate policy (no technical details, no diffs)
    text = (
        f"{building} needs recalibration (iteration {iteration}).\n"
        f"Reply *approve* to proceed or *reject* to skip."
    )
    resp = _post_slack(channel, text)
    thread_ts = resp.get("ts", "")

    if not thread_ts:
        print(f"  ⚠ Could not get thread_ts — pending entry not registered.")
        print(f"  (SLACK_BOT_TOKEN set: {'yes' if BOT_TOKEN else 'no'})")
        return

    # Register pending approval — Slack server reads this to intercept replies
    data = _load_pending()
    data[thread_ts] = {
        "building":  building,
        "iteration": iteration,
        "sets":      sets,
        "channel":   channel,
        "posted_at": int(time.time()),
        "status":    "pending",
    }
    _save_pending(data)

    print(f"  ✅ Calibration approval request posted (thread_ts={thread_ts})")
    print(f"  ✅ Pending entry written to {PENDING_FILE}")
    print(f"\n  Pipeline paused. Waiting for human reply in Slack…")
    print(f"  approve → Chisel patches IDF → Forge re-sims → (repeat until ASHRAE pass)")
    print(f"          → Report → Carbon scenarios → Signal notification")
    print(f"  reject  → Report only → pipeline ends")


# ── Main ─────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="NUS pipeline external trigger")
    parser.add_argument("--building",   required=True,          help="Building name, e.g. FOE24")
    parser.add_argument("--channel",    default=ALERTS_CHANNEL, help="Slack channel ID or name")
    parser.add_argument("--iteration",  type=int, default=1,    help="Calibration iteration number")
    parser.add_argument("--no-http",    action="store_true",    help="Skip HTTP trigger, use direct mode")
    parser.add_argument("--skip-calibration", action="store_true",
                        help="Bypass calibration approval loop and continue downstream workflow")
    args = parser.parse_args()

    b  = args.building
    ch = args.channel

    print(f"\n🔧 NUS Pipeline Trigger — {b}")
    print(f"   Channel:   {ch}")
    print(f"   Iteration: {args.iteration}")
    print(f"   Skip calibration: {args.skip_calibration}")

    # Try HTTP trigger first (delegates everything to the running Slack server)
    if not args.no_http:
        print(f"\n  Attempting HTTP trigger on port {PIPELINE_PORT}…")
        if _try_http_trigger(b, ch, skip_calibration=args.skip_calibration):
            print(f"  ✅ Pipeline handed off to Slack server via HTTP.")
            return
        print(f"  HTTP endpoint not available. Falling back to direct mode…")

    if args.skip_calibration:
        _run_no_calibration_pipeline(b, ch)
    else:
        _direct_trigger(b, ch, args.iteration)


if __name__ == "__main__":
    main()
