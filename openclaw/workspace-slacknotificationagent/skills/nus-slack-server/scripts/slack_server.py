"""
slack_server.py — OpenClaw Slack Query + Simulation Server (Socket Mode)
"""

import json
import os
import sys
import logging
import re
import subprocess
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.chdir(Path(__file__).parent)

# ── Load .env from workspace if env vars not already set ──────────────────
def _load_dotenv(path: str):
    p = Path(path)
    if not p.exists():
        return
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key = key.strip()
        val = val.strip().strip('"').strip("'")
        if key not in os.environ:
            os.environ[key] = val

_load_dotenv(str(Path(__file__).parents[3] / ".env"))  # workspace-slacknotificationagent/.env
_load_dotenv(str(Path(__file__).parents[4] / ".env"))  # ~/.openclaw/.env

try:
    from slack_bolt import App
    from slack_bolt.adapter.socket_mode import SocketModeHandler
except ImportError:
    print("Run: pip3 install slack-bolt --break-system-packages")
    sys.exit(1)

BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")

if not BOT_TOKEN:
    print("ERROR: SLACK_BOT_TOKEN not set"); sys.exit(1)
if not APP_TOKEN:
    print("ERROR: SLACK_APP_TOKEN not set"); sys.exit(1)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger("openclaw.slack_server")

app = App(token=BOT_TOKEN)

_processed: set = set()
_active_threads: set = set()

# ── Calibration approval state ─────────────────────────────────────────────
# Schema per entry (keyed by Slack thread_ts):
#   {
#     "building": "FOE12",
#     "iteration": 1,
#     "sets": ["Equipment_W_per_m2=20", "Lighting_W_per_m2=12"],
#     "channel": "C0ALEU5MCG0",
#     "status": "pending"   → "approved" | "rejected"
#   }
PENDING_APPROVALS_FILE = Path(
    os.getenv("NUS_PENDING_APPROVALS", "/tmp/nus_pending_approvals.json")
)
_approvals_lock = threading.Lock()


def _load_pending_approvals() -> dict:
    with _approvals_lock:
        if PENDING_APPROVALS_FILE.exists():
            try:
                return json.loads(PENDING_APPROVALS_FILE.read_text())
            except Exception:
                return {}
        return {}


def _save_pending_approvals(data: dict) -> None:
    with _approvals_lock:
        PENDING_APPROVALS_FILE.write_text(json.dumps(data, indent=2))


def _check_thread_for_missed_reply(thread_ts: str, entry: dict) -> bool:
    """
    Query the Slack thread directly and replay a missed approval/rejection.
    Returns True if an actionable reply was found and handled.
    """
    import urllib.request as _urlreq

    if not entry or entry.get("status") != "pending":
        return False

    channel = entry.get("channel", "")
    pend_type = entry.get("type", "calibration")
    if not channel:
        return False

    try:
        url = (f"https://slack.com/api/conversations.replies"
               f"?channel={channel}&ts={thread_ts}&limit=20")
        req = _urlreq.Request(url, headers={"Authorization": f"Bearer {BOT_TOKEN}"})
        resp = json.loads(_urlreq.urlopen(req).read())
        for msg in resp.get("messages", []):
            if msg.get("ts") == thread_ts:
                continue
            text = msg.get("text", "").strip()
            user = msg.get("user", "Slack engineer")
            if pend_type == "intervention":
                if re.match(r"^\s*intervene\s*$", text, re.IGNORECASE):
                    log.info(f"[REPLAY] Missed intervene for {entry['building']}")
                    _resolve_approval(thread_ts, "approved")
                    _handle_intervention(channel, thread_ts, entry, approver=user)
                    return True
                if re.match(r"^\s*skip\s*$", text, re.IGNORECASE):
                    log.info(f"[REPLAY] Missed skip for {entry['building']}")
                    _resolve_approval(thread_ts, "rejected")
                    _handle_intervention_skip(channel, thread_ts, entry["building"])
                    return True
            else:
                if re.match(r"^\s*approve\s*$", text, re.IGNORECASE):
                    log.info(f"[REPLAY] Missed approve for {entry['building']} iter {entry.get('iteration', 1)}")
                    _resolve_approval(thread_ts, "approved")
                    _handle_approval(channel, thread_ts, entry, approver=user)
                    return True
                if re.match(r"^\s*reject\s*$", text, re.IGNORECASE):
                    log.info(f"[REPLAY] Missed reject for {entry['building']} iter {entry.get('iteration', 1)}")
                    _resolve_approval(thread_ts, "rejected")
                    _handle_rejection(channel, thread_ts, entry["building"])
                    return True
    except Exception as e:
        log.warning(f"[REPLAY] Could not check thread {thread_ts}: {e}")

    return False


def _resolve_approval(thread_ts: str, status: str) -> None:
    data = _load_pending_approvals()
    if thread_ts in data:
        data[thread_ts]["status"] = status
        _save_pending_approvals(data)


def _get_pending(thread_ts: str):
    data = _load_pending_approvals()
    entry = data.get(thread_ts)
    if entry and entry.get("status") == "pending":
        return entry
    return None


def _register_pending(building: str, iteration: int, sets: list, channel: str) -> str:
    """Post a calibration approval request to Slack, register it, return thread_ts."""
    text = (
        f"{building} 🔧 Recalibration needed (iteration {iteration})\n"
        f"Reply \"approve\" or \"reject\"."
    )
    try:
        resp = app.client.chat_postMessage(channel=channel, text=text)
        if not resp.get("ok"):
            log.error(f"_register_pending post failed: {resp.get('error')}")
            return ""
        thread_ts = resp["ts"]
    except Exception as e:
        log.error(f"_register_pending exception: {e}")
        return ""

    data = _load_pending_approvals()
    data[thread_ts] = {
        "type":      "calibration",
        "building":  building,
        "iteration": iteration,
        "sets":      sets,
        "channel":   channel,
        "posted_at": int(time.time()),
        "status":    "pending",
    }
    _save_pending_approvals(data)
    return thread_ts


def _register_intervention_pending(building: str, channel: str, thread_ts: str) -> None:
    """
    Post an intervention approval request in the existing pipeline thread and
    register it in the pending approvals file.
    Called after Report completes and carbon scenarios have been computed.

    NOTE: No estimated reduction figures are shown at this stage — scenarios
    are estimates from IDF parameters, not actual simulations. Real numbers
    are only reported after the human approves and the intervention runs.
    """
    # Simple approval prompt — no scenario estimates
    prompt = (
        f"⚡ *Should we run intervention scenarios for {building}?*\n"
        f"Reply *intervene* to simulate 🟢 Shallow → 🟡 Medium → 🔴 Deep tiers.\n"
        f"Reply *skip* to end the pipeline here."
    )

    try:
        resp = app.client.chat_postMessage(channel=channel, text=prompt, thread_ts=thread_ts)
        if not resp.get("ok"):
            log.error(f"_register_intervention_pending post failed: {resp.get('error')}")
            return
        iv_thread_ts = resp["ts"]
    except Exception as e:
        log.error(f"_register_intervention_pending exception: {e}")
        return

    data = _load_pending_approvals()
    data[iv_thread_ts] = {
        "type":      "intervention",
        "building":  building,
        "channel":   channel,
        "thread_ts": thread_ts,   # parent pipeline thread
        "status":    "pending",
    }
    _save_pending_approvals(data)
    log.info(f"[PIPELINE] Intervention approval request registered for {building} (ts={iv_thread_ts})")


