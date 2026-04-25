"""
build_epw.py — NUS WeatherAgent site-calibrated EPW builder
============================================================
Patches a base Singapore TMY EPW file with validated observed weather data
to produce a site-calibrated EPW for use in EnergyPlus simulations.

Only months with data_quality="good" or "drift_alert" (with a flag, not blocked)
are patched. Months flagged as "poor" or "missing" retain base TMY values.

Usage:
    # Build a calibrated EPW for a single month's data range
    python build_epw.py --month 2024-08 \\
        --base-epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw \\
        --out /Users/ye/nus-energy/weather/calibrated/2024-08_site_calibrated.epw

    # Build a full-year calibrated EPW (patches all available validated months)
    python build_epw.py --year 2024 \\
        --base-epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw \\
        --out /Users/ye/nus-energy/weather/calibrated/2024_site_calibrated.epw
"""

import argparse
import json
import logging
import os
import sys
from pathlib import Path

import numpy as np
import pandas as pd

# ── Paths ─────────────────────────────────────────────────────────────────────
NUS_PROJECT_DIR  = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
WEATHER_DIR      = NUS_PROJECT_DIR / "weather"
OBSERVED_DIR     = WEATHER_DIR / "observed"
CALIBRATED_DIR   = WEATHER_DIR / "calibrated"
CALIBRATED_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(level=logging.INFO, format="%(asctime)s  [%(levelname)s]  %(message)s")
log = logging.getLogger("nimbus.build_epw")

# ── EPW column indices (0-based after splitting the data row on commas) ───────
# EPW spec is 1-based; we subtract 1 here for Python indexing.
EPW_COL_0 = {
    "year":  0,
    "month": 1,
    "day":   2,
    "hour":  3,
    "min":   4,
    "DBT":   6,    # Dry Bulb Temperature
    "DPT":   7,    # Dew Point Temperature
    "RH":    8,    # Relative Humidity
    "GHI":   13,   # Global Horizontal Irradiance (Wh/m²)
    "DNI":   14,   # Direct Normal Irradiance
    "DHI":   15,   # Diffuse Horizontal Irradiance
    "WD":    20,   # Wind Direction
    "WS":    21,   # Wind Speed
}

# Coverage threshold: only patch a column if observed coverage >= this fraction
PATCH_COVERAGE_THRESH = 0.80

# Map observed variable names → EPW column(s) they overwrite
# GHI from a pyranometer is split into DNI+DHI using the Erbs model
OBS_TO_EPW = {
    "DBT":    ["DBT"],
    "RH":     ["RH", "DPT"],   # RH also allows deriving DPT
    "GHI":    ["GHI", "DNI", "DHI"],
    "WS":     ["WS"],
    "WD":     ["WD"],
}


# ══════════════════════════════════════════════════════════════════════════════
# EPW read / write
# ══════════════════════════════════════════════════════════════════════════════

def read_epw(epw_path: str) -> tuple[list[str], pd.DataFrame]:
    """
    Read an EPW file.
    Returns (header_lines, data_df) where data_df has one row per hour (8760)
    and columns are the raw comma-separated EPW fields (as floats where possible).
    """
    path = Path(epw_path)
    if not path.exists():
        raise FileNotFoundError(f"EPW not found: {epw_path}")

    lines = path.read_text(encoding="latin-1").splitlines()

    # EPW header is the first 8 lines; data starts at line 9
    header_lines = lines[:8]
    data_lines   = lines[8:]

    rows = []
    for line in data_lines:
        if not line.strip():
            continue
        parts = line.split(",")
        rows.append(parts)

    df = pd.DataFrame(rows)
    log.info(f"EPW read: {len(df)} data rows from {path.name}")
    return header_lines, df


