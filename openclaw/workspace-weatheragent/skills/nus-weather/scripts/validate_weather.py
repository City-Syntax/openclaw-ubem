"""
validate_weather.py — NUS WeatherAgent data validation
=======================================================
Performs spike detection, gap filling, and data quality flagging on archived
hourly weather observations before they are used to patch an EPW file.

Usage:
    python validate_weather.py --month 2024-08
    python validate_weather.py --month 2024-08 --strict   # fail on poor months
"""

import argparse
import json
import logging
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
import os
NUS_PROJECT_DIR = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
OBSERVED_DIR    = NUS_PROJECT_DIR / "weather" / "observed"

logging.basicConfig(level=logging.INFO, format="%(asctime)s  [%(levelname)s]  %(message)s")
log = logging.getLogger("nimbus.validate")

# ── Thresholds ────────────────────────────────────────────────────────────────
SPIKE_SIGMA          = 4.0    # rolling-window z-score threshold for spikes
ROLLING_WINDOW_H     = 24     # hours for rolling mean/std
MAX_INTERP_GAP_H     = 3      # interpolate gaps up to this many consecutive hours
POOR_COVERAGE_THRESH = 0.80   # flag month as "poor" if <80% of hours are valid

# MSS monthly normals — Singapore mean DBT (°C) by month
MSS_DBT_NORMALS = {
    1: 26.5, 2: 27.1, 3: 27.5, 4: 28.1,
    5: 28.3, 6: 28.5, 7: 28.2, 8: 28.1,
    9: 27.9, 10: 27.6, 11: 27.0, 12: 26.7,
}
DBT_DRIFT_TOLERANCE = 2.0   # °C — alert if observed mean deviates by more than this


# ══════════════════════════════════════════════════════════════════════════════
# Spike detection
# ══════════════════════════════════════════════════════════════════════════════

def detect_spikes(series: pd.Series, sigma: float = SPIKE_SIGMA,
                  window: int = ROLLING_WINDOW_H) -> pd.Series:
    """
    Return a boolean mask of spike positions using a rolling z-score approach.
    True = spike (value is >sigma standard deviations from the rolling mean).
    """
    roll_mean = series.rolling(window=window, center=True, min_periods=6).mean()
    roll_std  = series.rolling(window=window, center=True, min_periods=6).std()
    z_score   = (series - roll_mean) / roll_std.replace(0, np.nan)
    return z_score.abs() > sigma


def remove_spikes(series: pd.Series, spike_mask: pd.Series) -> tuple[pd.Series, int]:
    """Replace spikes with NaN. Returns (cleaned series, n_spikes_removed)."""
    n_spikes = spike_mask.sum()
    cleaned = series.copy()
    cleaned[spike_mask] = np.nan
    return cleaned, int(n_spikes)


# ══════════════════════════════════════════════════════════════════════════════
# Gap filling
# ══════════════════════════════════════════════════════════════════════════════

def fill_gaps(series: pd.Series, max_gap: int = MAX_INTERP_GAP_H) -> tuple[pd.Series, int, int]:
    """
    Fill NaN gaps using linear interpolation, up to max_gap consecutive hours.
    Larger gaps are left as NaN.
    Returns (filled series, n_filled, n_remaining_gaps).
    """
    # Identify runs of NaN
    is_nan = series.isna()
    filled = series.copy()
    n_filled = 0
    n_remaining = 0

    # Walk the NaN runs
    in_gap = False
    gap_start = None
    for i, val in enumerate(is_nan):
        if val and not in_gap:
            in_gap = True
            gap_start = i
        elif not val and in_gap:
            gap_len = i - gap_start
            if gap_len <= max_gap:
                # Interpolate this run
                filled.iloc[gap_start:i] = np.nan  # ensure NaN for limit_area
                n_filled += gap_len
            else:
                n_remaining += gap_len
            in_gap = False
    if in_gap:
        # Gap at end of series
        gap_len = len(series) - gap_start
        n_remaining += gap_len

    # Run pandas interpolation with limit to respect max_gap
    filled = filled.interpolate(method="linear", limit=max_gap, limit_area="inside")
    return filled, n_filled, n_remaining


# ══════════════════════════════════════════════════════════════════════════════
# Coverage assessment
# ══════════════════════════════════════════════════════════════════════════════