_APPROVE_RE    = re.compile(r"^\s*approve\s*$",    re.IGNORECASE)
_REJECT_RE     = re.compile(r"^\s*reject\s*$",     re.IGNORECASE)
_INTERVENE_RE  = re.compile(r"^\s*intervene\s*$",  re.IGNORECASE)
_SKIP_RE       = re.compile(r"^\s*skip\s*$",       re.IGNORECASE)

# ── Path constants ─────────────────────────────────────────────────────────
NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))

SIMULATE_SCRIPT = Path(os.getenv(
    "SIMULATE_SCRIPT",
    "/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/scripts/simulate.py"
))
PATCH_IDF_SCRIPT = Path(os.getenv(
    "PATCH_IDF_SCRIPT",
    "/Users/ye/.openclaw/workspace-recalibrationagent/skills/nus-calibrate/scripts/patch_idf.py"
))
REPORT_SCRIPT = Path(os.getenv(
    "REPORT_SCRIPT",
    "/Users/ye/.openclaw/workspace-reportagent/skills/nus-report/scripts/report.py"
))
CARBON_SCRIPT = Path(os.getenv(
    "CARBON_SCRIPT",
    "/Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py"
))
INTERVENE_SCRIPT = Path(os.getenv(
    "INTERVENE_SCRIPT",
    str(Path(__file__).parent.parent.parent / "nus-intervene" / "scripts" / "intervene.py")
))
IDF_DIR = Path(os.getenv("IDF_DIR", "/Users/ye/nus-energy/idfs"))

ASHRAE_CVRMSE_THRESHOLD    = 15.0
ASHRAE_NMBE_THRESHOLD      = 5.0
DECARB_REDUCTION_THRESHOLD = 40.0  # % — trigger decarb alert if any scenario exceeds this

ALERTS_CHANNEL = os.getenv("SLACK_ALERTS_CHANNEL", "C0ALEU5MCG0")

# Calibration parameter bounds (min, max) — must match parameter_bounds.json
PARAM_BOUNDS = {
    "Infiltration_ACH":   (0.2, 6.0),
    "Equipment_W_per_m2": (3.0, 60.0),
    "Lighting_W_per_m2":  (5.0, 20.0),
}
# Calibration priority order
CALIB_PRIORITY = ["Infiltration_ACH", "Equipment_W_per_m2", "Lighting_W_per_m2"]

# ── Known buildings ────────────────────────────────────────────────────────
ALL_BUILDINGS = [
    "FOE1","FOE3","FOE5","FOE6","FOE9","FOE10","FOE11","FOE12",
    "FOE13","FOE15","FOE16","FOE18","FOE19","FOE20","FOE23","FOE24","FOE26",
    "FOS26","FOS35","FOS41","FOS43","FOS44","FOS46",
]
METERED_BUILDINGS = ALL_BUILDINGS  # all 23 GT buildings

# ── Helpers ────────────────────────────────────────────────────────────────

def _already_processed(event: dict) -> bool:
    eid = event.get("client_msg_id") or event.get("ts", "")
    if eid in _processed:
        return True
    _processed.add(eid)
    if len(_processed) > 500:
        _processed.clear()
    return False


def _post(channel: str, text: str, thread_ts=None) -> None:
    try:
        kwargs = {"channel": channel, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        resp = app.client.chat_postMessage(**kwargs)
        if not resp.get("ok"):
            log.error(f"chat_postMessage failed: {resp.get('error')}")
    except Exception as e:
        log.error(f"chat_postMessage exception: {e}", exc_info=True)


def _find_idf(building: str, idf_dir: Path = None) -> Path:
    """Find IDF for a building, searching subdirectories if needed."""
    base = idf_dir or IDF_DIR
    p = base / f"{building}.idf"
    if p.exists():
        return p
    matches = list(base.rglob(f"{building}.idf"))
    return matches[0] if matches else None


def _check_ashrae(building: str):
    """
    Returns (cvrmse, nmbe, calibrated).
    Reads metrics JSON if available, otherwise computes from CSV.
    Handles current parsed CSV shape robustly, including annual-summary rows.
    """
    try:
        import numpy as _np
        import pandas as _pd

        metrics_path = NUS_PROJECT_DIR / "outputs" / building / f"{building}_calibration_metrics.json"
        if metrics_path.exists():
            m = json.loads(metrics_path.read_text())
            cvrmse = m.get("cvrmse")
            nmbe = m.get("nmbe")
            calibrated = m.get("calibrated")
            if cvrmse is not None and nmbe is not None and calibrated is not None:
                return cvrmse, nmbe, calibrated

        sim_csv = NUS_PROJECT_DIR / "outputs" / building / "parsed" / f"{building}_monthly.csv"
        gt_csv  = NUS_PROJECT_DIR / "ground_truth" / "parsed" / f"{building}_ground_truth.csv"
        if not sim_csv.exists() or not gt_csv.exists():
            return None, None, False

        sim = _pd.read_csv(sim_csv)
        gt  = _pd.read_csv(gt_csv)

        # Keep only monthly rows from simulation output, dropping annual summary rows if present.
        sim = sim.copy()
        sim["month_num"] = _pd.to_numeric(sim.get("month"), errors="coerce")
        sim = sim[sim["month_num"].between(1, 12, inclusive="both")].copy()
        if sim.empty or "electricity_facility_kwh" not in sim.columns:
            return None, None, False
        sim["month_num"] = sim["month_num"].astype(int)
        sim = sim[["month_num", "electricity_facility_kwh"]].sort_values("month_num")

        gt = gt.copy()
        gt["month"] = gt["month"].astype(str)
        gt["year"] = gt["month"].str[:4].astype(int)
        gt["month_num"] = gt["month"].str[5:7].astype(int)
        gt = gt[gt["year"] == gt["year"].max()][["month_num", "measured_kwh"]].sort_values("month_num")

        merged = sim.merge(gt, on="month_num", how="inner")
        if len(merged) != 12:
            log.warning(f"_check_ashrae({building}): expected 12 aligned months, got {len(merged)}")
            return None, None, False

        sim_kwh = merged["electricity_facility_kwh"].astype(float).values
        meas = merged["measured_kwh"].astype(float).values
        diff = sim_kwh - meas
        nmbe = float(diff.sum() / meas.sum() * 100)
        cvrmse = float(_np.sqrt((diff**2).mean()) / meas.mean() * 100)
        calibrated = cvrmse <= ASHRAE_CVRMSE_THRESHOLD and abs(nmbe) <= ASHRAE_NMBE_THRESHOLD
        return round(cvrmse, 2), round(nmbe, 2), calibrated
    except Exception as e:
        log.warning(f"_check_ashrae({building}): {e}")
        return None, None, False


def _read_current_params(building: str) -> dict:
    """Read current IDF parameter values from calibration_metrics.json if available."""
    try:
        metrics_path = NUS_PROJECT_DIR / "outputs" / building / f"{building}_calibration_metrics.json"
        if metrics_path.exists():
            m = json.loads(metrics_path.read_text())
            return m.get("current_params", {})
    except Exception:
        pass
    return {}


def _propose_next_params(building: str, cvrmse: float, nmbe: float, iteration: int) -> list:
    """
    Simple heuristic: propose next calibration parameters based on NMBE direction.
    NMBE > 0 → over-predicting → reduce EPD/LPD or increase ACH
    NMBE < 0 → under-predicting → increase EPD/LPD or reduce ACH
    Returns list of "PARAM=VALUE" strings (max 2).
    """
    current = _read_current_params(building)
    epd = current.get("Equipment_W_per_m2", 8.0)
    lpd = current.get("Lighting_W_per_m2", 5.0)
    ach = current.get("Infiltration_ACH", 6.0)

    sets = []
    epd_min, epd_max = PARAM_BOUNDS["Equipment_W_per_m2"]
    lpd_min, lpd_max = PARAM_BOUNDS["Lighting_W_per_m2"]
    ach_min, ach_max = PARAM_BOUNDS["Infiltration_ACH"]

    if nmbe < -5:
        # Under-predicting — increase EPD, then LPD
        # Step size: 20% of remaining headroom, minimum 1
        step = max(1.0, (epd_max - epd) * 0.3)
        new_epd = min(round(epd + step, 1), epd_max)
        if new_epd != epd:
            sets.append(f"Equipment_W_per_m2={new_epd}")
        step_lpd = max(1.0, (lpd_max - lpd) * 0.3)
        new_lpd = min(round(lpd + step_lpd, 1), lpd_max)
        if new_lpd != lpd and len(sets) < 2:
            sets.append(f"Lighting_W_per_m2={new_lpd}")
    elif nmbe > 5:
        # Over-predicting — reduce EPD, then LPD
        step = max(1.0, (epd - epd_min) * 0.3)
        new_epd = max(round(epd - step, 1), epd_min)
        if new_epd != epd:
            sets.append(f"Equipment_W_per_m2={new_epd}")
        step_lpd = max(1.0, (lpd - lpd_min) * 0.3)
        new_lpd = max(round(lpd - step_lpd, 1), lpd_min)
        if new_lpd != lpd and len(sets) < 2:
            sets.append(f"Lighting_W_per_m2={new_lpd}")

    # If all params are at bounds with no improvement possible, flag it
    if not sets:
        log.warning(f"[CALIB] {building}: all params at bounds, cannot improve further")

    return sets


def _update_metrics_file(building: str, iteration: int, cvrmse: float, nmbe: float,
                          calibrated: bool, current_params: dict) -> None:
    """Write/update the calibration_metrics.json for a building."""
    try:
        metrics_path = NUS_PROJECT_DIR / "outputs" / building / f"{building}_calibration_metrics.json"
        metrics_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "building": building,
            "iteration": iteration,
            "cvrmse": cvrmse,
            "nmbe": nmbe,
            "calibrated": calibrated,
            "ground_truth_year": None,
            "current_params": current_params,
        }
        # Preserve ground_truth_year if already set
        if metrics_path.exists():
            existing = json.loads(metrics_path.read_text())
            data["ground_truth_year"] = existing.get("ground_truth_year")
        metrics_path.write_text(json.dumps(data, indent=2))
    except Exception as e:
        log.warning(f"_update_metrics_file({building}): {e}")