def write_epw(header_lines: list[str], data_df: pd.DataFrame, out_path: str):
    """
    Write a patched EPW file. header_lines unchanged; data_df rows reassembled.
    """
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)

    def _fmt(v):
        """Format a single EPW cell value — replace NaN/None with 9999 (EPW missing marker)."""
        try:
            import math
            if v is None or (isinstance(v, float) and math.isnan(v)):
                return "9999"
        except (TypeError, ValueError):
            pass
        return str(v)

    lines = header_lines[:]
    for _, row in data_df.iterrows():
        lines.append(",".join(_fmt(v) for v in row))

    out.write_text("\n".join(lines) + "\n", encoding="latin-1")
    log.info(f"EPW written → {out}  ({len(data_df)} data rows)")


# ══════════════════════════════════════════════════════════════════════════════
# Erbs decomposition: GHI → DNI + DHI
# ══════════════════════════════════════════════════════════════════════════════

def ghi_to_dni_dhi(ghi_series: pd.Series, hour_series: pd.Series,
                   lat: float = 1.3) -> tuple[pd.Series, pd.Series]:
    """
    Erbs model: decompose GHI into DNI and DHI.
    lat: latitude in degrees (Singapore ≈ 1.3°N)
    Returns (DNI, DHI) as Wh/m² series.
    """
    # Day of year (approximate from hour index assuming Jan 1 = hour 1)
    doy = np.ceil(hour_series / 24).clip(1, 365)
    # Solar declination (degrees)
    decl = 23.45 * np.sin(np.radians(360 * (284 + doy) / 365))
    # Hour angle (degrees) — 15° per hour, noon=0
    hour_of_day = ((hour_series - 1) % 24) + 1
    ha = (hour_of_day - 12.5) * 15.0

    lat_r  = np.radians(lat)
    decl_r = np.radians(decl)
    ha_r   = np.radians(ha)

    # Cosine of solar zenith angle
    cos_z  = (np.sin(lat_r) * np.sin(decl_r)
               + np.cos(lat_r) * np.cos(decl_r) * np.cos(ha_r))
    cos_z  = cos_z.clip(0, None)  # night hours → 0

    # Extraterrestrial horizontal irradiance
    I0   = 1367.0  # solar constant W/m²
    G_on = I0 * (1 + 0.033 * np.cos(np.radians(360 * doy / 365)))
    G_h  = G_on * cos_z

    kt = ghi_series / G_h.replace(0, np.nan)
    kt = kt.clip(0, 1).fillna(0)

    # Erbs diffuse fraction
    kd = pd.Series(np.where(
        kt <= 0.22,
        1 - 0.09 * kt,
        np.where(
            kt <= 0.80,
            0.9511 - 0.1604*kt + 4.388*kt**2 - 16.638*kt**3 + 12.336*kt**4,
            0.165
        )
    ), index=ghi_series.index)

    dhi = (kd * ghi_series).clip(0)
    dni = ((ghi_series - dhi) / cos_z.replace(0, np.nan)).fillna(0).clip(0)
    return dni, dhi


# ══════════════════════════════════════════════════════════════════════════════
# Dew point derivation from DBT + RH
# ══════════════════════════════════════════════════════════════════════════════

def rh_to_dewpoint(dbt: pd.Series, rh: pd.Series) -> pd.Series:
    """Magnus formula: derive dew point (°C) from dry-bulb temp and RH."""
    a, b = 17.625, 243.04   # Magnus coefficients
    alpha = ((a * dbt) / (b + dbt)) + np.log(rh / 100.0)
    return (b * alpha) / (a - alpha)


# ══════════════════════════════════════════════════════════════════════════════
# Patch one month's EPW rows
# ══════════════════════════════════════════════════════════════════════════════

