"""
fetch_weather.py — NUS WeatherAgent data acquisition script
============================================================
Pulls hourly weather observations from NUS onsite stations and/or the
data.gov.sg Realtime Weather API, and archives them as monthly Parquet files.

Usage:
    # Fetch current conditions from all stations (heartbeat mode)
    python fetch_weather.py --source station --station all

    # Fetch a full month's data (auto: try station first, fall back to API)
    python fetch_weather.py --month 2024-08 --source auto

    # Force API-only fetch for a specific month
    python fetch_weather.py --month 2024-08 --source api

    # Fetch from a specific station only
    python fetch_weather.py --month 2024-08 --source station --station NUS_KR_01
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import requests
from dotenv import load_dotenv

# ── Paths ─────────────────────────────────────────────────────────────────────
NUS_PROJECT_DIR   = Path(os.getenv("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))
WEATHER_DIR       = NUS_PROJECT_DIR / "weather"
OBSERVED_DIR      = WEATHER_DIR / "observed"
STATION_CFG_PATH  = WEATHER_DIR / "station_config.json"
LATEST_JSON_PATH  = OBSERVED_DIR / "latest_conditions.json"
DOTENV_PATH       = Path("/Users/ye/.openclaw/workspace-weatheragent/.env")

OBSERVED_DIR.mkdir(parents=True, exist_ok=True)
load_dotenv(DOTENV_PATH)

# ── Logging ───────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  [%(levelname)s]  %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(str(WEATHER_DIR / "weather_fetch.log")),
    ],
)
log = logging.getLogger("nimbus.fetch")

# ── NUS Localized Weather Station API ───────────────────────────────────────────
NUS_LOCALIZED_API_BASE = "your API"
NUS_LOCALIZED_STATIONS = ["MET_E1A"]

# ── data.gov.sg API config ────────────────────────────────────────────────────
DATA_GOV_BASE    = "api website"
FALLBACK_STATION = "S121"   # Clementi — closest to NUS Kent Ridge

DATAGOV_ENDPOINTS = {
    "DBT":    "air-temperature",
    "RH":     "relative-humidity",
    "WS":     "wind-speed",
    "WD":     "wind-direction",
    "Precip": "rainfall",
    # GHI not available via data.gov.sg — uses MSS monthly normals or TMY fallback
}

# ── EPW column mapping ────────────────────────────────────────────────────────
# Maps variable name → EPW data column index (1-based, as per EPW spec)
EPW_COL = {
    "DBT":    7,
    "DPT":    8,   # Dew Point — derived from RH if not directly measured
    "RH":     9,
    "GHI":    14,
    "DNI":    15,
    "DHI":    16,
    "WD":     21,
    "WS":     22,
    "Precip": 33,
}

# ── Sensor quality bounds (Singapore climate) ─────────────────────────────────
SENSOR_BOUNDS = {
    "DBT":    (22.0,  40.0),    # °C
    "RH":     (30.0, 100.0),    # %
    "GHI":    (0.0,  1200.0),   # W/m²
    "WS":     (0.0,   25.0),    # m/s
    "WD":     (0.0,  360.0),    # degrees
    "Precip": (0.0,  150.0),    # mm/hr (extreme tropical downpour upper bound)
}


# ══════════════════════════════════════════════════════════════════════════════
# NUS Localized API fetch
# ══════════════════════════════════════════════════════════════════════════════

def fetch_nus_localized_api_month(station_id: str, month: str) -> pd.DataFrame | None:
    """
    Fetch a full month of hourly data from the NUS localized weather station API.
    month: 'YYYY-MM' or 'YYYYMM'
    Returns DataFrame(index=DatetimeIndex[UTC], columns=[DBT,RH,GHI,WS,WD]) or None on failure.
    """
    month_fmt = month.replace("-", "")  # YYYYMM
    url = f"{NUS_LOCALIZED_API_BASE}/{station_id}/{month_fmt}"
    log.info(f"[nus_localized] Fetching {station_id} {month_fmt} — {url}")

    try:
        resp = requests.get(url, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"[nus_localized] Fetch failed for {station_id} {month_fmt}: {e}")
        return None

    # Check for API error response
    if "errorMessage" in data:
        log.warning(f"[nus_localized] API error for {station_id} {month_fmt}: {data.get('errorMessage')}")
        return None

    # Variable mapping: API key → DataFrame column
    var_map = {
        "AT3M":           "DBT",
        "RH3M":           "RH",
        "SolarRadiation": "GHI",
        "WindSpeed":      "WS",
        "WindDirection":  "WD",
    }

    # Use AT3M to get the date/hour grid
    ref_key = "AT3M"
    if ref_key not in data:
        log.warning(f"[nus_localized] Missing reference key '{ref_key}' in response for {station_id} {month_fmt}")
        return None

    ref = data[ref_key]
    dates = ref.get("x", [])   # list of 'YYYY-MM-DD' strings
    hours = ref.get("y", [])   # list of 'HH:MM' strings
    z_ref = ref.get("z", [])   # z[hour][day]

    if not dates or not hours or not z_ref:
        log.warning(f"[nus_localized] Empty grid for {station_id} {month_fmt}")
        return None

    # Build DatetimeIndex: all combinations of date × hour
    timestamps = []
    for h_idx, hour_str in enumerate(hours):
        for d_idx, date_str in enumerate(dates):
            ts = pd.Timestamp(f"{date_str}T{hour_str}:00", tz="UTC")
            timestamps.append((ts, h_idx, d_idx))

    # Build DataFrame row-by-row
    records = {ts: {} for ts, _, _ in timestamps}
    for api_key, col in var_map.items():
        if api_key not in data:
            log.warning(f"[nus_localized] Variable '{api_key}' not in response — column '{col}' will be NaN")
            for ts, _, _ in timestamps:
                records[ts].setdefault(col, np.nan)
            continue
        z = data[api_key].get("z", [])
        for ts, h_idx, d_idx in timestamps:
            try:
                val = z[h_idx][d_idx]
                records[ts][col] = float(val) if val is not None else np.nan
            except (IndexError, TypeError, ValueError):
                records[ts][col] = np.nan

    df = pd.DataFrame.from_dict(records, orient="index")
    df.index = pd.DatetimeIndex(df.index, tz="UTC")
    df.index.name = "timestamp"
    df = df.sort_index()

    # Ensure all expected columns exist
    for col in ["DBT", "RH", "GHI", "WS", "WD"]:
        if col not in df.columns:
            df[col] = np.nan
    df = df[["DBT", "RH", "GHI", "WS", "WD"]]

    log.info(f"[nus_localized] {station_id} {month_fmt}: {df.shape[0]} rows, "
             f"DBT coverage={df['DBT'].notna().mean()*100:.1f}%")
    return df


def fetch_nus_localized_api_current(station_id: str) -> dict | None:
    """
    Fetch the most recent reading from the NUS localized API via LatestValue.
    Walks back up to 12 months to find the latest available month (API only
    holds data up to some lag behind the current calendar month).
    Returns flat dict {DBT, RH, GHI, WS, WD, timestamp} or None on failure.
    """
    now = datetime.now(tz=timezone.utc)
    log.info(f"[nus_localized_current] Fetching LatestValue from {station_id}")

    # Walk back from current month until we get a valid response (up to 12 months)
    data = None
    month_fmt = None
    for months_back in range(0, 13):
        if now.month - months_back <= 0:
            year  = now.year - 1
            month = now.month - months_back + 12
        else:
            year  = now.year
            month = now.month - months_back
        month_fmt = f"{year}{month:02d}"
        url = f"{NUS_LOCALIZED_API_BASE}/{station_id}/{month_fmt}"
        try:
            resp = requests.get(url, timeout=15)
            resp.raise_for_status()
            candidate = resp.json()
            if isinstance(candidate, dict) and "LatestValue" in candidate:
                data = candidate
                log.info(f"[nus_localized_current] Found data at {month_fmt} (months_back={months_back})")
                break
            else:
                log.debug(f"[nus_localized_current] {month_fmt} has no LatestValue, trying earlier")
        except Exception as e:
            log.debug(f"[nus_localized_current] {month_fmt} failed: {e}")

    if data is None:
        log.warning(f"[nus_localized_current] No LatestValue found for {station_id} in last 12 months")
        return None

    # LatestValue is a top-level key in the API response (not nested per-variable)
    lv = data["LatestValue"]
    lv_map = {
        "AT3M":          "DBT",
        "RH3M":          "RH",
        "SolarRadiation": "GHI",
        "WindSpeed":     "WS",
        "WindDirection": "WD",
    }

    # Use the timestamp reported by the API itself if available
    lv_ts = lv.get("Timestamp")
    try:
        ts_str = pd.Timestamp(lv_ts).isoformat() if lv_ts else now.isoformat()
    except Exception:
        ts_str = now.isoformat()

    result = {
        "timestamp": ts_str,
        "source":    f"nus_localized:{station_id}",
    }
    found_any = False
    for api_key, col in lv_map.items():
        raw = lv.get(api_key)
        if raw is not None:
            try:
                val = float(raw)
                result[col] = None if (val != val) else val  # NaN → None
                if result[col] is not None:
                    found_any = True
            except (TypeError, ValueError):
                result[col] = None
        else:
            result[col] = None

    if not found_any:
        log.warning(f"[nus_localized_current] LatestValue present but all values null for {station_id}")
        return None

    log.info(f"[nus_localized_current] {station_id} ({month_fmt}) — DBT={result.get('DBT')}°C  RH={result.get('RH')}%")
    return result


# ══════════════════════════════════════════════════════════════════════════════
# Station config loader
# ══════════════════════════════════════════════════════════════════════════════

def load_station_config() -> dict:
    if not STATION_CFG_PATH.exists():
        log.warning(f"Station config not found at {STATION_CFG_PATH} — API-only mode")
        return {"stations": [], "api_fallback": {
            "provider": "data.gov.sg",
            "preferred_station_id": FALLBACK_STATION,
            "base_url": DATA_GOV_BASE,
        }}
    with open(STATION_CFG_PATH) as f:
        return json.load(f)


# ══════════════════════════════════════════════════════════════════════════════
# Onsite station fetch
# ══════════════════════════════════════════════════════════════════════════════

def fetch_station_current(station: dict) -> dict | None:
    """
    Fetch the latest reading from a single onsite station.
    Returns a flat dict of {variable: value} or None on failure.
    """
    station_id = station["id"]

    if station["type"] == "rest":
        api_key = os.getenv(station.get("api_key_env", ""), "")
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        try:
            resp = requests.get(station["url"], headers=headers, timeout=10)
            resp.raise_for_status()
            data = resp.json()
            # Expected schema: {"timestamp": "...", "DBT": 28.4, "RH": 82.1, ...}
            log.info(f"[{station_id}] REST fetch OK — {data.get('timestamp','?')}")
            return data
        except Exception as e:
            log.warning(f"[{station_id}] REST fetch failed: {e}")
            return None

    elif station["type"] == "csv_push":
        csv_path = Path(station["csv_path"])
        if not csv_path.exists():
            log.warning(f"[{station_id}] CSV push file not found: {csv_path}")
            return None
        try:
            df = pd.read_csv(csv_path)
            row = df.iloc[-1].to_dict()
            log.info(f"[{station_id}] CSV push read OK — {row.get('timestamp','?')}")
            return row
        except Exception as e:
            log.warning(f"[{station_id}] CSV push read failed: {e}")
            return None

    else:
        log.warning(f"[{station_id}] Unknown station type: {station['type']}")
        return None


def fetch_station_historical(station: dict, month: str) -> pd.DataFrame | None:
    """
    Fetch a full month of hourly data from a REST station.
    month: 'YYYY-MM'
    Returns a DataFrame indexed by hourly timestamps, or None on failure.
    """
    station_id = station["id"]
    if station["type"] != "rest":
        log.info(f"[{station_id}] Historical fetch only supported for REST stations")
        return None

    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    start = datetime(year, mon, 1, tzinfo=timezone.utc)
    # End: first hour of next month
    if mon == 12:
        end = datetime(year + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(year, mon + 1, 1, tzinfo=timezone.utc)

    api_key = os.getenv(station.get("api_key_env", ""), "")
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}

    # Most station APIs support ?from=<ISO>&to=<ISO> for historical pulls
    url = station["url"].replace("/latest", "/historical")
    params = {
        "from": start.isoformat(),
        "to":   end.isoformat(),
        "interval": "hourly",
    }

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        resp.raise_for_status()
        data = resp.json()
        # Expected: {"readings": [{"timestamp": "...", "DBT": ..., "RH": ..., ...}, ...]}
        readings = data.get("readings", [])
        if not readings:
            log.warning(f"[{station_id}] Historical API returned empty readings for {month}")
            return None
        df = pd.DataFrame(readings)
        df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True)
        df = df.set_index("timestamp").sort_index()
        log.info(f"[{station_id}] Historical fetch OK — {len(df)} rows for {month}")
        return df
    except Exception as e:
        log.warning(f"[{station_id}] Historical fetch failed for {month}: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# data.gov.sg API fetch
# ══════════════════════════════════════════════════════════════════════════════

def _datagov_fetch_day(variable: str, date_str: str, station_id: str = FALLBACK_STATION) -> list[dict]:
    """
    Fetch one day of 5-minute readings for a single variable from data.gov.sg.
    Returns a list of {timestamp, value} dicts.
    """
    endpoint = DATAGOV_ENDPOINTS.get(variable)
    if not endpoint:
        return []

    url = f"{DATA_GOV_BASE}/{endpoint}"
    params = {"date": date_str}

    try:
        resp = requests.get(url, params=params, timeout=15)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.warning(f"[datagov] {variable} fetch failed for {date_str}: {e}")
        return []

    readings = []
    for item in data.get("items", []):
        ts = item.get("timestamp")
        for r in item.get("readings", []):
            if r.get("station_id") == station_id:
                readings.append({"timestamp": ts, "value": r.get("value")})
    return readings


def fetch_datagov_month(month: str, station_id: str = FALLBACK_STATION) -> pd.DataFrame:
    """
    Fetch a full month of all supported weather variables from data.gov.sg.
    Resamples 5-min readings to hourly means.
    Returns DataFrame with columns: DBT, RH, WS, WD, Precip.
    """
    year, mon = int(month.split("-")[0]), int(month.split("-")[1])
    if mon == 12:
        days_in_month = 31
    else:
        days_in_month = (datetime(year, mon + 1, 1) - datetime(year, mon, 1)).days

    log.info(f"[datagov] Fetching {month} — {days_in_month} days, station {station_id}")

    all_frames = {}
    for variable in DATAGOV_ENDPOINTS:
        day_series = []
        for day in range(1, days_in_month + 1):
            date_str = f"{year}-{mon:02d}-{day:02d}"
            readings = _datagov_fetch_day(variable, date_str, station_id)
            day_series.extend(readings)
        if day_series:
            df_var = pd.DataFrame(day_series)
            df_var["timestamp"] = pd.to_datetime(df_var["timestamp"], utc=True)
            df_var = df_var.set_index("timestamp")["value"]
            # Resample 5-min to hourly
            if variable == "Precip":
                df_var = df_var.resample("1h").sum()   # rainfall: sum
            else:
                df_var = df_var.resample("1h").mean()  # all others: mean
            all_frames[variable] = df_var
            log.info(f"[datagov]   {variable}: {len(df_var)} hourly rows")
        else:
            log.warning(f"[datagov]   {variable}: no data returned")

    if not all_frames:
        log.error(f"[datagov] No data for any variable for {month}")
        return pd.DataFrame()

    df = pd.concat(all_frames, axis=1)
    df.index.name = "timestamp"
    log.info(f"[datagov] Combined DataFrame: {df.shape[0]} rows × {df.shape[1]} cols")
    return df


def fetch_datagov_current(station_id: str = FALLBACK_STATION) -> dict | None:
    """
    Fetch the most recent reading for all variables from data.gov.sg.
    Returns a flat dict or None on full failure.
    """
    now_str = datetime.now().strftime("%Y-%m-%d")
    result = {"timestamp": datetime.now(tz=timezone.utc).isoformat(), "source": "data.gov.sg"}

    for variable in DATAGOV_ENDPOINTS:
        readings = _datagov_fetch_day(variable, now_str, station_id)
        if readings:
            result[variable] = readings[-1]["value"]  # most recent
        else:
            result[variable] = None

    if all(v is None for k, v in result.items() if k not in ("timestamp", "source")):
        log.error("[datagov] All variables returned None — API may be down")
        return None

    return result


# ══════════════════════════════════════════════════════════════════════════════
# Sensor quality check (hard bounds only — spike detection is in validate_weather.py)
# ══════════════════════════════════════════════════════════════════════════════

def _hard_bound_check(df: pd.DataFrame) -> pd.DataFrame:
    """Replace values outside hard physical bounds with NaN."""
    for var, (lo, hi) in SENSOR_BOUNDS.items():
        if var in df.columns:
            mask = (df[var] < lo) | (df[var] > hi)
            n_bad = mask.sum()
            if n_bad:
                log.warning(f"  Hard bound: {n_bad} out-of-range {var} values → NaN")
                df.loc[mask, var] = np.nan
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Archive to Parquet
# ══════════════════════════════════════════════════════════════════════════════

def save_to_archive(df: pd.DataFrame, month: str, source: str):
    """Save validated hourly DataFrame to the monthly Parquet archive."""
    out_path = OBSERVED_DIR / f"{month}.parquet"
    df.attrs["source"] = source
    df.attrs["month"]  = month
    df.to_parquet(out_path)
    log.info(f"[archive] Saved {df.shape[0]} rows → {out_path}  (source={source})")


def load_from_archive(month: str) -> pd.DataFrame | None:
    """Load a previously archived month, or None if not present."""
    path = OBSERVED_DIR / f"{month}.parquet"
    if not path.exists():
        return None
    df = pd.read_parquet(path)
    log.info(f"[archive] Loaded {month} — {df.shape[0]} rows from {path}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Update latest_conditions.json
# ══════════════════════════════════════════════════════════════════════════════

def update_latest_conditions(readings: dict, source: str, station_id: str = None):
    """
    Write the current-conditions snapshot to latest_conditions.json.
    Downstream agents (Compass, Radar) read this file.
    """
    payload = {
        "timestamp":   readings.get("timestamp", datetime.now(tz=timezone.utc).isoformat()),
        "source":      source,
        "station_id":  station_id,
        "conditions": {
            k: readings.get(k)
            for k in ("DBT", "RH", "GHI", "WS", "WD", "Precip")
        },
        "units": {
            "DBT": "°C", "RH": "%", "GHI": "W/m²",
            "WS": "m/s", "WD": "degrees", "Precip": "mm/hr",
        },
    }
    LATEST_JSON_PATH.write_text(json.dumps(payload, indent=2, default=str))
    log.info(f"[latest] Updated {LATEST_JSON_PATH}")


# ══════════════════════════════════════════════════════════════════════════════
# Main fetch orchestration
# ══════════════════════════════════════════════════════════════════════════════

def fetch_current(config: dict, station_filter: str = "all") -> dict:
    """
    Fetch current conditions. Try onsite stations first, fall back to data.gov.sg.
    Returns merged reading dict from the best available source.
    """
    stations = config.get("stations", [])
    if station_filter != "all":
        stations = [s for s in stations if s["id"] == station_filter]

    merged = {}

    # Try NUS localized API first
    for sid in NUS_LOCALIZED_STATIONS:
        reading = fetch_nus_localized_api_current(sid)
        if reading:
            merged[sid] = reading
            update_latest_conditions(reading, source=reading.get("source", f"nus_localized:{sid}"), station_id=sid)
            log.info(f"[current] nus_localized:{sid} — DBT={reading.get('DBT')}°C  RH={reading.get('RH')}%")
            break  # one localized station is sufficient

    if not merged:
        for station in stations:
            reading = fetch_station_current(station)
            if reading:
                merged[station["id"]] = reading
                update_latest_conditions(reading, source=f"station:{station['id']}", station_id=station["id"])
                log.info(f"[current] {station['id']} — DBT={reading.get('DBT')}°C  RH={reading.get('RH')}%")

    # Fallback to data.gov.sg for missing values or if no stations responded
    if not merged:
        log.info("[current] No stations responded — falling back to data.gov.sg")
        fb_station = config.get("api_fallback", {}).get("preferred_station_id", FALLBACK_STATION)
        reading = fetch_datagov_current(fb_station)
        if reading:
            update_latest_conditions(reading, source="data.gov.sg", station_id=fb_station)
            merged["api_fallback"] = reading

    return merged


def fetch_month(month: str, config: dict, source: str = "auto",
                station_filter: str = "all") -> pd.DataFrame | None:
    """
    Fetch a full month of hourly data.
    source: "station" | "api" | "auto" (station first, API fallback)
    Returns a validated hourly DataFrame or None if no data available.
    """
    df = None
    used_source = None

    # Try NUS localized API first
    if source in ("station", "auto"):
        for sid in NUS_LOCALIZED_STATIONS:
            result = fetch_nus_localized_api_month(sid, month)
            if result is not None and not result.empty:
                df = result
                used_source = f"nus_localized:{sid}"
                break

    if df is None and source in ("station", "auto"):
        stations = config.get("stations", [])
        if station_filter != "all":
            stations = [s for s in stations if s["id"] == station_filter]

        for station in stations:
            station_df = fetch_station_historical(station, month)
            if station_df is not None and not station_df.empty:
                df = station_df
                used_source = f"station:{station['id']}"
                break  # use first successful station

    if df is None and source in ("api", "auto"):
        log.info(f"[{month}] Using data.gov.sg API (source={source})")
        fb_station = config.get("api_fallback", {}).get("preferred_station_id", FALLBACK_STATION)
        df = fetch_datagov_month(month, fb_station)
        used_source = f"data.gov.sg:{fb_station}"

    if df is None or df.empty:
        log.error(f"[{month}] No data from any source — skipping archive")
        return None

    # Hard-bounds check (spike detection is in validate_weather.py)
    df = _hard_bound_check(df)

    save_to_archive(df, month, used_source)
    log.info(f"[{month}] Fetch complete — source: {used_source}  rows: {len(df)}")
    return df


# ══════════════════════════════════════════════════════════════════════════════
# Entrypoint
# ══════════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description="Nimbus — NUS weather data fetcher")
    parser.add_argument("--month",   default=None,
                        help="Month to fetch: YYYY-MM (omit for current conditions only)")
    parser.add_argument("--source",  default="auto",
                        choices=["station", "api", "auto"],
                        help="Data source: station | api | auto (default: auto)")
    parser.add_argument("--station", default="all",
                        help="Station ID filter, or 'all' (default: all)")
    args = parser.parse_args()

    config = load_station_config()

    if args.month:
        df = fetch_month(args.month, config, source=args.source, station_filter=args.station)
        if df is not None:
            coverage = df.notna().mean().mean() * 100
            print(f"\n✅ {args.month} archived — {len(df)} hourly rows  coverage={coverage:.1f}%")
            print(df.describe().to_string())
        else:
            print(f"\n❌ No data fetched for {args.month}")
            sys.exit(1)
    else:
        readings = fetch_current(config, station_filter=args.station)
        if readings:
            print(f"\n🌦️ Current conditions ({len(readings)} station(s)):")
            for sid, r in readings.items():
                print(f"  {sid}: DBT={r.get('DBT')}°C  RH={r.get('RH')}%  "
                      f"WS={r.get('WS')}m/s  GHI={r.get('GHI','—')}W/m²")
        else:
            print("\n❌ No current conditions available")
            sys.exit(1)


if __name__ == "__main__":
    main()