def _load_simulation_baseline(building: str) -> dict:
    """Load annual baseline metrics from parsed monthly output, excluding annual summary rows."""
    try:
        import pandas as _pd

        sim_csv = NUS_PROJECT_DIR / "outputs" / building / "parsed" / f"{building}_monthly.csv"
        if not sim_csv.exists():
            return {}
        df = _pd.read_csv(sim_csv)
        if "month" in df.columns:
            month_num = _pd.to_numeric(df["month"], errors="coerce")
            df = df[month_num.between(1, 12, inclusive="both")].copy()
        if df.empty:
            return {}

        annual_kwh = None
        for col in ["cooling_elec_adj_kwh", "total_electricity_kwh_incl_cooling_equiv", "electricity_facility_kwh"]:
            if col in df.columns:
                base = df["electricity_facility_kwh"].sum() if "electricity_facility_kwh" in df.columns else 0.0
                annual_kwh = base + df[col].sum() if col == "cooling_elec_adj_kwh" else df[col].sum()
                break
        if annual_kwh is None:
            annual_kwh = df["electricity_facility_kwh"].sum() if "electricity_facility_kwh" in df.columns else 0.0

        eui = None
        for col in ["eui_adj_kwh_m2", "eui_total_kwh_m2", "eui_kwh_m2"]:
            if col in df.columns:
                eui = df[col].sum()
                break

        carbon = None
        if "carbon_tco2e" in df.columns:
            carbon = float(df["carbon_tco2e"].sum())
            if annual_kwh and "electricity_facility_kwh" in df.columns and annual_kwh > df["electricity_facility_kwh"].sum():
                carbon = annual_kwh * 0.4168 / 1000.0

        return {
            "annual_kwh": float(annual_kwh),
            "eui_kwh_m2": round(float(eui), 1) if eui is not None else None,
            "carbon_tco2e": round(float(carbon), 1) if carbon is not None else None,
        }
    except Exception as e:
        log.warning(f"_load_simulation_baseline({building}): {e}")
        return {}


def _bca_tier_for_eui(eui) -> str:  # float or None
    if eui is None:
        return "—"
    if eui <= 85:
        return "✅ BCA Platinum (≤85)"
    if eui <= 100:
        return "✅ BCA GoldPlus (≤100)"
    if eui <= 115:
        return "⚠️ BCA Gold (≤115)"
    if eui <= 130:
        return "⚠️ BCA Certified (≤130)"
    return "🔴 Below BCA Certified"


def _post_final_results_to_slack(channel: str, thread_ts: str, building: str,
                                 cvrmse: float = None, nmbe: float = None,
                                 calibrated: bool = False) -> None:
    baseline = _load_simulation_baseline(building)
    if not baseline:
        _post(channel, f"⚠️ *{building}* final results unavailable — missing parsed output.", thread_ts)
        return

    carbon_json = NUS_PROJECT_DIR / "outputs" / building / "carbon" / f"{building}_carbon_scenarios.json"
    scenario_lines = []
    if carbon_json.exists():
        try:
            data = json.loads(carbon_json.read_text())
            scenario_map = {}
            for s in data.get("scenarios", []):
                sid = (s.get("id") or "").lower()
                scenario_map[sid] = s
            for sid, label in [("shallow", "🟢 Zero cost"), ("zero_cost", "🟢 Zero cost"), ("medium", "🟡 Medium cost"), ("deep", "🔴 High cost")]:
                if sid in scenario_map:
                    scenario_lines.append(f"{label}: {scenario_map[sid].get('reduction_pct', 0):.0f}% reduction")
        except Exception as e:
            log.warning(f"_post_final_results_to_slack({building}) scenario parse: {e}")

    header_icon = "✅" if calibrated else "⚠️"
    lines = [
        f"{building} {header_icon} Final results",
        "",
        f"Annual kWh incl. cooling equiv: {baseline['annual_kwh']:,.0f} kWh",
        f"EUI incl. cooling: {baseline['eui_kwh_m2']:.1f} kWh/m²/yr  ({_bca_tier_for_eui(baseline['eui_kwh_m2'])})" if baseline.get("eui_kwh_m2") is not None else "EUI incl. cooling: —",
        f"Carbon incl. cooling: {(baseline.get('carbon_tco2e') or baseline.get('annual_tco2e')):.1f} tCO₂e/yr" if (baseline.get('carbon_tco2e') or baseline.get('annual_tco2e')) is not None else "Carbon incl. cooling: —",
    ]
    if cvrmse is not None and nmbe is not None:
        status_str = "✅ Calibrated" if calibrated else "⚠️ Needs recalibration"
        lines += ["", f"📐 CVRMSE {cvrmse:.1f}%  NMBE {nmbe:+.1f}%  {status_str}"]
    if scenario_lines:
        lines += [""] + scenario_lines
    _post(channel, "\n".join(lines), thread_ts)


# ── Post-calibration pipeline (Phases 5 → 6 → 7) ─────────────────────────

