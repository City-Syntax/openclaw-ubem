#!/usr/bin/env python3
"""
run_pipeline.py — NUS Energy Management Pipeline Runner (full 7-phase)
=======================================================================
Orchestrates the complete NUS campus energy pipeline end-to-end:

  Phase 0   — Setup (dirs, state, logging)
  Phase 1a  — Weather / EPW (Nimbus)
  Phase 1b  — Ground truth preparation (Radar)
  Phase 2   — EnergyPlus simulation (Forge)
  Phase 3   — Detection gate / ASHRAE metrics (Radar)
  Phase 4   — Diagnosis loop (Lens ⏸ → Chisel → Slack approval → Forge)
  Phase 5   — Report (Ledger)
  Phase 6   — Notification (Signal)
  Phase 7   — Intervention (Compass + Oracle)

Architecture note
-----------------
All agents except Lens have callable Python scripts that run via subprocess.
Lens (DiagnosisAgent) is a pure LLM agent — no script exists. The pipeline:
  1. Writes a structured lens_input.json for each building needing diagnosis
  2. Prints a clear PAUSE banner and exits (or pauses for next building)
  3. Is re-run with --resume-from diagnosis after Lens writes lens_output.json

Usage:
  python3 run_pipeline.py --month 2024-08
  python3 run_pipeline.py --month 2024-08 --buildings FOE6 FOE13
  python3 run_pipeline.py --month 2024-08 --skip-weather --workers 8
  python3 run_pipeline.py --month 2024-08 --dry-run
  python3 run_pipeline.py --month 2024-08 --resume-from diagnosis
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import tempfile
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any
import time

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

# Load .env from SlackNotificationAgent workspace so SLACK_BOT_TOKEN etc. are available
# to all subprocesses spawned by run_pipeline.py.
_DOTENV = Path("/Users/ye/.openclaw/workspace-slacknotificationagent/.env")
if _DOTENV.exists():
    for _line in _DOTENV.read_text().splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _v = _line.split("=", 1)
            os.environ.setdefault(_k.strip(), _v.strip())

NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))

PATHS: dict[str, Path] = {
    "idf_dir":            NUS_PROJECT_DIR / "idfs",
    "gt_dir":             NUS_PROJECT_DIR / "ground_truth" / "parsed",
    "outputs_dir":        NUS_PROJECT_DIR / "outputs",
    "base_epw":           NUS_PROJECT_DIR / "weather" / "SGP_Singapore.486980_IWEC.epw",
    "calibrated_epw_dir": NUS_PROJECT_DIR / "weather" / "calibrated",
    "calibration_log":    NUS_PROJECT_DIR / "calibration_log.md",
    "parameter_bounds":   NUS_PROJECT_DIR / "parameter_bounds.json",
    "pipeline_state":     NUS_PROJECT_DIR / "outputs" / "pipeline_state.json",
    "reports_dir":        NUS_PROJECT_DIR / "outputs" / "reports",
}

SCRIPTS: dict[str, Path] = {
    # Phase 1a — Nimbus (WeatherAgent)
    "fetch_weather":    Path("/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/scripts/fetch_weather.py"),
    "validate_weather": Path("/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/scripts/validate_weather.py"),
    "build_epw":        Path("/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/scripts/build_epw.py"),
    # Phase 1b — Radar prep
    "prepare_gt":       Path("/Users/ye/.openclaw/workspace-anomalyagent/skills/nus-groundtruth/scripts/prepare_ground_truth.py"),
    # Phase 2 — Forge (SimulationAgent)
    "simulate":         Path("/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/scripts/simulate.py"),
    # Phase 3 — Radar parse
    "parse_eso":        Path("/Users/ye/.openclaw/workspace-anomalyagent/skills/nus-parse/scripts/parse_eso.py"),
    # Phase 4 — Chisel (RecalibrationAgent) — only after Lens output + Slack approval
    "patch_idf":        Path("/Users/ye/.openclaw/workspace-recalibrationagent/skills/nus-calibrate/scripts/patch_idf.py"),
    "calibration_loop": Path("/Users/ye/.openclaw/workspace-orchestrator/skills/nus-orchestrate/scripts/calibration_loop.py"),
    # Phase 5 — Ledger (ReportAgent)
    "report":           Path("/Users/ye/.openclaw/workspace-reportagent/skills/nus-report/scripts/report.py"),
    # Phase 6 — Signal (SlackNotificationAgent) — calibration approval only
    "pipeline_trigger": Path("/Users/ye/.openclaw/workspace-slacknotificationagent/skills/nus-slack-server/scripts/pipeline_trigger.py"),
    # Phase 6 deferred — Signal final intervention results (posted after Phase 7)
    "signal_notify":    Path("/Users/ye/.openclaw/workspace-slacknotificationagent/skills/nus-slack-server/scripts/signal_notify.py"),
    # Phase 7 — Compass (InterventionAgent)
    "carbon_scenarios": Path("/Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py"),
    # Phase 7 — Oracle (QueryAgent)
    "query":            Path("/Users/ye/.openclaw/workspace-queryagent/skills/nus-query/scripts/query.py"),
}

GT_BUILDINGS: list[str] = [
    "FOE6",  "FOE9",  "FOE13", "FOE18", "FOS43", "FOS46",
    "FOE1",  "FOE3",  "FOE5",  "FOE10", "FOE11", "FOE12", "FOE15",
    "FOE16", "FOE19", "FOE20", "FOE23", "FOE24", "FOE26",
    "FOS26", "FOS35", "FOS41", "FOS44",
]


def discover_all_buildings() -> list[str]:
    """Return all building stems from every IDF under the project idf dir."""
    seen: set[str] = set()
    for p in PATHS["idf_dir"].rglob("*.idf"):
        seen.add(p.stem)
    return sorted(seen)

PHASE_ORDER = ["weather", "ground_truth", "simulation", "detection",
               "diagnosis", "report", "notify", "intervene"]

# Singapore timezone offset (+08:00)
SGT = timezone(timedelta(hours=8))


# ---------------------------------------------------------------------------
# Logging helpers
# ---------------------------------------------------------------------------

def setup_logging(outputs_dir: Path, month: str) -> logging.Logger:
    """Configure root logger: INFO to stdout + plain log file."""
    outputs_dir.mkdir(parents=True, exist_ok=True)
    log_path = outputs_dir / f"pipeline_{month}.log"

    logger = logging.getLogger("pipeline")
    logger.setLevel(logging.INFO)

    fmt = logging.Formatter("%(asctime)s  %(levelname)-8s  %(message)s",
                            datefmt="%Y-%m-%d %H:%M:%S")

    ch = logging.StreamHandler(sys.stdout)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    fh = logging.FileHandler(log_path, encoding="utf-8")
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    return logger


# ---------------------------------------------------------------------------
# Pipeline state helpers
# ---------------------------------------------------------------------------

def load_state(state_path: Path, month: str) -> dict[str, Any]:
    """Load existing pipeline_state.json or create a fresh one."""
    if state_path.exists():
        try:
            with state_path.open() as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    now = datetime.now(SGT)
    return {
        "run_date":        now.strftime("%Y-%m-%d"),
        "run_started":     now.isoformat(timespec="seconds"),
        "month":           month,
        "epw_source":      None,
        "phases_complete": [],
        "phases_pending":  list(PHASE_ORDER),
        "buildings":       {},
        "needs_diagnosis": [],
        "errors":          [],
    }


def save_state(state: dict[str, Any], state_path: Path) -> None:
    """Atomically write pipeline_state.json via temp file + rename."""
    state_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = state_path.with_suffix(".tmp")
    with tmp.open("w") as f:
        json.dump(state, f, indent=2)
    tmp.replace(state_path)


def mark_phase(state: dict[str, Any], phase: str) -> None:
    """Record a completed phase (idempotent) and remove from pending."""
    if phase not in state["phases_complete"]:
        state["phases_complete"].append(phase)
    pending = state.get("phases_pending", [])
    if phase in pending:
        pending.remove(phase)
        state["phases_pending"] = pending


def record_error(state: dict[str, Any], msg: str) -> None:
    state.setdefault("errors", []).append(msg)


def phase_complete(state: dict[str, Any], phase: str) -> bool:
    """Return True if the phase is already recorded as complete."""
    return phase in state.get("phases_complete", [])


def _ensure_building_state(state: dict[str, Any], building: str) -> dict[str, Any]:
    bstate = state.setdefault("buildings", {}).setdefault(building, {})
    bstate.setdefault("phase_times", {})
    return bstate


def _mark_building_phase_start(state: dict[str, Any], building: str, phase: str) -> None:
    bstate = _ensure_building_state(state, building)
    phase_times = bstate.setdefault("phase_times", {}).setdefault(phase, {})
    phase_times["started_at"] = datetime.now(SGT).isoformat(timespec="seconds")
    phase_times["start_epoch"] = int(time.time())



def _mark_building_phase_end(state: dict[str, Any], building: str, phase: str, status: str = "done") -> None:
    bstate = _ensure_building_state(state, building)
    phase_times = bstate.setdefault("phase_times", {}).setdefault(phase, {})
    end_epoch = int(time.time())
    phase_times["ended_at"] = datetime.now(SGT).isoformat(timespec="seconds")
    phase_times["end_epoch"] = end_epoch
    if "start_epoch" in phase_times:
        phase_times["duration_seconds"] = max(0, end_epoch - int(phase_times["start_epoch"]))
    phase_times["status"] = status


# ---------------------------------------------------------------------------
# Subprocess runner
# ---------------------------------------------------------------------------

# Per-script subprocess timeouts (seconds). None = no limit.
SCRIPT_TIMEOUTS: dict[str, int | None] = {
    "simulate":        600,   # EnergyPlus full-year run
    "report":          300,   # chart render can be slow for campus
    "carbon_scenarios": 60,   # estimates-only (--no-simulate); full EP run would be 600s
    "signal_notify":    30,
    "pipeline_trigger": 30,
    "patch_idf":        60,
    "query":            60,
}
DEFAULT_SCRIPT_TIMEOUT = 120


def run_script(
    script_key: str,
    args: list[str],
    *,
    dry_run: bool = False,
    timeout: int | None = None,
) -> tuple[int, str, str]:
    """
    Run `python3 {SCRIPTS[script_key]} {args}`.
    Returns (returncode, stdout, stderr).
    In dry-run mode prints the command and returns (0, "", "").
    """
    script_path = SCRIPTS[script_key]
    cmd = [sys.executable, str(script_path)] + [str(a) for a in args]
    cmd_str = " ".join(cmd)

    if dry_run:
        print(f"[DRY-RUN] Would run: {cmd_str}")
        return 0, "", ""

    t = timeout if timeout is not None else SCRIPT_TIMEOUTS.get(script_key, DEFAULT_SCRIPT_TIMEOUT)
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=t)
        return result.returncode, result.stdout, result.stderr
    except subprocess.TimeoutExpired:
        return 1, "", f"[TIMEOUT] {script_key} exceeded {t}s"


# ---------------------------------------------------------------------------
# Phase 0 — Setup
# ---------------------------------------------------------------------------

def phase_setup(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    month: str,
) -> None:
    logger.info("=" * 64)
    logger.info("NUS Energy Pipeline — month: %s", month)
    logger.info("Run started: %s", state["run_started"])
    logger.info("=" * 64)

    for key in ("outputs_dir", "gt_dir", "calibrated_epw_dir", "reports_dir"):
        PATHS[key].mkdir(parents=True, exist_ok=True)
        logger.info("Directory ready: %s", PATHS[key])

    save_state(state, state_path)
    logger.info("Pipeline state: %s", state_path)


# ---------------------------------------------------------------------------
# Phase 1a — Weather (Nimbus)
# ---------------------------------------------------------------------------

def phase_weather(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    month: str,
    *,
    dry_run: bool = False,
) -> None:
    logger.info("--- Phase 1a: Weather (Nimbus) ---")
    calibrated_epw = PATHS["calibrated_epw_dir"] / f"{month}_site_calibrated.epw"

    rc, out, err = run_script("fetch_weather", ["--month", month, "--source", "auto"],
                              dry_run=dry_run)
    if out:
        logger.info("[fetch_weather] %s", out.strip())
    if err:
        logger.warning("[fetch_weather stderr] %s", err.strip())

    rc2, out2, err2 = run_script("validate_weather", ["--month", month], dry_run=dry_run)
    if out2:
        logger.info("[validate_weather] %s", out2.strip())
    if err2:
        logger.warning("[validate_weather stderr] %s", err2.strip())

    rc3, out3, err3 = run_script("build_epw", [
        "--month", month,
        "--base-epw", str(PATHS["base_epw"]),
        "--out", str(calibrated_epw),
    ], dry_run=dry_run)
    if out3:
        logger.info("[build_epw] %s", out3.strip())
    if err3:
        logger.warning("[build_epw stderr] %s", err3.strip())

    if dry_run or calibrated_epw.exists():
        state["epw_source"] = f"calibrated:{month}"
        logger.info("EPW source: calibrated (%s)", calibrated_epw)
    else:
        state["epw_source"] = "tmy_fallback"
        logger.warning("Calibrated EPW not produced — falling back to base TMY: %s",
                       PATHS["base_epw"])

    mark_phase(state, "weather")
    save_state(state, state_path)
    logger.info("--- Phase 1a complete ---")


# ---------------------------------------------------------------------------
# Phase 1b — Ground truth prep (Radar)
# ---------------------------------------------------------------------------

def phase_ground_truth(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    *,
    dry_run: bool = False,
) -> None:
    logger.info("--- Phase 1b: Ground Truth Prep ---")

    sentinel = PATHS["gt_dir"] / f"{GT_BUILDINGS[0]}_ground_truth.csv"
    if sentinel.exists() and not dry_run:
        logger.info("Ground truth already prepared (sentinel: %s) — skipping.", sentinel)
        mark_phase(state, "ground_truth")
        save_state(state, state_path)
        return

    rc, out, err = run_script("prepare_gt", [], dry_run=dry_run)
    if out:
        logger.info("[prepare_gt] %s", out.strip())
    if err and rc != 0:
        logger.warning("[prepare_gt stderr] %s", err.strip())
        record_error(state, f"prepare_gt rc={rc}")

    mark_phase(state, "ground_truth")
    save_state(state, state_path)
    logger.info("--- Phase 1b complete ---")


# ---------------------------------------------------------------------------
# Phase 2 — Simulation (Forge)
# ---------------------------------------------------------------------------

def _resolve_epw_path(month: str) -> Path:
    calibrated = PATHS["calibrated_epw_dir"] / f"{month}_site_calibrated.epw"
    return calibrated if calibrated.exists() else PATHS["base_epw"]


def _find_idf(building: str) -> Path | None:
    matches = list(PATHS["idf_dir"].rglob(f"{building}.idf"))
    return matches[0] if matches else None


def _run_simulation_for_building(
    logger: logging.Logger,
    state: dict[str, Any],
    building: str,
    month: str,
    epw_path: Path,
    *,
    dry_run: bool = False,
) -> int:
    """Run simulate.py for a single building. Returns exit code."""
    idf_path = _find_idf(building)
    if idf_path is None:
        msg = f"IDF not found for {building} in {PATHS['idf_dir']}"
        logger.warning(msg)
        record_error(state, msg)
        state["buildings"].setdefault(building, {})["status"] = "error"
        return 1

    _mark_building_phase_start(state, building, "simulation")
    rc, out, err = run_script("simulate", [
        "--idf",    str(idf_path),
        "--month",  month,
        "--gt-dir", str(PATHS["gt_dir"]),
        "--output", str(PATHS["outputs_dir"]),
        "--epw",    str(epw_path),
    ], dry_run=dry_run)

    if out:
        logger.info("[simulate:%s] %s", building, out.strip()[:500])
    if rc != 0:
        if err:
            logger.warning("[simulate:%s stderr] %s", building, err.strip()[:300])
        state["buildings"].setdefault(building, {})["status"] = "error"
        record_error(state, f"simulate:{building} rc={rc}")
        _mark_building_phase_end(state, building, "simulation", status="error")
    else:
        _mark_building_phase_end(state, building, "simulation", status="done")

    return rc


def phase_simulation(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    month: str,
    buildings: list[str],
    workers: int,
    *,
    dry_run: bool = False,
) -> None:
    logger.info("--- Phase 2: Simulation (Forge) ---")
    epw_path = _resolve_epw_path(month)
    logger.info("EPW for simulation: %s", epw_path)

    # Always simulate GT buildings only — one at a time via --idf
    for building in buildings:
        _run_simulation_for_building(
            logger, state, building, month, epw_path, dry_run=dry_run)

    mark_phase(state, "simulation")
    save_state(state, state_path)
    logger.info("--- Phase 2 complete ---")


# ---------------------------------------------------------------------------
# Phase 3 — Detection gate (Radar)
# ---------------------------------------------------------------------------

def phase_detection(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    buildings: list[str],
    *,
    dry_run: bool = False,
) -> list[str]:
    """
    Prefer per-building metrics derived directly from parsed monthly results +
    prepared ground truth. Fall back to simulation_summary.csv only when those
    direct artifacts are unavailable.
    """
    logger.info("--- Phase 3: Detection Gate ---")
    summary_path = PATHS["outputs_dir"] / "simulation_summary.csv"

    if dry_run:
        logger.info("[DRY-RUN] Would classify buildings using parsed monthly outputs and GT CSVs.")
        mark_phase(state, "detection")
        save_state(state, state_path)
        return []

    try:
        import math
        import pandas as pd
        import numpy as np
    except ImportError:
        logger.error("pandas/numpy are required for detection gate — pip install pandas numpy")
        record_error(state, "pandas/numpy not available — detection skipped")
        mark_phase(state, "detection")
        save_state(state, state_path)
        return []

    def _finite_number(v) -> bool:
        try:
            return v is not None and math.isfinite(float(v))
        except (TypeError, ValueError):
            return False

    def _metrics_from_direct_artifacts(building: str) -> dict[str, float] | None:
        monthly_path = PATHS["outputs_dir"] / building / "parsed" / f"{building}_monthly.csv"
        gt_path = PATHS["gt_dir"] / f"{building}_ground_truth.csv"
        if not monthly_path.exists() or not gt_path.exists():
            return None

        try:
            sim_df = pd.read_csv(monthly_path)
            gt_df = pd.read_csv(gt_path)
        except Exception as exc:
            logger.warning("[%s] Failed reading direct detection artifacts: %s", building, exc)
            return None

        if "month_name" in sim_df.columns:
            sim_df = sim_df[sim_df["month_name"].astype(str).str.upper() != "ANNUAL"].copy()
        if "month" not in sim_df.columns:
            return None

        sim_col = None
        for candidate in ["electricity_facility_kwh", "cooling_elec_adj_kwh", "cooling_elec_kwh"]:
            if candidate in sim_df.columns:
                sim_col = candidate
                break
        if sim_col is None:
            return None

        try:
            sim_df["month"] = pd.to_numeric(sim_df["month"], errors="coerce")
            sim_df[sim_col] = pd.to_numeric(sim_df[sim_col], errors="coerce")
            sim_df = sim_df.dropna(subset=["month", sim_col])
            sim_df["month"] = sim_df["month"].astype(int)
            sim_monthly = sim_df.groupby("month")[sim_col].sum().rename("simulated_kwh")

            gt_df["month"] = pd.to_datetime(gt_df["month"], errors="coerce")
            gt_df["measured_kwh"] = pd.to_numeric(gt_df["measured_kwh"], errors="coerce")
            gt_df = gt_df.dropna(subset=["month", "measured_kwh"])
            gt_df["month_num"] = gt_df["month"].dt.month
            gt_monthly = gt_df.groupby("month_num")["measured_kwh"].mean().rename("measured_kwh")

            comparison = pd.concat([sim_monthly, gt_monthly], axis=1, join="inner").dropna()
            if comparison.empty:
                return None

            rmse = np.sqrt(((comparison["simulated_kwh"] - comparison["measured_kwh"]) ** 2).mean())
            mean_measured = comparison["measured_kwh"].mean()
            measured_sum = comparison["measured_kwh"].sum()
            if mean_measured == 0 or measured_sum == 0:
                return None

            cvrmse = (rmse / mean_measured) * 100.0
            nmbe = ((comparison["simulated_kwh"] - comparison["measured_kwh"]).sum() / measured_sum) * 100.0
            if not (_finite_number(cvrmse) and _finite_number(nmbe)):
                return None
            return {"cvrmse": float(cvrmse), "nmbe": float(nmbe)}
        except Exception as exc:
            logger.warning("[%s] Direct metric calculation failed: %s", building, exc)
            return None

    summary_metrics: dict[str, dict[str, float]] = {}
    if summary_path.exists():
        try:
            df = pd.read_csv(summary_path)
            df.columns = [c.strip().lower() for c in df.columns]
            col_building = next((c for c in df.columns if "building" in c), None)
            col_cvrmse = next((c for c in df.columns if "cvrmse" in c), None)
            col_nmbe = next((c for c in df.columns if "nmbe" in c), None)
            if col_building and col_cvrmse and col_nmbe:
                for _, row in df.iterrows():
                    bname = str(row[col_building]).strip()
                    cvrmse = pd.to_numeric(row[col_cvrmse], errors="coerce")
                    nmbe = pd.to_numeric(row[col_nmbe], errors="coerce")
                    if _finite_number(cvrmse) and _finite_number(nmbe):
                        summary_metrics[bname] = {
                            "cvrmse": float(cvrmse),
                            "nmbe": float(nmbe),
                        }
            else:
                logger.warning("simulation_summary.csv missing expected columns. Found: %s", list(df.columns))
        except Exception as exc:
            logger.warning("Failed to read simulation_summary.csv: %s", exc)
            record_error(state, f"detection summary read warning: {exc}")

    metrics: dict[str, dict[str, float]] = {}
    direct_count = 0
    summary_count = 0
    for building in buildings:
        direct = _metrics_from_direct_artifacts(building)
        if direct is not None:
            metrics[building] = direct
            direct_count += 1
        elif building in summary_metrics:
            metrics[building] = summary_metrics[building]
            summary_count += 1

    logger.info("Detection metric sources: direct=%d, summary_fallback=%d", direct_count, summary_count)

    def classify(cvrmse: float, nmbe: float) -> str:
        abs_nmbe = abs(nmbe)
        if cvrmse <= 15.0 and abs_nmbe <= 5.0:
            return "calibrated"
        if cvrmse > 30.0 or abs_nmbe > 10.0:
            return "critical"
        return "warning"

    header = f"{'Building':<12}  {'Status':<12}  {'CV(RMSE)%':>10}  {'NMBE%':>8}"
    logger.info(header)
    logger.info("-" * len(header))

    needs_diagnosis: list[str] = []
    for building in buildings:
        if building in metrics:
            m = metrics[building]
            cvrmse, nmbe = m["cvrmse"], m["nmbe"]
            status = classify(cvrmse, nmbe)
            bstate = state["buildings"].setdefault(building, {})
            _mark_building_phase_start(state, building, "detection")
            bstate.update({
                "status": status,
                "cvrmse": round(cvrmse, 2),
                "nmbe": round(nmbe, 2),
                "iteration": bstate.get("iteration", 0),
            })
            _mark_building_phase_end(state, building, "detection", status=status)
            logger.info("%-12s  %-12s  %10.2f  %8.2f", building, status, cvrmse, nmbe)
            if status in ("warning", "critical"):
                needs_diagnosis.append(building)
        else:
            existing = state["buildings"].get(building, {}).get("status", "no_data")
            if existing != "error":
                state["buildings"].setdefault(building, {})["status"] = "no_data"
            _mark_building_phase_start(state, building, "detection")
            _mark_building_phase_end(state, building, "detection", status=state["buildings"][building]["status"])
            logger.info("%-12s  %-12s  %10s  %8s", building, state["buildings"][building]["status"], "—", "—")

    state["needs_diagnosis"] = needs_diagnosis

    if needs_diagnosis:
        logger.info("Buildings needing diagnosis: %s", ", ".join(needs_diagnosis))
    else:
        logger.info("All buildings within ASHRAE thresholds — no diagnosis required.")

    mark_phase(state, "detection")
    save_state(state, state_path)
    logger.info("--- Phase 3 complete ---")
    return needs_diagnosis


# ---------------------------------------------------------------------------
# Phase 4 — Diagnosis loop (Lens → Slack → Chisel → Forge)
# ---------------------------------------------------------------------------

def _lens_pause_banner(building: str, month: str, lens_input_path: Path,
                       lens_output_path: Path) -> None:
    """Print the PAUSE banner for a building awaiting Lens."""
    w = 56
    lines = [
        "╔" + "═" * w + "╗",
        "║" + "  ⏸  PIPELINE PAUSED — LENS DIAGNOSIS REQUIRED".center(w) + "║",
        "║" + f"  Building: {building}".ljust(w) + "║",
        "║" + f"  Input: {str(lens_input_path)}"[:w].ljust(w) + "║",
        "║" + " " * w + "║",
        "║" + "  Run Lens (DiagnosisAgent) on this building,".ljust(w) + "║",
        "║" + f"  then write output to:".ljust(w) + "║",
        "║" + f"    {str(lens_output_path)}"[:w - 4].ljust(w) + "║",
        "║" + " " * w + "║",
        "║" + "  Resume with:".ljust(w) + "║",
        "║" + f"    python3 run_pipeline.py --month {month}".ljust(w) + "║",
        "║" + "    --resume-from diagnosis".ljust(w) + "║",
        "╚" + "═" * w + "╝",
    ]
    print("\n" + "\n".join(lines) + "\n")


def _write_lens_input(building: str, month: str, state: dict[str, Any]) -> Path:
    """Write lens_input.json for a building and return its path."""
    out_dir = PATHS["outputs_dir"] / building
    out_dir.mkdir(parents=True, exist_ok=True)
    lens_input_path = out_dir / "lens_input.json"

    bstate = state["buildings"].get(building, {})
    payload = {
        "building":        building,
        "cvrmse":          bstate.get("cvrmse"),
        "nmbe":            bstate.get("nmbe"),
        "monthly_csv":     str(PATHS["outputs_dir"] / building / "parsed" / f"{building}_monthly.csv"),
        "gt_csv":          str(PATHS["gt_dir"] / f"{building}_ground_truth.csv"),
        "prepared_idf":    str(PATHS["outputs_dir"] / building / "prepared" / f"{building}_prepared.idf"),
        "calibration_log": str(PATHS["calibration_log"]),
        "iteration_count": bstate.get("iteration", 0) + 1,
    }

    with lens_input_path.open("w") as f:
        json.dump(payload, f, indent=2)

    return lens_input_path


def _wait_for_approval(building: str, month: str) -> bool | None:
    """
    Check for slack_approval.json.
    Returns True (approved), False (rejected), or None (not yet written).
    """
    approval_path = PATHS["outputs_dir"] / building / "slack_approval.json"
    if not approval_path.exists():
        return None
    try:
        with approval_path.open() as f:
            data = json.load(f)
        return bool(data.get("approved", False))
    except (json.JSONDecodeError, OSError):
        return None


# Exit codes from calibration_loop.py
_CALIB_EXIT_CONVERGED    = 0
_CALIB_EXIT_ERROR        = 1
_CALIB_EXIT_AT_BOUNDS    = 2
_CALIB_EXIT_REJECTED     = 3
_CALIB_EXIT_NEED_LENS    = 4
_CALIB_EXIT_WAIT_TIMEOUT = 5

_CALIB_LOOP_SCRIPT = Path(__file__).parent / "calibration_loop.py"


def _run_calibration_loop(
    logger: logging.Logger,
    building: str,
    month: str,
    max_wait: int = 14400,
    *,
    dry_run: bool = False,
) -> int:
    """Invoke calibration_loop.py for one building and return its exit code."""
    if dry_run:
        logger.info("[DRY-RUN] Would run calibration loop for %s", building)
        return _CALIB_EXIT_CONVERGED

    cmd = [
        sys.executable, str(_CALIB_LOOP_SCRIPT),
        "--building", building,
        "--month",    month,
        "--max-wait", str(max_wait),
    ]
    env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}
    proc = subprocess.run(cmd, env=env)
    return proc.returncode


def phase_diagnosis(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    month: str,
    needs_diagnosis: list[str],
    *,
    dry_run: bool = False,
    approval_timeout: int = 14400,
) -> None:
    """
    Phase 4 — Diagnosis Loop

    For each building that failed the ASHRAE gate, runs calibration_loop.py
    which drives the full Lens → Chisel → Slack approval → Forge → metrics cycle
    until CVRMSE ≤ 15% AND |NMBE| ≤ 5%, or a terminal condition is reached.

    Outcomes per building:
      ✅ converged    — ASHRAE thresholds met; building marked calibrated
      ⏸ need_lens    — lens_output.json missing; pipeline records building for
                        manual Lens invocation and continues with others
      ⛔ at_bounds    — all params at bounds without convergence; flagged for
                        human review via pipeline_state + log
      ❌ rejected     — engineer rejected via Slack; falls through to report
      ⏱ timeout      — no Slack response in approval_timeout seconds;
                        escalated, pipeline continues with remaining buildings
      ❌ error        — script error; recorded in state errors
    """
    logger.info("--- Phase 4: Diagnosis Loop ---")

    state["needs_diagnosis"] = needs_diagnosis or []
    needs_lens_later: list[str] = []

    if not needs_diagnosis:
        logger.info("No buildings require diagnosis — skipping.")
        mark_phase(state, "diagnosis")
        save_state(state, state_path)
        logger.info("--- Phase 4 complete ---")
        return

    logger.info("Buildings entering calibration loop: %s", ", ".join(needs_diagnosis))

    for building in needs_diagnosis:
        logger.info("──── Calibration loop: %s ────", building)
        bstate = state["buildings"].setdefault(building, {})

        rc = _run_calibration_loop(
            logger, building, month, max_wait=approval_timeout, dry_run=dry_run
        )

        if rc == _CALIB_EXIT_CONVERGED:
            bstate["status"]      = "calibrated"
            bstate["calib_result"] = "converged"
            logger.info("[%s] ✅ Converged — ASHRAE thresholds met.", building)

        elif rc == _CALIB_EXIT_NEED_LENS:
            bstate["calib_result"] = "awaiting_lens"
            needs_lens_later.append(building)
            logger.info(
                "[%s] ⏸ Lens diagnosis required — building queued for manual Lens run.",
                building,
            )

        elif rc == _CALIB_EXIT_AT_BOUNDS:
            bstate["status"]      = "engineer_review"
            bstate["calib_result"] = "at_bounds"
            logger.warning(
                "[%s] ⛔ All parameters at bounds — flagged for human review.", building
            )
            record_error(state, f"{building}: calibration at bounds — human review required")

        elif rc == _CALIB_EXIT_REJECTED:
            bstate["calib_result"] = "rejected"
            logger.info("[%s] ❌ Recalibration rejected by engineer.", building)

        elif rc == _CALIB_EXIT_WAIT_TIMEOUT:
            bstate["calib_result"] = "approval_timeout"
            logger.warning(
                "[%s] ⏱ Approval timed out — escalated to #private. Continuing pipeline.",
                building,
            )
            record_error(state, f"{building}: Slack approval timed out")

        else:  # _CALIB_EXIT_ERROR or unknown
            bstate["calib_result"] = "error"
            logger.error("[%s] ❌ calibration_loop.py exited with error (rc=%d).", building, rc)
            record_error(state, f"{building}: calibration loop error rc={rc}")

        save_state(state, state_path)

    if needs_lens_later:
        state["awaiting_lens"] = needs_lens_later
        logger.info(
            "Buildings awaiting Lens diagnosis: %s", ", ".join(needs_lens_later)
        )
        logger.info(
            "Run DiagnosisAgent (Lens) for each and write outputs/{BUILDING}/lens_output.json,"
            " then re-run: python3 run_pipeline.py --month %s --resume-from diagnosis",
            month,
        )

    mark_phase(state, "diagnosis")
    save_state(state, state_path)
    logger.info("--- Phase 4 complete ---")


# ---------------------------------------------------------------------------
# Phase 5 — Report (Ledger)
# ---------------------------------------------------------------------------

def phase_report(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    month: str,
    *,
    dry_run: bool = False,
    buildings_filter: list[str] | None = None,
) -> None:
    logger.info("--- Phase 5: Report (Ledger) ---")

    PATHS["reports_dir"].mkdir(parents=True, exist_ok=True)

    # Write a summary JSON for Ledger to consume
    summary_path = PATHS["outputs_dir"] / "pipeline_summary.json"
    if not dry_run:
        with summary_path.open("w") as f:
            json.dump({
                "month":    month,
                "buildings": state.get("buildings", {}),
                "epw_source": state.get("epw_source"),
                "errors":   state.get("errors", []),
            }, f, indent=2)
        logger.info("Written pipeline summary for Ledger: %s", summary_path)

    # report.py accepts: --building, --campus, --open
    # For a pipeline run we use --campus to get the full summary report.
    all_state_buildings = list(state.get("buildings", {}).keys())
    report_targets = (
        [b for b in buildings_filter if b in all_state_buildings]
        if buildings_filter else all_state_buildings
    )
    for building in report_targets:
        _mark_building_phase_start(state, building, "report")
        rc_b, out_b, err_b = run_script("report", [
            "--building", building,
        ], dry_run=dry_run)
        if out_b:
            logger.info("[report:%s] %s", building, out_b.strip())
        if rc_b != 0:
            if err_b:
                logger.warning("[report:%s stderr] %s", building, err_b.strip()[:200])
            record_error(state, f"report:{building} rc={rc_b}")
            _mark_building_phase_end(state, building, "report", status="error")
        else:
            _mark_building_phase_end(state, building, "report", status="done")

    rc, out, err = run_script("report", [
        "--campus",
    ], dry_run=dry_run)

    if out:
        logger.info("[report] %s", out.strip())
    if rc != 0:
        if err:
            logger.warning("[report stderr] %s", err.strip())
        record_error(state, f"report rc={rc}")
    else:
        logger.info("Report written to: %s", PATHS["reports_dir"])

    mark_phase(state, "report")
    save_state(state, state_path)
    logger.info("--- Phase 5 complete ---")


# ---------------------------------------------------------------------------
# Phase 6 — Notification (Signal)
# ---------------------------------------------------------------------------
# Phase 6 is now a no-op stub — Signal only posts after Phase 7 (intervention results).
# Calibration approval requests are posted inline during Phase 4 (diagnosis loop).
# ---------------------------------------------------------------------------

def phase_notify(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    month: str,
    *,
    dry_run: bool = False,
) -> None:
    logger.info("--- Phase 6: Notification (Signal) — deferred to post-Phase-7 ---")
    # Signal posts final intervention results after Compass completes (Phase 7).
    # Nothing to do here — mark complete and continue.
    mark_phase(state, "notify")
    save_state(state, state_path)
    logger.info("--- Phase 6 complete ---")


# ---------------------------------------------------------------------------
# Phase 7 — Intervention (Compass + Oracle)
# ---------------------------------------------------------------------------

def phase_intervene(
    logger: logging.Logger,
    state: dict[str, Any],
    state_path: Path,
    month: str,
    *,
    dry_run: bool = False,
    buildings_filter: list[str] | None = None,
) -> None:
    logger.info("--- Phase 7: Intervention (Compass + Oracle) ---")

    # Determine approved buildings from intervention_approved.json (if exists)
    approval_path = PATHS["outputs_dir"] / "intervention_approved.json"
    approved_buildings: list[str] = []

    if approval_path.exists() and not dry_run:
        try:
            with approval_path.open() as f:
                data = json.load(f)
            raw = data.get("buildings", [])
            if raw == ["all"] or raw == "all":
                approved_buildings = list(GT_BUILDINGS)
            else:
                approved_buildings = [b for b in raw if b in GT_BUILDINGS]
        except (json.JSONDecodeError, OSError):
            approved_buildings = list(GT_BUILDINGS)
            logger.warning("Could not parse intervention_approved.json — defaulting to all GT buildings.")
    else:
        # Auto-approve all GT buildings if file absent (or dry-run)
        approved_buildings = list(GT_BUILDINGS)
        if dry_run:
            logger.info("[DRY-RUN] Would run Compass for all GT buildings.")

    # Apply --buildings filter to Compass + Signal (if specified)
    if buildings_filter:
        approved_buildings = [b for b in approved_buildings if b in buildings_filter]
        logger.info("Filtered to %d building(s) via --buildings: %s",
                    len(approved_buildings), ", ".join(approved_buildings))

    logger.info("Running Compass for %d building(s): %s",
                len(approved_buildings), ", ".join(approved_buildings))

    failed_compass: list[str] = []
    for building in approved_buildings:
        _mark_building_phase_start(state, building, "intervene")
        rc, out, err = run_script("carbon_scenarios", [
            "--building",   building,
            "--outputs",    str(PATHS["outputs_dir"]),
            "--idfs",       str(PATHS["idf_dir"]),
            "--month",      month,
            "--no-simulate",  # use estimates; full EnergyPlus counterfactuals would timeout
        ], dry_run=dry_run)

        if out:
            logger.info("[carbon_scenarios:%s] %s", building, out.strip()[:400])
        if rc != 0:
            if err:
                logger.warning("[carbon_scenarios:%s stderr] %s", building, err.strip()[:200])
            record_error(state, f"carbon_scenarios:{building} rc={rc}")
            failed_compass.append(building)
            _mark_building_phase_end(state, building, "intervene", status="error")
        else:
            logger.info("Compass scenarios complete for %s.", building)
            _mark_building_phase_end(state, building, "intervene", status="done")

    if failed_compass:
        logger.warning("Compass failed for: %s", ", ".join(failed_compass))

    # Enable Oracle Q&A mode
    logger.info("Enabling Oracle Q&A mode …")
    # query.py accepts: --building, --metric, --ranking, --summary, --bca-gap, --campus-carbon
    # Use --campus-carbon for a stakeholder-facing campus overview.
    rc_q, out_q, err_q = run_script("query", [
        "--campus-carbon",
    ], dry_run=dry_run)

    if out_q:
        logger.info("[query] %s", out_q.strip())
    if rc_q != 0:
        if err_q:
            logger.warning("[query stderr] %s", err_q.strip()[:200])
        record_error(state, f"oracle query rc={rc_q}")

    # Phase 6 (deferred): Signal posts final intervention results now that Compass is done.
    logger.info("--- Phase 6 (deferred): Signal — posting final intervention results ---")
    signal_buildings = [b for b in approved_buildings if b not in failed_compass]
    if not signal_buildings:
        logger.warning("No successful Compass buildings — skipping Signal notification.")
    else:
        for bld in signal_buildings:
            bstate     = state.get("buildings", {}).get(bld, {})
            calibrated = bstate.get("status") == "calibrated"
            rc_sig, out_sig, err_sig = run_script("signal_notify", [
                "--building", bld,
                "--channel",  "#openclaw-alerts",
                *(["--calibrated"] if calibrated else []),
            ], dry_run=dry_run)
            if out_sig:
                logger.info("[signal:%s] %s", bld, out_sig.strip())
            if rc_sig != 0:
                if err_sig:
                    logger.warning("[signal:%s stderr] %s", bld, err_sig.strip()[:200])
                record_error(state, f"signal:{bld} rc={rc_sig}")
                logger.warning("Signal failed for %s — logged, continuing.", bld)
            else:
                logger.info("Signal: final results posted for %s.", bld)

    mark_phase(state, "intervene")
    save_state(state, state_path)
    logger.info("--- Phase 7 complete ---")


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------

RESUME_PHASES = ["weather", "simulation", "detection", "diagnosis",
                 "report", "notify", "intervene"]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="NUS Energy Management Pipeline Runner (full 7-phase)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "--month", required=True, metavar="YYYY-MM",
        help="Target simulation month (e.g. 2024-08)",
    )
    parser.add_argument(
        "--buildings", nargs="+", default=None, metavar="BUILDING",
        help="Specific buildings to process (default: all 23 GT buildings)",
    )
    parser.add_argument(
        "--skip-weather", action="store_true",
        help="Skip Phase 1a; use existing calibrated EPW or TMY fallback",
    )
    parser.add_argument(
        "--skip-simulation", action="store_true",
        help="Skip Phase 2; use existing parsed CSVs for detection",
    )
    parser.add_argument(
        "--workers", type=int, default=4, metavar="N",
        help="Parallel EnergyPlus workers for Forge batch simulation (default: 4)",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="Print what would run without executing anything",
    )
    parser.add_argument(
        "--resume-from",
        choices=RESUME_PHASES,
        default=None,
        metavar="PHASE",
        help=(
            "Resume from a specific phase, skipping earlier completed phases. "
            f"Choices: {', '.join(RESUME_PHASES)}"
        ),
    )
    parser.add_argument(
        "--approval-timeout", type=int, default=14400, metavar="SECONDS",
        help="Seconds to wait for Slack approval per calibration iteration (default: 14400 = 4h)",
    )
    return parser.parse_args()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    args = parse_args()

    buildings: list[str] = args.buildings if args.buildings else discover_all_buildings()
    specific_buildings: bool = args.buildings is not None

    PATHS["outputs_dir"].mkdir(parents=True, exist_ok=True)
    logger = setup_logging(PATHS["outputs_dir"], args.month)

    if args.dry_run:
        logger.info("*** DRY-RUN MODE — no scripts will be executed ***")
    if args.resume_from:
        logger.info("*** RESUME MODE — resuming from phase: %s ***", args.resume_from)

    # ── Phase 0: Setup ────────────────────────────────────────────────────
    state_path = PATHS["pipeline_state"]
    state = load_state(state_path, args.month)
    phase_setup(logger, state, state_path, args.month)

    # Determine which phases to skip based on --resume-from
    resume_idx = PHASE_ORDER.index(args.resume_from) if args.resume_from else 0

    def should_skip(phase: str) -> bool:
        """Return True if phase comes before the resume point."""
        try:
            return PHASE_ORDER.index(phase) < resume_idx
        except ValueError:
            return False

    # ── Phase 1a: Weather ────────────────────────────────────────────────
    if should_skip("weather") or args.skip_weather or phase_complete(state, "weather"):
        calibrated_epw = PATHS["calibrated_epw_dir"] / f"{args.month}_site_calibrated.epw"
        if calibrated_epw.exists():
            state["epw_source"] = f"calibrated:{args.month}"
            logger.info("Skipping Phase 1a — using existing calibrated EPW: %s", calibrated_epw)
        else:
            state["epw_source"] = "tmy_fallback"
            logger.info("Skipping Phase 1a — calibrated EPW absent, will use TMY: %s",
                        PATHS["base_epw"])
        save_state(state, state_path)
    else:
        phase_weather(logger, state, state_path, args.month, dry_run=args.dry_run)

    # ── Phase 1b: Ground truth ───────────────────────────────────────────
    if not should_skip("weather") and not phase_complete(state, "ground_truth"):
        phase_ground_truth(logger, state, state_path, dry_run=args.dry_run)
    elif phase_complete(state, "ground_truth"):
        logger.info("Phase 1b already complete — skipping.")

    # ── Phase 2: Simulation ──────────────────────────────────────────────
    if should_skip("simulation") or args.skip_simulation or phase_complete(state, "simulation"):
        logger.info("Skipping Phase 2 (simulation).")
        mark_phase(state, "simulation")
        save_state(state, state_path)
    else:
        phase_simulation(
            logger, state, state_path,
            month=args.month,
            buildings=buildings,
            workers=args.workers,
            dry_run=args.dry_run,
        )

    # ── Phase 3: Detection gate ──────────────────────────────────────────
    if should_skip("detection") or phase_complete(state, "detection"):
        logger.info("Skipping Phase 3 (detection) — using state from previous run.")
        needs_diagnosis = state.get("needs_diagnosis", [])
    else:
        needs_diagnosis = phase_detection(
            logger, state, state_path, buildings, dry_run=args.dry_run)

    # ── Phase 4: Diagnosis loop ──────────────────────────────────────────
    if should_skip("diagnosis") or phase_complete(state, "diagnosis"):
        logger.info("Skipping Phase 4 (diagnosis) — already complete.")
    else:
        phase_diagnosis(
            logger, state, state_path, args.month, needs_diagnosis,
            dry_run=args.dry_run,
            approval_timeout=args.approval_timeout,
        )

    # ── Phase 5: Report ──────────────────────────────────────────────────
    if should_skip("report") or phase_complete(state, "report"):
        logger.info("Skipping Phase 5 (report) — already complete.")
    else:
        phase_report(logger, state, state_path, args.month, dry_run=args.dry_run,
                     buildings_filter=buildings if specific_buildings else None)

    # ── Phase 6: Notification ────────────────────────────────────────────
    if should_skip("notify") or phase_complete(state, "notify"):
        logger.info("Skipping Phase 6 (notify) — already complete.")
    else:
        phase_notify(logger, state, state_path, args.month, dry_run=args.dry_run)

    # ── Phase 7: Intervention ────────────────────────────────────────────
    if should_skip("intervene") or phase_complete(state, "intervene"):
        logger.info("Skipping Phase 7 (intervene) — already complete.")
    else:
        phase_intervene(logger, state, state_path, args.month, dry_run=args.dry_run,
                        buildings_filter=buildings if specific_buildings else None)

    # ── Final summary ─────────────────────────────────────────────────────
    logger.info("=" * 64)
    logger.info("Pipeline complete. Phases done: %s", state["phases_complete"])
    if state.get("phases_pending"):
        logger.info("Phases still pending: %s", state["phases_pending"])
    if state.get("errors"):
        logger.warning("Errors encountered (%d):", len(state["errors"]))
        for err in state["errors"]:
            logger.warning("  • %s", err)
    else:
        logger.info("No errors.")
    logger.info("State: %s", PATHS["pipeline_state"])
    logger.info("=" * 64)


if __name__ == "__main__":
    main()