def patch_month(data_df: pd.DataFrame, obs_df: pd.DataFrame,
                month: int, quality_report: dict) -> tuple[pd.DataFrame, dict]:
    """
    Overwrite EPW data rows for a given month with validated observed values.
    Returns (patched data_df, patch_summary dict).
    """
    summary = {"month": month, "patched": [], "skipped": [], "tmy_retained": []}

    # Rows in EPW for this month
    month_mask = data_df.iloc[:, EPW_COL_0["month"]].astype(int) == month
    n_rows = month_mask.sum()
    if n_rows == 0:
        log.warning(f"  Month {month}: no EPW rows found")
        return data_df, summary

    # Hours for this month in observed data
    obs_month = obs_df[obs_df.index.month == month].copy()
    if obs_month.empty:
        log.info(f"  Month {month}: no observed data — retaining TMY")
        summary["tmy_retained"].append("all")
        return data_df, summary

    # Resample to ensure we have exactly one value per EPW row
    obs_hourly = obs_month.resample("1h").mean()

    # Align to EPW row indices for this month
    epw_rows = data_df[month_mask].copy()
    n_epw    = len(epw_rows)
    n_obs    = len(obs_hourly)

    if abs(n_epw - n_obs) > 2:
        log.warning(f"  Month {month}: EPW rows ({n_epw}) vs obs rows ({n_obs}) mismatch — skipping patch")
        summary["skipped"].append(f"row count mismatch: EPW={n_epw} obs={n_obs}")
        return data_df, summary

    # Trim or pad observed to match EPW row count
    if n_obs > n_epw:
        obs_hourly = obs_hourly.iloc[:n_epw]
    elif n_obs < n_epw:
        pad = pd.DataFrame(np.nan, index=range(n_epw - n_obs), columns=obs_hourly.columns)
        obs_hourly = pd.concat([obs_hourly, pad])

    obs_vals = obs_hourly.reset_index(drop=True)
    epw_idx  = epw_rows.index

    # Hour index for Erbs model (1-based, cumulative within year)
    hour_of_year = pd.Series(range(1, n_epw + 1))

    # ── Patch each variable ────────────────────────────────────────────────
    for obs_var, epw_fields in OBS_TO_EPW.items():
        if obs_var not in obs_vals.columns:
            summary["tmy_retained"].append(obs_var)
            continue

        col_report = quality_report.get("variables", {}).get(obs_var, {})
        coverage   = col_report.get("coverage_pct", 0) / 100.0

        if coverage < PATCH_COVERAGE_THRESH:
            log.info(f"  Month {month} {obs_var}: coverage {coverage*100:.1f}% < threshold — TMY retained")
            summary["tmy_retained"].append(obs_var)
            continue

        obs_col = obs_vals[obs_var]

        if obs_var == "GHI":
            # Split GHI → DNI + DHI using Erbs model
            dni, dhi = ghi_to_dni_dhi(obs_col, hour_of_year)
            data_df.loc[epw_idx, EPW_COL_0["GHI"]] = obs_col.values.round(1)
            data_df.loc[epw_idx, EPW_COL_0["DNI"]] = dni.values.round(1)
            data_df.loc[epw_idx, EPW_COL_0["DHI"]] = dhi.values.round(1)
            log.info(f"  Month {month}: GHI/DNI/DHI patched from observed (coverage={coverage*100:.0f}%)")
            summary["patched"].extend(["GHI", "DNI", "DHI"])

        elif obs_var == "RH":
            # Patch RH
            data_df.loc[epw_idx, EPW_COL_0["RH"]] = obs_col.values.round(1)
            # Derive and patch DPT if DBT is also available (patched or TMY)
            dbt_col_vals = data_df.loc[epw_idx, EPW_COL_0["DBT"]].astype(float)
            dpt = rh_to_dewpoint(dbt_col_vals.reset_index(drop=True), obs_col)
            data_df.loc[epw_idx, EPW_COL_0["DPT"]] = dpt.values.round(1)
            log.info(f"  Month {month}: RH + DPT patched (coverage={coverage*100:.0f}%)")
            summary["patched"].extend(["RH", "DPT"])

        else:
            # Direct patch
            epw_col = EPW_COL_0[epw_fields[0]]
            data_df.loc[epw_idx, epw_col] = obs_col.values.round(2)
            log.info(f"  Month {month}: {obs_var} patched (coverage={coverage*100:.0f}%)")
            summary["patched"].append(obs_var)

    return data_df, summary