def _run_post_calibration_pipeline(channel: str, thread_ts: str, building: str, skipped_calibration: bool = False) -> None:
    """
    Phase 5 (Report/Ledger) → Phase 6a (Carbon Scenarios/Compass)
    → Phase 6b (Signal notification + HUMAN intervention approval gate).

    Flow:
      1. Ledger generates calibration report
      2. Compass computes carbon reduction scenarios (3 tiers)
      3. Signal posts results summary to Slack
      4. STOP — post intervention approval prompt:
           "Reply 'intervene' to apply scenarios / 'skip' to end"
      5. Human replies → _handle_intervention() or pipeline ends

    Carbon scenarios and actual IDF interventions are NEVER applied
    automatically — they require explicit human approval in Slack.

    Called after ASHRAE pass. Runs in calling thread (already a background worker).
    """
    env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}

    # ── Phase 5: Report (Ledger 📊) ─────────────────────────────────────────
    log.info(f"[LEDGER] Generating report for {building}")
    try:
        r = subprocess.run(
            ["python3", str(REPORT_SCRIPT), "--building", building],
            capture_output=True, text=True, timeout=300,
            env=env, cwd=str(NUS_PROJECT_DIR),
        )
        if r.returncode == 0:
            log.info(f"[LEDGER] Report archived for {building}")
        else:
            err = (r.stderr or r.stdout)[-300:]
            log.warning(f"[LEDGER] Report generation failed for {building}: {err[:200]}")
    except subprocess.TimeoutExpired:
        log.warning(f"[LEDGER] Report timed out for {building}")
    except Exception as e:
        log.warning(f"[LEDGER] Report error for {building}: {e}")

    # ── Phase 6a: Carbon Scenarios (Compass 🧭) ──────────────────────────────
    log.info(f"[COMPASS] Analysing carbon reduction scenarios for {building}")
    top_scenario_pct = 0.0
    carbon_ok = False
    try:
        r = subprocess.run(
            ["python3", str(CARBON_SCRIPT),
             "--building", building,
             "--outputs", str(NUS_PROJECT_DIR / "outputs"),
             "--idfs",    str(NUS_PROJECT_DIR / "idfs"),
             "--no-simulate"],
            capture_output=True, text=True, timeout=300,
            env=env, cwd=str(NUS_PROJECT_DIR),
        )
        if r.returncode == 0:
            carbon_json = NUS_PROJECT_DIR / "outputs" / building / "carbon" / f"{building}_carbon_scenarios.json"
            if carbon_json.exists():
                data = json.loads(carbon_json.read_text())
                for s in data.get("scenarios", []):
                    pct = s.get("reduction_pct", 0) or 0
                    if pct > top_scenario_pct:
                        top_scenario_pct = pct
                carbon_ok = True
        else:
            err = (r.stderr or r.stdout)[-300:]
            log.warning(f"[COMPASS] Carbon scenario analysis failed for {building}: {err[:200]}")
    except subprocess.TimeoutExpired:
        log.warning(f"[COMPASS] Carbon scenario analysis timed out for {building}")
    except Exception as e:
        log.warning(f"[COMPASS] Carbon scenario error for {building}: {e}")

    # ── Phase 6b: Signal 📣 — calibration results summary ────────────────────
    cvrmse, nmbe, _ = _check_ashrae(building)
    metrics_str = f"CVRMSE {cvrmse:.1f}%, NMBE {nmbe:+.1f}%" if cvrmse is not None else "metrics unavailable"

    if skipped_calibration:
        _post_final_results_to_slack(channel, thread_ts, building, cvrmse, nmbe, calibrated=False)
        return

    _post_final_results_to_slack(channel, thread_ts, building, cvrmse, nmbe, calibrated=True)

    # ── Phase 7 gate: HUMAN intervention approval ────────────────────────────
    if carbon_ok:
        _register_intervention_pending(building, channel, thread_ts)
    else:
        log.warning(f"[SIGNAL] Carbon scenarios unavailable for {building} — pipeline complete")


# ── Simulation ─────────────────────────────────────────────────────────────

def _run_simulation(channel: str, thread_ts: str, building: str = None,
                    idf_dir: Path = None, iteration: int = 0,
                    approver: str = "") -> None:
    """
    Run EnergyPlus simulation in background thread.
    After completion:
      - ASHRAE pass → _run_post_calibration_pipeline
      - ASHRAE fail + approver set → auto-patch next iteration (no re-ask)
      - ASHRAE fail + no approver → post new Slack approval request
    """
    skill_dir = SIMULATE_SCRIPT.parent.parent
    effective_idf_dir = idf_dir or IDF_DIR

    if not effective_idf_dir.exists():
        log.warning(f"[FORGE] IDF folder not found: {effective_idf_dir}")
        return

    if building:
        idf_path = _find_idf(building, effective_idf_dir)
        if not idf_path:
            log.warning(f"[FORGE] IDF not found for {building} under {effective_idf_dir}")
            return
        cmd = ["python3", str(SIMULATE_SCRIPT), "--idf", str(idf_path)]
        target_label = building
    else:
        cmd = ["python3", str(SIMULATE_SCRIPT), "--idf-dir", str(effective_idf_dir)]
        target_label = f"all IDFs in `{effective_idf_dir.name}`"

    env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}

    log.info(f"[SIM] Starting: {' '.join(cmd)}")
    log.info(f"[FORGE] Simulating {target_label}")

    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=7200,
            env=env, cwd=str(skill_dir),
        )
        stdout = result.stdout.strip()
        stderr = result.stderr.strip()

        if result.returncode == 0:
            lines = stdout.splitlines()
            start = next(
                (i for i, l in enumerate(lines) if "BATCH RESULTS" in l or "ENERGY SUMMARY" in l),
                max(0, len(lines) - 30),
            )
            summary = "\n".join(lines[start:])
            log.info(f"[FORGE] Simulation complete — {target_label}: {summary[:200]}")

            # Post-simulation pipeline continuation (single building only)
            if building:
                cvrmse, nmbe, calibrated = _check_ashrae(building)
                if cvrmse is not None and nmbe is not None:
                    current_params = _read_current_params(building)
                    _update_metrics_file(building, iteration, cvrmse, nmbe, calibrated, current_params)
                    log.info(f"[SIM] Metrics updated for {building}: CVRMSE={cvrmse} NMBE={nmbe} calibrated={calibrated}")
                else:
                    log.warning(f"[SIM] Could not compute ASHRAE metrics for {building} after simulation")

                if calibrated:
                    log.info(f"[PIPELINE] {building} ASHRAE pass (CVRMSE={cvrmse}%, NMBE={nmbe:+.1f}%) → Report → Compass → Signal")
                    _run_post_calibration_pipeline(channel, thread_ts, building)
                elif cvrmse is not None:
                    next_iter = iteration + 1
                    log.info(f"[PIPELINE] {building} ASHRAE fail (CVRMSE={cvrmse}%, NMBE={nmbe:+.1f}%) → iteration {next_iter}")
                    next_sets = _propose_next_params(building, cvrmse, nmbe, next_iter)
                    if not next_sets:
                        log.warning(f"[PIPELINE] {building}: all calibration parameters at bounds — human review required (not posted to Slack)")
                    elif approver:
                        # Human already approved — auto-iterate without re-asking; no Slack progress post
                        log.info(f"[PIPELINE] {building}: CVRMSE={cvrmse}%, NMBE={nmbe:+.1f}% — auto-applying iteration {next_iter} (approved by {approver})")
                        next_entry = {
                            "building": building,
                            "iteration": next_iter,
                            "sets": next_sets,
                            "channel": channel,
                        }
                        _handle_approval(channel, thread_ts, next_entry, approver)
                    else:
                        # No approver context — need fresh human sign-off
                        _register_pending(building, next_iter, next_sets, channel)
        else:
            err_preview = "\n".join((stderr or stdout).splitlines()[-10:])
            log.warning(f"[FORGE] Simulation failed — {target_label}: {err_preview}")

    except subprocess.TimeoutExpired:
        log.warning(f"[FORGE] Simulation timed out — {target_label}")
    except Exception as e:
        log.error(f"Simulation error: {e}", exc_info=True)
        log.warning(f"[FORGE] Unexpected simulation error: {e}")


