#!/usr/bin/env python3
"""
calibration_loop.py — Phase 4 calibration loop for NUS energy pipeline
=======================================================================
Drives the Lens → Chisel → Slack approval → Forge → Radar cycle for a single
building until ASHRAE Guideline 14 thresholds are met or the loop terminates.

Termination conditions:
  ✅ CVRMSE ≤ 15% AND |NMBE| ≤ 5%  → converged, exit 0
  ⛔ All calibration parameters at bounds without convergence → exit 2 (flag)
  ❌ Slack approval rejected         → exit 3 (rejected)
  ⏸ lens_output.json missing        → exit 4 (waiting for Lens)
  ⏱ Approval timeout (4h default)   → exit 5 (escalated, waiting)

Usage (called by run_pipeline.py per building):
  python3 calibration_loop.py --building FOE13 --month 2024-08 [--max-wait 14400]

Environment:
  NUS_PROJECT_DIR  — root of nus-energy project (default: /Users/ye/nus-energy)
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import subprocess
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
SGT = timezone(timedelta(hours=8))

PATHS = {
    "idf_dir":          NUS_PROJECT_DIR / "idfs",
    "outputs_dir":      NUS_PROJECT_DIR / "outputs",
    "gt_dir":           NUS_PROJECT_DIR / "ground_truth" / "parsed",
    "calibration_log":  NUS_PROJECT_DIR / "calibration_log.md",
    "parameter_bounds": NUS_PROJECT_DIR / "parameter_bounds.json",
    "base_epw":         NUS_PROJECT_DIR / "weather" / "SGP_Singapore.486980_IWEC.epw",
    "calibrated_epw_dir": NUS_PROJECT_DIR / "weather" / "calibrated",
}

SCRIPTS = {
    "patch_idf":   Path("/Users/ye/.openclaw/workspace-recalibrationagent/skills/nus-calibrate/scripts/patch_idf.py"),
    "simulate":    Path("/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/scripts/simulate.py"),
    "prepare_gt":  Path("/Users/ye/.openclaw/workspace-anomalyagent/skills/nus-groundtruth/scripts/prepare_ground_truth.py"),
}

# ASHRAE Guideline 14 thresholds
CVRMSE_THRESHOLD = 15.0
NMBE_THRESHOLD   = 5.0

# Approval poll interval (seconds)
APPROVAL_POLL_INTERVAL = 30

# Exit codes
EXIT_CONVERGED    = 0
EXIT_ERROR        = 1
EXIT_AT_BOUNDS    = 2
EXIT_REJECTED     = 3
EXIT_NEED_LENS    = 4
EXIT_WAIT_TIMEOUT = 5

# Calibration parameter order (priority)
CALIB_PARAMS = ["Infiltration_ACH", "Equipment_W_per_m2", "Lighting_W_per_m2"]


# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

def setup_logger(building: str) -> logging.Logger:
    logger = logging.getLogger(f"calib_loop.{building}")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        h = logging.StreamHandler(sys.stdout)
        h.setFormatter(logging.Formatter(
            f"%(asctime)s  %(levelname)-8s  [{building}] %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        ))
        logger.addHandler(h)
    return logger


# ---------------------------------------------------------------------------
# Parameter bounds helpers
# ---------------------------------------------------------------------------

def load_bounds() -> dict[str, dict]:
    path = PATHS["parameter_bounds"]
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def get_param_bounds(bounds: dict, param: str) -> tuple[float, float]:
    """Return (min, max) for a calibration parameter."""
    entry = bounds.get(param, {})
    return float(entry.get("min", -1e9)), float(entry.get("max", 1e9))


def param_at_bound(value: float, param: str, bounds: dict) -> str | None:
    """Return 'min', 'max', or None."""
    lo, hi = get_param_bounds(bounds, param)
    if math.isclose(value, lo, rel_tol=1e-4, abs_tol=1e-6):
        return "min"
    if math.isclose(value, hi, rel_tol=1e-4, abs_tol=1e-6):
        return "max"
    return None


# ---------------------------------------------------------------------------
# Current IDF param reader (reads from lens_output proposed values)
# ---------------------------------------------------------------------------

def read_lens_output(building: str) -> dict | None:
    path = PATHS["outputs_dir"] / building / "lens_output.json"
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except Exception:
        return None


def clear_lens_output(building: str) -> None:
    """Remove lens_output.json so the next iteration gets a fresh diagnosis."""
    path = PATHS["outputs_dir"] / building / "lens_output.json"
    if path.exists():
        path.unlink()


def read_slack_approval(building: str) -> bool | None:
    """Return True (approved), False (rejected), None (not yet written)."""
    path = PATHS["outputs_dir"] / building / "slack_approval.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return bool(data.get("approved", False))
    except Exception:
        return None


def clear_slack_approval(building: str) -> None:
    path = PATHS["outputs_dir"] / building / "slack_approval.json"
    if path.exists():
        path.unlink()


# ---------------------------------------------------------------------------
# Metrics helpers
# ---------------------------------------------------------------------------

def compute_metrics(building: str) -> dict[str, float] | None:
    """
    Compute CVRMSE and NMBE from parsed monthly CSV vs ground truth.
    Returns {"cvrmse": float, "nmbe": float} or None.
    """
    try:
        import math as _math
        import numpy as np
        import pandas as pd
    except ImportError:
        return None

    monthly_path = PATHS["outputs_dir"] / building / "parsed" / f"{building}_monthly.csv"
    gt_path = PATHS["gt_dir"] / f"{building}_ground_truth.csv"
    if not monthly_path.exists() or not gt_path.exists():
        return None

    try:
        sim_df = pd.read_csv(monthly_path)
        gt_df  = pd.read_csv(gt_path)
    except Exception:
        return None

    # Filter out ANNUAL summary row
    if "month_name" in sim_df.columns:
        sim_df = sim_df[sim_df["month_name"].astype(str).str.upper() != "ANNUAL"].copy()

    # Pick simulation energy column
    sim_col = None
    for c in ["electricity_facility_kwh", "cooling_elec_adj_kwh", "cooling_elec_kwh"]:
        if c in sim_df.columns:
            sim_col = c
            break
    if sim_col is None:
        return None

    try:
        sim_df["month"] = pd.to_numeric(sim_df["month"], errors="coerce")
        sim_df[sim_col] = pd.to_numeric(sim_df[sim_col], errors="coerce")
        sim_df = sim_df.dropna(subset=["month", sim_col])
        sim_df["month"] = sim_df["month"].astype(int)
        sim_monthly = sim_df.groupby("month")[sim_col].sum()

        gt_df["month"] = pd.to_datetime(gt_df["month"], errors="coerce")
        gt_df["measured_kwh"] = pd.to_numeric(gt_df["measured_kwh"], errors="coerce")
        gt_df = gt_df.dropna(subset=["month", "measured_kwh"])
        gt_df["month_num"] = gt_df["month"].dt.month
        gt_monthly = gt_df.groupby("month_num")["measured_kwh"].mean()

        comp = pd.concat(
            [sim_monthly.rename("sim"), gt_monthly.rename("meas")], axis=1, join="inner"
        ).dropna()

        if comp.empty or comp["meas"].mean() == 0:
            return None

        rmse = float(np.sqrt(((comp["sim"] - comp["meas"]) ** 2).mean()))
        mean_meas = float(comp["meas"].mean())
        sum_meas  = float(comp["meas"].sum())

        cvrmse = rmse / mean_meas * 100.0
        nmbe   = float((comp["sim"] - comp["meas"]).sum()) / sum_meas * 100.0

        if not (_math.isfinite(cvrmse) and _math.isfinite(nmbe)):
            return None

        return {"cvrmse": round(cvrmse, 4), "nmbe": round(nmbe, 4)}
    except Exception:
        return None


def passes_ashrae(metrics: dict[str, float]) -> bool:
    return (metrics["cvrmse"] <= CVRMSE_THRESHOLD
            and abs(metrics["nmbe"]) <= NMBE_THRESHOLD)


# ---------------------------------------------------------------------------
# Count iterations from calibration log
# ---------------------------------------------------------------------------

def count_iterations(building: str) -> int:
    log = PATHS["calibration_log"]
    if not log.exists():
        return 0
    text = log.read_text(errors="replace")
    # Count lines like "## FOE13 — Iteration N — ..."
    count = 0
    for line in text.splitlines():
        if line.startswith(f"## {building} ") and "Iteration" in line:
            count += 1
    return count


# ---------------------------------------------------------------------------
# Script runners
# ---------------------------------------------------------------------------

def run_subprocess(cmd: list[str], logger: logging.Logger, label: str) -> int:
    logger.info("Running %s: %s", label, " ".join(cmd))
    env = {**os.environ, "NUS_PROJECT_DIR": str(NUS_PROJECT_DIR)}
    result = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if result.stdout:
        for line in result.stdout.strip().splitlines()[-20:]:
            logger.info("  [%s stdout] %s", label, line)
    if result.returncode != 0 and result.stderr:
        for line in result.stderr.strip().splitlines()[-10:]:
            logger.warning("  [%s stderr] %s", label, line)
    return result.returncode


def find_idf(building: str) -> Path | None:
    matches = list(PATHS["idf_dir"].rglob(f"{building}.idf"))
    return matches[0] if matches else None


def resolve_epw(month: str) -> Path:
    calibrated = PATHS["calibrated_epw_dir"] / f"{month}_site_calibrated.epw"
    return calibrated if calibrated.exists() else PATHS["base_epw"]


def run_patch_idf(building: str, params: dict[str, float],
                  iteration: int, approver: str, logger: logging.Logger) -> int:
    cmd = [
        sys.executable, str(SCRIPTS["patch_idf"]),
        "--building", building,
        "--iteration", str(iteration),
        "--approver", approver,
    ]
    for param, value in params.items():
        cmd += ["--set", f"{param}={value}"]
    return run_subprocess(cmd, logger, "patch_idf")


def run_simulate(building: str, month: str, logger: logging.Logger) -> int:
    idf = find_idf(building)
    if idf is None:
        logger.error("IDF not found for %s", building)
        return 1
    epw = resolve_epw(month)
    cmd = [
        sys.executable, str(SCRIPTS["simulate"]),
        "--idf",    str(idf),
        "--month",  month,
        "--output", str(PATHS["outputs_dir"]),
        "--gt-dir", str(PATHS["gt_dir"]),
        "--epw",    str(epw),
    ]
    return run_subprocess(cmd, logger, "simulate")


def run_prepare_gt(logger: logging.Logger) -> int:
    cmd = [sys.executable, str(SCRIPTS["prepare_gt"])]
    return run_subprocess(cmd, logger, "prepare_gt")


# ---------------------------------------------------------------------------
# Slack approval: write request sentinel + poll for response
# ---------------------------------------------------------------------------

def write_approval_request(building: str, iteration: int,
                            params: dict[str, float]) -> None:
    """
    Write slack_approval_request.json so Signal can pick it up and message Slack.
    The actual approval (slack_approval.json) is written externally by the Slack
    bot when the engineer replies "approve" or "reject".
    """
    req_path = PATHS["outputs_dir"] / building / "slack_approval_request.json"
    req_path.parent.mkdir(parents=True, exist_ok=True)
    req_path.write_text(json.dumps({
        "building":  building,
        "iteration": iteration,
        "params":    params,
        "requested_at": datetime.now(SGT).isoformat(),
    }, indent=2))


def poll_for_approval(building: str, logger: logging.Logger,
                      max_wait: int) -> bool | None:
    """
    Poll until slack_approval.json appears or max_wait seconds elapse.
    Returns True (approved), False (rejected), None (timed out).
    """
    deadline = time.monotonic() + max_wait
    logger.info("Waiting up to %ds for Slack approval for %s …", max_wait, building)
    while time.monotonic() < deadline:
        result = read_slack_approval(building)
        if result is not None:
            return result
        time.sleep(APPROVAL_POLL_INTERVAL)
    return None  # timed out


# ---------------------------------------------------------------------------
# All-at-bounds check
# ---------------------------------------------------------------------------

def all_params_at_bounds(lens_output: dict, bounds: dict) -> bool:
    """
    Return True if every proposed parameter is already at its min or max bound.
    This signals that further iteration cannot help.
    """
    causes = lens_output.get("likely_causes", [])
    if not causes:
        return False
    for cause in causes:
        param   = cause.get("parameter")
        current = cause.get("current")
        if param not in CALIB_PARAMS or current is None:
            continue
        if param_at_bound(float(current), param, bounds) is None:
            return False
    return True


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def calibration_loop(building: str, month: str, max_wait: int,
                     logger: logging.Logger) -> int:
    """
    Run the calibration loop for one building.
    Returns an EXIT_* code.
    """
    bounds = load_bounds()

    logger.info("=" * 60)
    logger.info("Starting calibration loop for %s (month=%s)", building, month)
    logger.info("=" * 60)

    while True:
        # ── Step 1: Compute current metrics ──────────────────────────────
        metrics = compute_metrics(building)
        if metrics is None:
            logger.error("Could not compute metrics — missing parsed CSV or GT data.")
            return EXIT_ERROR

        cvrmse = metrics["cvrmse"]
        nmbe   = metrics["nmbe"]
        logger.info("Current metrics → CVRMSE=%.2f%%  NMBE=%+.2f%%", cvrmse, nmbe)

        # ── Step 2: Convergence check ─────────────────────────────────────
        if passes_ashrae(metrics):
            logger.info("✅ ASHRAE thresholds met — calibration converged.")
            return EXIT_CONVERGED

        # ── Step 3: Check for Lens output ─────────────────────────────────
        lens_output = read_lens_output(building)
        if lens_output is None:
            # Write lens_input.json for the LLM agent, then exit asking for it
            _write_lens_input(building, metrics, logger)
            logger.info("⏸ Waiting for Lens (DiagnosisAgent) to write lens_output.json")
            logger.info("  Path: %s", PATHS["outputs_dir"] / building / "lens_output.json")
            logger.info("  Re-run this script after Lens writes its output.")
            return EXIT_NEED_LENS

        # ── Step 4: engineer_review_required guard ────────────────────────
        if lens_output.get("engineer_review_required", False):
            logger.warning("⚠️  Lens flagged engineer_review_required — halting loop.")
            logger.warning("   Human inspection needed for %s.", building)
            return EXIT_AT_BOUNDS

        # ── Step 5: All-at-bounds guard ───────────────────────────────────
        if all_params_at_bounds(lens_output, bounds):
            logger.warning(
                "⛔ All calibration parameters at bounds — cannot converge further."
            )
            logger.warning(
                "   CVRMSE=%.2f%%  NMBE=%+.2f%%  — flag for human review.", cvrmse, nmbe
            )
            return EXIT_AT_BOUNDS

        # ── Step 6: Extract proposed patches from Lens output ─────────────
        causes = lens_output.get("likely_causes", [])
        if not causes or not lens_output.get("recommend_recalibration", False):
            logger.info("Lens does not recommend recalibration — loop complete.")
            return EXIT_CONVERGED

        # Build params dict (up to 2, ordered by CALIB_PARAMS priority)
        params: dict[str, float] = {}
        for cause in sorted(causes,
                             key=lambda c: CALIB_PARAMS.index(c["parameter"])
                             if c["parameter"] in CALIB_PARAMS else 99):
            param   = cause.get("parameter")
            suggested = cause.get("suggested")
            confidence = float(cause.get("confidence", 0.0))
            if param not in CALIB_PARAMS:
                logger.warning("Skipping unsupported param '%s'", param)
                continue
            if confidence < 0.70:
                logger.warning(
                    "Skipping '%s' — confidence %.2f < 0.70 (refer to engineer)", param, confidence
                )
                continue
            lo, hi = get_param_bounds(bounds, param)
            clamped = max(lo, min(hi, float(suggested)))
            if clamped != float(suggested):
                logger.info("Clamping %s: %.4f → %.4f (bounds [%.2f, %.2f])",
                            param, suggested, clamped, lo, hi)
            params[param] = round(clamped, 4)
            if len(params) == 2:
                break  # max 2 per iteration

        if not params:
            logger.warning("No valid parameter proposals from Lens — cannot proceed.")
            return EXIT_AT_BOUNDS

        iteration = count_iterations(building) + 1
        logger.info("Iteration %d — proposing: %s", iteration,
                    ", ".join(f"{k}={v}" for k, v in params.items()))

        # ── Step 7: Request Slack approval ────────────────────────────────
        clear_slack_approval(building)
        write_approval_request(building, iteration, params)
        logger.info(
            "📣 Slack approval request written for %s iteration %d — waiting …",
            building, iteration,
        )

        approved = poll_for_approval(building, logger, max_wait)

        if approved is None:
            # Timed out — escalate (caller handles escalation)
            logger.warning(
                "⏱ Approval timeout after %ds — escalating to #private.", max_wait
            )
            return EXIT_WAIT_TIMEOUT

        if not approved:
            logger.info("❌ Approval rejected by engineer — halting calibration.")
            return EXIT_REJECTED

        # ── Step 8: Apply patch ───────────────────────────────────────────
        approver = _read_approver(building) or "Slack-engineer"
        rc = run_patch_idf(building, params, iteration, approver, logger)
        if rc != 0:
            logger.error("patch_idf.py failed (rc=%d) — aborting loop.", rc)
            return EXIT_ERROR

        # ── Step 9: Re-simulate ───────────────────────────────────────────
        logger.info("Running EnergyPlus for %s (iteration %d) …", building, iteration)
        rc = run_simulate(building, month, logger)
        if rc != 0:
            logger.error("simulate.py failed (rc=%d) — aborting loop.", rc)
            return EXIT_ERROR

        # ── Step 10: Recompute ground truth metrics ───────────────────────
        logger.info("Recomputing ground truth metrics …")
        run_prepare_gt(logger)  # non-fatal if it fails; compute_metrics() reads directly

        # ── Step 11: Clear Lens output so next iteration gets fresh diagnosis
        clear_lens_output(building)
        logger.info("Cleared lens_output.json — will re-diagnose next iteration.")

        # Loop back to Step 1 ─────────────────────────────────────────────


# ---------------------------------------------------------------------------
# Helpers for loop
# ---------------------------------------------------------------------------

def _write_lens_input(building: str, metrics: dict[str, float],
                      logger: logging.Logger) -> None:
    """Write lens_input.json so DiagnosisAgent has structured context."""
    out_dir = PATHS["outputs_dir"] / building
    out_dir.mkdir(parents=True, exist_ok=True)

    calibration_log = PATHS["calibration_log"]
    iteration_count = count_iterations(building)

    payload = {
        "building":        building,
        "cvrmse":          metrics["cvrmse"],
        "nmbe":            metrics["nmbe"],
        "monthly_csv":     str(out_dir / "parsed" / f"{building}_monthly.csv"),
        "gt_csv":          str(PATHS["gt_dir"] / f"{building}_ground_truth.csv"),
        "prepared_idf":    str(out_dir / "prepared" / f"{building}_prepared.idf"),
        "calibration_log": str(calibration_log),
        "iteration_count": iteration_count,
        "requested_at":    datetime.now(SGT).isoformat(),
    }
    lens_input_path = out_dir / "lens_input.json"
    lens_input_path.write_text(json.dumps(payload, indent=2))
    logger.info("Wrote lens_input.json → %s", lens_input_path)


def _read_approver(building: str) -> str | None:
    """Try to read the approver name from slack_approval.json."""
    path = PATHS["outputs_dir"] / building / "slack_approval.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        return data.get("approver") or data.get("user") or None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the calibration loop for one NUS building."
    )
    parser.add_argument("--building", required=True, help="Building ID, e.g. FOE13")
    parser.add_argument("--month",    required=True, help="Month YYYY-MM, e.g. 2024-08")
    parser.add_argument(
        "--max-wait", type=int, default=14400,
        help="Max seconds to wait for Slack approval per iteration (default: 14400 = 4h)"
    )
    args = parser.parse_args()

    logger = setup_logger(args.building)
    rc = calibration_loop(args.building, args.month, args.max_wait, logger)

    exit_messages = {
        EXIT_CONVERGED:    f"✅ {args.building} converged — ASHRAE thresholds met.",
        EXIT_ERROR:        f"❌ {args.building} loop aborted due to script error.",
        EXIT_AT_BOUNDS:    f"⛔ {args.building} at parameter bounds — human review required.",
        EXIT_REJECTED:     f"❌ {args.building} recalibration rejected by engineer.",
        EXIT_NEED_LENS:    f"⏸ {args.building} waiting for Lens diagnosis.",
        EXIT_WAIT_TIMEOUT: f"⏱ {args.building} approval timed out — escalated to #private.",
    }
    logger.info(exit_messages.get(rc, f"Unknown exit code {rc}"))
    sys.exit(rc)


if __name__ == "__main__":
    main()