# ══════════════════════════════════════════════════════════════════════════════
# Build EPW: main orchestration
# ══════════════════════════════════════════════════════════════════════════════

def build_calibrated_epw(base_epw: str, out_path: str,
                         months: list[str]) -> dict:
    """
    Patch base EPW with validated observed data for each listed month.
    months: list of 'YYYY-MM' strings.
    Returns a manifest dict describing what was patched.
    """
    header_lines, data_df = read_epw(base_epw)
    manifest = {
        "base_epw":  base_epw,
        "out_epw":   out_path,
        "months":    {},
        "overall_quality": "good",
    }

    for month_str in months:
        year, mon = int(month_str.split("-")[0]), int(month_str.split("-")[1])

        # Load validated observed data
        validated_path = OBSERVED_DIR / f"{month_str}_validated.parquet"
        quality_path   = OBSERVED_DIR / f"{month_str}_quality_report.json"

        if not validated_path.exists():
            log.warning(f"[{month_str}] No validated data — TMY retained for this month")
            manifest["months"][month_str] = {"status": "TMY_retained", "reason": "no validated data"}
            continue

        if not quality_path.exists():
            log.warning(f"[{month_str}] No quality report — TMY retained for this month")
            manifest["months"][month_str] = {"status": "TMY_retained", "reason": "no quality report"}
            continue

        with open(quality_path) as f:
            quality_report = json.load(f)

        quality = quality_report.get("data_quality", "poor")
        if quality == "poor":
            log.warning(f"[{month_str}] Quality=poor — TMY retained for this month")
            manifest["months"][month_str] = {
                "status": "TMY_retained", "reason": "data_quality=poor",
                "flags":  quality_report.get("flags", []),
            }
            manifest["overall_quality"] = "partial"
            continue

        obs_df = pd.read_parquet(validated_path)
        obs_df.index = pd.to_datetime(obs_df.index, utc=True)

        data_df, patch_summary = patch_month(data_df, obs_df, mon, quality_report)
        manifest["months"][month_str] = {
            "status":        "patched",
            "data_quality":  quality,
            "patched_vars":  patch_summary["patched"],
            "tmy_retained":  patch_summary["tmy_retained"],
            "flags":         quality_report.get("flags", []),
        }
        log.info(f"[{month_str}] Patched: {patch_summary['patched']}  TMY: {patch_summary['tmy_retained']}")

    write_epw(header_lines, data_df, out_path)

    # Save manifest
    manifest_path = Path(out_path).with_suffix(".json")
    with open(manifest_path, "w") as f:
        json.dump(manifest, f, indent=2)
    log.info(f"Manifest → {manifest_path}")

    # Print summary
    n_patched = sum(1 for v in manifest["months"].values() if v["status"] == "patched")
    n_tmy     = sum(1 for v in manifest["months"].values() if v["status"] == "TMY_retained")
    print(f"\n🌦️ Calibrated EPW built:")
    print(f"   Output:         {out_path}")
    print(f"   Months patched: {n_patched}/{len(months)}")
    print(f"   TMY retained:   {n_tmy}/{len(months)}")
    for m, info in manifest["months"].items():
        icon = "✅" if info["status"] == "patched" else "⬜"
        print(f"   {icon} {m}  {info['status']}  vars={info.get('patched_vars',[])} flags={info.get('flags',[])}")

    return manifest


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Nimbus — site-calibrated EPW builder")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--month", help="Single month: YYYY-MM")
    group.add_argument("--year",  type=int, help="Full year: YYYY (patches all available months)")
    parser.add_argument("--base-epw", required=True, help="Path to base Singapore TMY EPW")
    parser.add_argument("--out",      required=True, help="Output calibrated EPW path")
    args = parser.parse_args()

    if args.month:
        months = [args.month]
    else:
        months = [f"{args.year}-{m:02d}" for m in range(1, 13)]

    build_calibrated_epw(args.base_epw, args.out, months)


if __name__ == "__main__":
    main()