# ── Calibration approval handler ───────────────────────────────────────────

def _handle_approval(channel: str, thread_ts: str, entry: dict, approver: str) -> None:
    """Patch IDF then re-simulate. Runs in a background thread."""
    building  = entry["building"]
    iteration = entry.get("iteration", 1)
    sets      = entry.get("sets", [])

    def _worker():
        log.info(f"[APPROVAL] Starting calibration worker for {building} iteration {iteration} on thread {thread_ts}")
        _post(channel,
              f"✅ Approved by *{approver}* , applying iteration {iteration} patch for *{building}*...",
              thread_ts)

        if not PATCH_IDF_SCRIPT.exists():
            log.error(f"[APPROVAL] patch_idf.py not found at {PATCH_IDF_SCRIPT}")
            log.warning(f"[CHISEL] patch_idf.py not found at {PATCH_IDF_SCRIPT}")
            return

        if not sets:
            log.error(f"[APPROVAL] No patch parameters provided for {building} iteration {iteration}")
            log.warning(f"[CHISEL] No approved calibration parameters found for {building}")
            return

        cmd = [
            "python3", str(PATCH_IDF_SCRIPT),
            "--building", building,
            "--iteration", str(iteration),
            "--approver", approver,
        ]
        for s in sets:
            cmd += ["--set", s]

        log.info(f"[PATCH] Running: {' '.join(cmd)}")
        try:
            proc = subprocess.run(
                cmd, capture_output=True, text=True, timeout=120,
                env={**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)},
            )
        except subprocess.TimeoutExpired:
            log.error(f"[PATCH] Timed out for {building} iteration {iteration}")
            log.warning(f"[CHISEL] Patch timed out for {building}")
            return
        except Exception as e:
            log.error(f"[PATCH] Unexpected exception for {building}: {e}", exc_info=True)
            log.warning(f"[CHISEL] Patch crashed for {building}: {e}")
            return

        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout)[-400:]
            log.error(f"[PATCH] Failed for {building} iteration {iteration}: {err}")
            log.warning(f"[CHISEL] Patch failed for {building}: {err[:200]}")
            return

        patch_tail = (proc.stdout or proc.stderr or "").strip().splitlines()[-3:]
        if patch_tail:
            log.info(f"[PATCH] Success for {building} iteration {iteration}: {' | '.join(patch_tail)}")

        log.info(f"[CHISEL] {building} IDF patched (iteration {iteration}) — re-simulating")

        # Re-simulate — pass iteration and approver so the loop auto-continues if ASHRAE still fails
        _run_simulation(channel, thread_ts, building, iteration=iteration, approver=approver)

    t = threading.Thread(target=_worker, daemon=True, name=f"approval-{building}-iter-{iteration}")
    t.start()


def _handle_intervention(channel: str, thread_ts: str, entry: dict, approver: str) -> None:
    """
    Human approved intervention. Run all three tiers (Shallow → Medium → Deep)
    via carbon_scenarios.py (already computed) and post full results to Slack.
    Runs in a background thread.
    """
    building    = entry["building"]
    parent_ts   = entry.get("thread_ts", thread_ts)

    def _worker():
        _post(channel,
              f"⚡ *{building}* — intervention analysis approved by *{approver}*.\n"
              f"Running 🟢 Shallow → 🟡 Medium → 🔴 Deep scenarios...",
              parent_ts)

        carbon_json = NUS_PROJECT_DIR / "outputs" / building / "carbon" / f"{building}_carbon_scenarios.json"
        if not carbon_json.exists():
            # Scenarios not yet computed — run Compass now
            env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}
            r = subprocess.run(
                ["python3", str(CARBON_SCRIPT),
                 "--building", building,
                 "--outputs", str(NUS_PROJECT_DIR / "outputs"),
                 "--idfs",    str(NUS_PROJECT_DIR / "idfs"),
                 "--no-simulate"],
                capture_output=True, text=True, timeout=300,
                env=env, cwd=str(NUS_PROJECT_DIR),
            )
            if r.returncode != 0 or not carbon_json.exists():
                err = (r.stderr or r.stdout)[-300:]
                _post(channel, f"❌ Carbon scenario analysis failed:\n```{err}```", parent_ts)
                return

        try:
            data      = json.loads(carbon_json.read_text())
            baseline  = data.get("baseline", {})
            scenarios = data.get("scenarios", [])
            flags     = data.get("flags", [])

            tier_icons = {"shallow": "🟢", "medium": "🟡", "deep": "🔴"}
            lines = [
                f"🏢 *{building} — Intervention Scenarios*",
                f"Baseline: *{baseline.get('annual_kwh', 0):,.0f} kWh/yr* | "
                f"*{baseline.get('annual_tco2e', 0):.1f} tCO₂e/yr* | "
                f"EUI incl. cooling: {baseline.get('eui_kwh_m2') or '—'} kWh/m² | "
                f"BCA: {baseline.get('bca_rating', '—')}",
                "",
            ]
            for s in scenarios:
                sid   = s.get("id", "")
                icon  = tier_icons.get(sid.lower(), "•")
                pct   = s.get("reduction_pct", 0)
                co2s  = s.get("co2_saved_tco2e", 0)
                co2r  = s.get("co2_remaining_tco2e", 0)
                cap   = s.get("capex_tier", "—")
                trade = s.get("tradeoff", "")
                expl  = s.get("explanation", "")
                source = "+".join(sorted(s.get("sources", []))) or "estimated"
                lines.append(
                    f"{icon} *{s.get('label', sid)}* — *{pct:.1f}%* reduction | "
                    f"{co2s:.1f} tCO₂e/yr saved | Capex: {cap} | Source: {source}"
                )
                if expl:
                    lines.append(f"   _{expl[:180]}{'…' if len(expl) > 180 else ''}_")
                lines.append("")

            if flags:
                lines.append("⚠️ *Notes:*")
                for f in flags:
                    lines.append(f"  • {f}")

            _post(channel, "\n".join(lines), parent_ts)
            _post(channel,
                  f"✅ *{building} pipeline complete.* "
                  f"Intervention scenarios are ready — contact NUS OED to initiate the preferred tier.",
                  parent_ts)
            log.info(f"[PIPELINE] {building} intervention scenarios delivered.")

        except Exception as e:
            log.error(f"_handle_intervention worker error: {e}", exc_info=True)
            _post(channel, f"⚠️ Error reading intervention scenarios: {e}", parent_ts)

    threading.Thread(target=_worker, daemon=True).start()


def _handle_intervention_skip(channel: str, thread_ts: str, building: str) -> None:
    """Human replied 'skip' to intervention prompt — end pipeline cleanly."""
    parent_ts = thread_ts
    _post(channel,
          f"⏭️ *{building}* — intervention skipped. Pipeline complete.",
          parent_ts)
    log.info(f"[PIPELINE] {building} intervention skipped by human.")