def assess_coverage(df: pd.DataFrame, month: str) -> dict:
    """
    Calculate per-variable coverage and an overall month quality flag.
    Returns a quality report dict.
    """
    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    if mon == 12:
        expected_hours = 31 * 24
    else:
        import calendar
        expected_hours = calendar.monthrange(year, mon)[1] * 24

    report = {
        "month": month,
        "expected_hours": expected_hours,
        "actual_rows": len(df),
        "variables": {},
        "data_quality": "good",
        "flags": [],
    }

    # Solar radiation variables are excluded from the overall quality gate because
    # GHI/DNI/DHI are always ~0 at night — nighttime nulls are expected, not gaps.
    # build_epw.py handles them separately with TMY fallback when coverage < 80%.
    SOLAR_VARS = {"GHI", "DNI", "DHI"}

    coverages = []
    for col in df.columns:
        valid = df[col].notna().sum()
        coverage = valid / expected_hours
        report["variables"][col] = {
            "valid_hours": int(valid),
            "coverage_pct": round(coverage * 100, 1),
        }
        if col not in SOLAR_VARS:
            coverages.append(coverage)
        if coverage < POOR_COVERAGE_THRESH:
            report["flags"].append(
                f"{col} coverage {coverage*100:.1f}% < {POOR_COVERAGE_THRESH*100:.0f}% threshold"
            )

    overall_coverage = min(coverages) if coverages else 0.0
    if overall_coverage < POOR_COVERAGE_THRESH:
        report["data_quality"] = "poor"

    # DBT drift check vs MSS normals
    if "DBT" in df.columns:
        obs_mean = df["DBT"].mean()
        normal = MSS_DBT_NORMALS.get(mon)
        if normal is not None and not np.isnan(obs_mean):
            delta = abs(obs_mean - normal)
            report["variables"]["DBT"]["obs_mean_c"]    = round(obs_mean, 2)
            report["variables"]["DBT"]["mss_normal_c"]  = normal
            report["variables"]["DBT"]["delta_c"]       = round(delta, 2)
            if delta > DBT_DRIFT_TOLERANCE:
                report["flags"].append(
                    f"DBT drift: observed mean {obs_mean:.1f}°C vs MSS normal {normal:.1f}°C "
                    f"(Δ={delta:.1f}°C > {DBT_DRIFT_TOLERANCE}°C tolerance)"
                )
                if report["data_quality"] != "poor":
                    report["data_quality"] = "drift_alert"

    return report


# ══════════════════════════════════════════════════════════════════════════════
# Main validation pipeline
# ══════════════════════════════════════════════════════════════════════════════

def validate_month(month: str, strict: bool = False) -> dict:
    """
    Full validation pipeline for one archived month:
    1. Load from Parquet archive
    2. Spike detection + removal
    3. Gap filling (up to 3h)
    4. Coverage assessment + quality flag
    5. Save validated Parquet back to archive

    Returns a quality report dict.
    """
    archive_path = OBSERVED_DIR / f"{month}.parquet"
    if not archive_path.exists():
        log.error(f"[{month}] No archive found at {archive_path}")
        return {"month": month, "data_quality": "missing", "error": "archive not found"}

    df = pd.read_parquet(archive_path)
    log.info(f"[{month}] Loaded {df.shape[0]} rows × {df.shape[1]} cols from archive")

    spike_log = {}
    fill_log  = {}

    for col in df.columns:
        if df[col].dtype not in [np.float64, np.float32, float]:
            continue

        # Step 1: spike detection and removal
        spike_mask = detect_spikes(df[col])
        df[col], n_spikes = remove_spikes(df[col], spike_mask)
        if n_spikes:
            log.warning(f"[{month}] {col}: removed {n_spikes} spike(s) → NaN")
            spike_log[col] = n_spikes

        # Step 2: gap filling
        df[col], n_filled, n_remaining = fill_gaps(df[col])
        fill_log[col] = {"filled": n_filled, "remaining_gaps": n_remaining}
        if n_filled:
            log.info(f"[{month}] {col}: interpolated {n_filled} hour(s)")
        if n_remaining:
            log.warning(f"[{month}] {col}: {n_remaining} hour(s) still missing after fill")

    # Step 3: coverage assessment
    report = assess_coverage(df, month)
    report["spikes_removed"] = spike_log
    report["gaps_filled"]    = fill_log

    # Step 4: save validated data back
    validated_path = OBSERVED_DIR / f"{month}_validated.parquet"
    df.to_parquet(validated_path)
    report["validated_path"] = str(validated_path)
    log.info(f"[{month}] Validated data saved → {validated_path}")

    # Step 5: save quality report JSON
    report_path = OBSERVED_DIR / f"{month}_quality_report.json"
    with open(report_path, "w") as f:
        json.dump(report, f, indent=2, default=str)
    log.info(f"[{month}] Quality report → {report_path}")

    # Print summary
    quality = report["data_quality"]
    flags   = report["flags"]
    icon    = "✅" if quality == "good" else ("⚠️" if quality == "drift_alert" else "🔴")
    print(f"\n{icon} {month} — quality: {quality}")
    for v, stats in report["variables"].items():
        print(f"   {v:<8} {stats.get('coverage_pct', 0):>5.1f}% valid"
              f"  ({stats.get('valid_hours','?')}/{report['expected_hours']} h)")
    if flags:
        print("   Flags:")
        for flag in flags:
            print(f"     ⚠️ {flag}")

    if strict and quality in ("poor", "missing"):
        log.error(f"[{month}] Strict mode: exiting due to quality={quality}")
        sys.exit(1)

    return report


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Nimbus — weather data validator")
    parser.add_argument("--month", required=True,
                        help="Month to validate: YYYY-MM")
    parser.add_argument("--strict", action="store_true",
                        help="Exit non-zero if month quality is 'poor'")
    args = parser.parse_args()
    validate_month(args.month, strict=args.strict)


if __name__ == "__main__":
    main()