def _handle_rejection(channel: str, thread_ts: str, building: str) -> None:
    """On reject: log deferral, still run Report so findings are archived."""
    _post(channel,
          f"⏭️ Recalibration rejected for *{building}* — logging deferral and archiving report.",
          thread_ts)
    log.info(f"[CALIB] Rejected for {building}")

    # Still run Report (Phase 5) so metrics are archived
    def _worker():
        env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}
        try:
            r = subprocess.run(
                ["python3", str(REPORT_SCRIPT), "--building", building],
                capture_output=True, text=True, timeout=120,
                env=env, cwd=str(NUS_PROJECT_DIR),
            )
            if r.returncode == 0:
                _post(channel, f"📄 Report archived for *{building}* (rejected calibration).", thread_ts)
        except Exception as e:
            log.warning(f"Report after reject failed for {building}: {e}")

    threading.Thread(target=_worker, daemon=True).start()


# ── Intervention ───────────────────────────────────────────────────────────

def _run_intervention(channel: str, thread_ts: str, idf_dir: Path, target_pct: float):
    def _worker():
        folder_name = idf_dir.name if idf_dir else "all"
        _post(channel, f"🔧 Chisel is proposing interventions for *{folder_name}* "
              f"(target: {target_pct:.0f}% reduction)...", thread_ts)

        if not INTERVENE_SCRIPT.exists():
            _post(channel, f"❌ intervene.py not found at `{INTERVENE_SCRIPT}`", thread_ts)
            return
        if idf_dir is None or not idf_dir.exists():
            _post(channel, f"❌ IDF folder not found: `{idf_dir}`", thread_ts)
            return

        result_json = Path("/tmp") / f"intervene_{folder_name}_{int(time.time())}.json"
        env = {
            **os.environ,
            "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR),
            "SIMULATE_SCRIPT": str(SIMULATE_SCRIPT),
        }
        cmd = [
            "python3", str(INTERVENE_SCRIPT),
            "--folder", folder_name,
            "--target-pct", str(target_pct),
            "--output-json", str(result_json),
        ]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=1800, env=env)
        except subprocess.TimeoutExpired:
            _post(channel, "⏱️ Intervention simulation timed out (30 min limit).", thread_ts)
            return

        if result_json.exists():
            data = json.loads(result_json.read_text())
            report = _format_intervention_report(data.get("results", []), target_pct, folder_name)
            _post(channel, report, thread_ts)
        else:
            err = (proc.stderr or "No output")[-500:]
            _post(channel, f"❌ Intervention failed:\n```{err}```", thread_ts)

    threading.Thread(target=_worker, daemon=True).start()


def _format_intervention_report(results: list, target_pct: float, folder: str) -> str:
    lines = [f"⚡ *Intervention Report — {folder} (target: {target_pct:.0f}% CO₂ reduction)*\n"]
    good = [r for r in results if "saving_kwh" in r]
    total_baseline   = sum(r.get("baseline_kwh", 0) for r in good)
    total_saving_kwh = sum(r.get("saving_kwh", 0) for r in good)
    total_tco2e      = sum(r.get("saving_tco2e", 0) for r in good)
    total_sgd        = sum(r.get("saving_sgd", 0) for r in good)
    if total_baseline > 0:
        overall_pct = total_saving_kwh / total_baseline * 100
        lines.append(f"*Portfolio: {overall_pct:.1f}% reduction | "
                     f"{total_tco2e:.0f} tCO₂e/yr saved | SGD {total_sgd:,}/yr*\n")
    lines.append("")
    for r in results:
        b = r["building"]
        if "error" in r:
            lines.append(f"• *{b}* — ❌ {r['error']}")
            continue
        icon = "✅" if r.get("meets_target") else "⚠️"
        lines.append(f"{icon} *{b}* — {r['saving_pct']}% saved "
                     f"({r['saving_kwh']:,} kWh | {r['saving_tco2e']} tCO₂e | SGD {r['saving_sgd']:,}/yr)")
        for c in r.get("changes", []):
            lines.append(f"   └ {c['description']}: {c['from']} → {c['to']}")
    lines.append("\n_Patched IDFs saved under `outputs/<building>/intervention/idfs/`._")
    return "\n".join(lines)


# ── Query / calibration status ─────────────────────────────────────────────

def _ask_query_agent(question: str) -> str:
    try:
        from openclaw_agents.agents import QueryAgent, AgentMessage, PipelineState
        agent  = QueryAgent()
        msg    = AgentMessage(sender="slack_server", receiver="query",
                              building="CAMPUS", stage="query",
                              payload={"question": question})
        state  = PipelineState(building="CAMPUS")
        result = agent.run(msg, state)
        return result.payload.get("answer", "Sorry, I could not answer that.")
    except Exception as e:
        log.error(f"QueryAgent error: {e}", exc_info=True)
        return f"⚠️ OpenClaw error: {str(e)}"


def _get_carbon_scenarios_reply(building: str) -> str:
    """
    Read carbon_scenarios.json for a building and return a formatted Slack message
    describing all three intervention tiers (shallow / medium / deep).
    Returns None if no scenarios file exists yet.
    """
    carbon_json = NUS_PROJECT_DIR / "outputs" / building / "carbon" / f"{building}_carbon_scenarios.json"
    if not carbon_json.exists():
        return None
    try:
        data = json.loads(carbon_json.read_text())
        baseline = data.get("baseline", {})
        scenarios = data.get("scenarios", [])
        flags = data.get("flags", [])

        annual_kwh  = baseline.get("annual_kwh", 0)
        annual_co2  = baseline.get("annual_tco2e", 0)
        bca         = baseline.get("bca_rating", "—")

        lines = [
            f"🏢 *{building} — Carbon Reduction Scenarios*",
            f"Baseline: *{annual_kwh:,.0f} kWh/yr* | *{annual_co2:.1f} tCO₂e/yr* | BCA: {bca}",
            "",
        ]

        tier_icons = {"shallow": "🟢", "medium": "🟡", "deep": "🔴"}
        for s in scenarios:
            sid   = s.get("id", "")
            label = s.get("label", sid.title())
            icon  = tier_icons.get(sid.lower(), "•")
            pct   = s.get("reduction_pct", 0)
            co2s  = s.get("co2_saved_tco2e", 0)
            co2r  = s.get("co2_remaining_tco2e", 0)
            capex = s.get("capex_tier", "—")
            expl  = s.get("explanation", "")
            trade = s.get("tradeoff", "")

            lines.append(f"{icon} *{label}* — {pct:.1f}% reduction | {co2s:.1f} tCO₂e/yr saved | Capex: {capex}")
            if expl:
                lines.append(f"   _{expl[:300]}{'…' if len(expl)>300 else ''}_")
            if trade:
                lines.append(f"   *Trade-off:* {trade}")
            lines.append("")

        if flags:
            lines.append("⚠️ *Notes:*")
            for f in flags:
                lines.append(f"  • {f}")

        return "\n".join(lines)
    except Exception as e:
        log.warning(f"_get_carbon_scenarios_reply({building}): {e}")
        return None


def _is_simulation_request(text: str) -> bool:
    return bool(re.search(
        r"\b(run|start|launch|trigger|simulate|re-?run|rerun|kick.?off)\b.{0,30}"
        r"\b(forge|simulat|energyplus|idf|building)\b"
        r"|\b(forge|simulation|energyplus)\b.{0,30}\b(run|start|go|launch|now)\b"
        r"|\brun\s+(all\s+)?(simulation|forge|buildings?)\b"
        r"|\bsimulat\w*\s+(building|all|campus|everything|foe|fos)\b",
        text, re.IGNORECASE,
    ))


def _is_calibration_status_request(text: str) -> bool:
    return bool(re.search(
        r"\b(recalibration|calibration)\b.{0,60}\b(approved|done|finished|complete|status|result)\b"
        r"|\b(recalibration|calibration)\s+status\b"
        r"|\brecalibration\s+for\s+\w+\b",
        text, re.IGNORECASE,
    ))


def _is_intervention_request(text: str) -> bool:
    return bool(re.search(
        # Action verbs + intervention nouns
        r"\b(propose|suggest|recommend|apply|run|simulate|test|find|show|tell|explain|describe|detail|list|what|give)\b"
        r".{0,40}\b(intervention|measure|retrofit|saving|reduction|scenario|option|decarboni[sz])\b"
        # Or reduction verbs + energy/carbon nouns
        r"|\b(reduce|cut|lower|decrease)\b.{0,30}\b(carbon|emission|energy|co2|consumption)\b"
        # Or direct scenario keywords
        r"|\b(shallow|medium|deep)\b.{0,20}\b(scenario|retrofit|intervention|option)\b"
        r"|\bcarbon.{0,20}\b(scenario|option|plan|reduction)\b"
        r"|\bintervention.{0,20}\b(option|detail|plan|for|of)\b",
        text, re.IGNORECASE,
    ))


def _extract_building(text: str):
    upper = text.upper()
    for b in ALL_BUILDINGS:
        if b in upper:
            return b
    return None


def _extract_idf_dir(text: str):
    m = re.search(r"\b(?:on|from|in|folder|directory|dir)\s+(?:folder\s+)?([\w\-./]+idfs?/[\w\-./]+)",
                  text, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(idfs?/[\w\-./]+)", text, re.IGNORECASE)
    if m:
        raw = m.group(1).rstrip("/")
        p = Path(raw)
        if not p.is_absolute():
            parts = p.parts
            try:
                idx = next(i for i, part in enumerate(parts) if part.lower() in ("idfs", "idf"))
                p = NUS_PROJECT_DIR / Path(*parts[idx:])
            except StopIteration:
                p = NUS_PROJECT_DIR / p
        return p
    m2 = re.search(r"\b(?:folder|dir|in|on|from)\s+([A-Z][A-Z0-9]+(?:[_\-][A-Z0-9]+)+)\b",
                   text, re.IGNORECASE)
    if m2:
        archetype = m2.group(1).upper()
        for base in [NUS_PROJECT_DIR / "idfs", IDF_DIR]:
            candidate = base / archetype
            if candidate.exists():
                return candidate
    return None


def _extract_target_pct(text: str) -> float:
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:%|percent)", text, re.IGNORECASE)
    return float(m.group(1)) if m else 5.0


def _get_calibration_status(text: str) -> str:
    upper = text.upper()
    building = next((b for b in METERED_BUILDINGS if b in upper), None)
    calib_log = NUS_PROJECT_DIR / "Calibration_log.md"
    if not calib_log.exists():
        return "⚠️ No Calibration_log.md found yet."
    log_text = calib_log.read_text()
    if building:
        m = re.search(rf"## {building} — Calibration.*?(?=\n## |\Z)", log_text, re.DOTALL)
        if not m:
            return (f"⏳ *{building}* calibration in progress." if f"## {building}" in log_text
                    else f"ℹ️ No calibration record found for *{building}*.")
        return f"✅ *{building} calibration status:*\n\n{m.group(0).strip()}"
    completed = re.findall(r"## (\w+) — Calibration Pipeline Complete", log_text)
    if not completed:
        return "ℹ️ No completed calibrations found yet."
    return (f"✅ *Completed calibrations ({len(completed)}):* " + ", ".join(completed) +
            "\n\nAsk about a specific building for full results.")


# ── Dispatch ───────────────────────────────────────────────────────────────

def _dispatch(text: str, channel: str, thread_ts: str, user_id: str = "") -> None:
    _active_threads.add(thread_ts)
    if len(_active_threads) > 200:
        excess = len(_active_threads) - 100
        for t in list(_active_threads)[:excess]:
            _active_threads.discard(t)

    # Pending approval gate — checked first (calibration or intervention)
    pending = _get_pending(thread_ts)
    if pending and _check_thread_for_missed_reply(thread_ts, pending):
        return
    if pending:
        building  = pending["building"]
        pend_type = pending.get("type", "calibration")
        log.info(f"[PENDING] Received reply '{text}' for {building} (type={pend_type}, ts={thread_ts})")

        if pend_type == "intervention":
            if _INTERVENE_RE.match(text):
                _resolve_approval(thread_ts, "approved")
                _handle_intervention(channel, thread_ts, pending, approver=user_id or "Slack engineer")
            elif _SKIP_RE.match(text):
                _resolve_approval(thread_ts, "rejected")
                _handle_intervention_skip(channel, thread_ts, building)
            else:
                _post(channel,
                      f"⏳ *{building}* intervention approval pending.\n"
                      f"Reply *intervene* to run scenarios or *skip* to end.",
                      thread_ts)
        else:
            # calibration approval
            if _APPROVE_RE.match(text):
                _resolve_approval(thread_ts, "approved")
                _handle_approval(channel, thread_ts, pending, approver=user_id or "Slack engineer")
            elif _REJECT_RE.match(text):
                _resolve_approval(thread_ts, "rejected")
                _handle_rejection(channel, thread_ts, building)
            else:
                _post(channel,
                      f"⏳ *{building}* recalibration is pending approval.\n"
                      f"Reply *approve* to proceed or *reject* to skip.",
                      thread_ts)
        return

    if _is_calibration_status_request(text):
        _post(channel, _get_calibration_status(text), thread_ts)
    elif _is_intervention_request(text):
        building = _extract_building(text)
        if building:
            # If carbon scenarios already exist for this building, serve them directly
            reply = _get_carbon_scenarios_reply(building)
            if reply:
                _post(channel, reply, thread_ts)
                return
        # No pre-computed scenarios — run the intervention pipeline
        idf_dir    = _extract_idf_dir(text)
        target_pct = _extract_target_pct(text)
        _run_intervention(channel, thread_ts, idf_dir, target_pct)
    elif _is_simulation_request(text):
        building = _extract_building(text)
        idf_dir  = _extract_idf_dir(text)
        threading.Thread(
            target=_run_simulation,
            args=(channel, thread_ts, building, idf_dir, 0),
            daemon=True,
        ).start()
    else:
        _post(channel, "🔍 Checking campus energy data...", thread_ts)
        answer = _ask_query_agent(text)
        _post(channel, answer, thread_ts)


# ── Event handlers ─────────────────────────────────────────────────────────

@app.event({"type": "message"})
def debug_all_messages(event, **kwargs):
    log.info(f"[DEBUG] bot_id={event.get('bot_id','—')} subtype={event.get('subtype','—')} "
             f"channel_type={event.get('channel_type','—')} text={event.get('text','')[:80]}")


@app.event("app_mention")
def handle_mention(event, **kwargs):
    if event.get("bot_id") or _already_processed(event):
        return
    text = event.get("text", "")
    if "<@" in text:
        text = text.split(">", 1)[-1].strip()
    channel   = event.get("channel")
    thread_ts = event.get("thread_ts") or event.get("ts")
    if not text:
        _post(channel, "Hi! Ask me anything about NUS campus energy, or say *run Forge* to start a simulation.", thread_ts)
        return
    log.info(f"Mention in {channel}: '{text}'")
    _dispatch(text, channel, thread_ts, user_id=event.get("user", ""))


@app.event("message")
def handle_message(event, **kwargs):
    if event.get("bot_id") or event.get("subtype") or _already_processed(event):
        return
    channel_type = event.get("channel_type")
    thread_ts    = event.get("thread_ts")

    # DMs — always respond
    if channel_type == "im":
        text    = event.get("text", "").strip()
        channel = event.get("channel")
        if text:
            log.info(f"DM: '{text}'")
            _dispatch(text, channel, event.get("ts"), user_id=event.get("user", ""))
        return

    # Channel threads — respond if bot participated OR if there's a pending approval
    if thread_ts and (thread_ts in _active_threads or _get_pending(thread_ts)):
        text    = event.get("text", "").strip()
        channel = event.get("channel")
        if text:
            log.info(f"Thread follow-up in {channel}: '{text}'")
            _dispatch(text, channel, thread_ts, user_id=event.get("user", ""))


# ── Startup: replay missed approvals ──────────────────────────────────────

def _replay_missed_approvals() -> None:
    """
    On startup, check all pending approval threads via conversations.replies.
    Catches any 'approve'/'reject' that came in while the server was down.
    """
    time.sleep(3)  # wait for socket connection to stabilise
    pending_all = _load_pending_approvals()
    for thread_ts, entry in list(pending_all.items()):
        _check_thread_for_missed_reply(thread_ts, entry)


# ── 4h escalation watchdog ─────────────────────────────────────────────────

ESCALATION_DELAY_S  = int(os.getenv("APPROVAL_ESCALATION_S",  str(4 * 3600)))  # default 4h
ESCALATION_CHANNEL  = os.getenv("SLACK_PRIVATE_CHANNEL", "#private")
WATCHDOG_INTERVAL_S = 60  # check every minute


def _escalation_watchdog() -> None:
    """
    Background thread: every minute, scan pending approvals.
    Any entry still 'pending' after ESCALATION_DELAY_S seconds gets an
    escalation message posted to #private, and 'escalated' flag set so
    we don't repeat it.
    """
    log.info("[WATCHDOG] Escalation watchdog started "
             f"(threshold={ESCALATION_DELAY_S}s, check every {WATCHDOG_INTERVAL_S}s)")
    while True:
        time.sleep(WATCHDOG_INTERVAL_S)
        try:
            now  = int(time.time())
            data = _load_pending_approvals()
            changed = False
            for thread_ts, entry in list(data.items()):
                if entry.get("status") != "pending":
                    continue
                if entry.get("escalated"):
                    continue
                posted_at = entry.get("posted_at")
                if not posted_at:
                    continue
                age = now - posted_at
                if age < ESCALATION_DELAY_S:
                    continue
                # Escalate
                building  = entry.get("building", "unknown")
                iteration = entry.get("iteration", "?")
                orig_channel = entry.get("channel", ALERTS_CHANNEL)
                hours = age // 3600
                try:
                    app.client.chat_postMessage(
                        channel=ESCALATION_CHANNEL,
                        text=(
                            f"🔔 *{building}* recalibration approval (iteration {iteration}) "
                            f"has been waiting {hours}h with no response.\n"
                            f"Please reply *approve* or *reject* in the original thread in "
                            f"{orig_channel}."
                        ),
                    )
                    log.info(f"[WATCHDOG] Escalated {building} iter {iteration} "
                             f"(age={hours}h) to {ESCALATION_CHANNEL}")
                except Exception as e:
                    log.warning(f"[WATCHDOG] Escalation post failed for {building}: {e}")
                entry["escalated"] = True
                changed = True
            if changed:
                _save_pending_approvals(data)
        except Exception as e:
            log.warning(f"[WATCHDOG] Unexpected error: {e}")


# ── HTTP trigger server (optional, for pipeline_trigger.py) ───────────────

PIPELINE_HTTP_PORT = int(os.getenv("PIPELINE_HTTP_PORT", "8765"))

def _start_http_trigger_server():
    """
    Lightweight HTTP server on PIPELINE_HTTP_PORT.
    Accepts POST /trigger {"building": "FOE24", "channel": "#openclaw-alerts"}
    and runs the same pipeline the Slack bot would run.
    Only starts if PIPELINE_HTTP_PORT env var is set.
    """
    if not os.getenv("PIPELINE_HTTP_PORT"):
        return  # not enabled by default

    import http.server
    import socketserver

    class _Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            log.info(f"[HTTP] {fmt % args}")

        def do_POST(self):
            if self.path != "/trigger":
                self.send_response(404)
                self.end_headers()
                return
            try:
                length = int(self.headers.get("Content-Length", 0))
                body   = json.loads(self.rfile.read(length))
                building = body.get("building", "")
                channel  = body.get("channel", ALERTS_CHANNEL)
                skip_calibration = bool(body.get("skip_calibration", False))
                if not building:
                    self.send_response(400)
                    self.end_headers()
                    self.wfile.write(b'{"error": "missing building"}')
                    return
                self.send_response(202)
                self.end_headers()
                self.wfile.write(json.dumps({"ok": True, "building": building}).encode())
                # Run in background thread so HTTP response returns immediately
                threading.Thread(
                    target=_run_building_pipeline,
                    args=(building, channel, skip_calibration),
                    daemon=True,
                ).start()
            except Exception as e:
                log.error(f"[HTTP] /trigger error: {e}")
                self.send_response(500)
                self.end_headers()

    def _serve():
        with socketserver.TCPServer(("127.0.0.1", PIPELINE_HTTP_PORT), _Handler) as httpd:
            log.info(f"[HTTP] Pipeline trigger listening on port {PIPELINE_HTTP_PORT}")
            httpd.serve_forever()

    threading.Thread(target=_serve, daemon=True).start()


def _run_building_pipeline(building: str, channel: str, skip_calibration: bool = False):
    """
    Full pipeline for a building triggered externally (via HTTP or direct call).
    Mirrors what happens after a Slack-triggered simulation:
      - If ASHRAE fails → propose params → post calibration approval request → wait
      - If ASHRAE passes → Phase 5→6b
    """
    cvrmse, nmbe, calibrated = _check_ashrae(building)
    if cvrmse is None:
        _post(channel,
              f"⚠️ *{building}* — No calibration metrics found. "
              f"Run nus-groundtruth first.")
        return

    log.info(f"[PIPELINE] {building}: CVRMSE={cvrmse}%, NMBE={nmbe:+.1f}%, calibrated={calibrated}, skip_calibration={skip_calibration}")
    # No opener post — only 2 Slack message types: calibration approval + final intervention results
    synth_ts = None

    if calibrated or skip_calibration:
        _run_post_calibration_pipeline(channel, synth_ts, building, skipped_calibration=skip_calibration and not calibrated)
    else:
        iteration = 1
        next_sets = _propose_next_params(building, cvrmse, nmbe, iteration)
        if next_sets:
            _register_pending(building, iteration, next_sets, channel)
        else:
            log.warning(f"[PIPELINE] {building}: all calibration parameters at bounds — human review required (not posted to Slack)")


def _run_building_pipeline_no_calibration(building: str, channel: str):
    _run_building_pipeline(building, channel, skip_calibration=True)


# ── Entry point ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print("=" * 60)
    print("OpenClaw — Energy_assistant (Socket Mode)")
    print("=" * 60)
    threading.Thread(target=_replay_missed_approvals, daemon=True).start()
    threading.Thread(target=_escalation_watchdog, daemon=True).start()
    _start_http_trigger_server()
    handler = SocketModeHandler(app, APP_TOKEN)
    handler.start()
