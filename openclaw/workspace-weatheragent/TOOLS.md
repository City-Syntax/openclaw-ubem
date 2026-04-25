# TOOLS.md — WeatherAgent (Nimbus)

## Project

| Item | Value |
|---|---|
| NUS_PROJECT_DIR | `/Users/ye/nus-energy` |
| Weather root | `$NUS_PROJECT_DIR/weather/` |
| Workspace | `/Users/ye/.openclaw/workspace-weatheragent/` |

---

## Data Sources

### 1. NUS Onsite Weather Stations

Station configuration lives in `$NUS_PROJECT_DIR/weather/station_config.json`.
See the **Station Config** section below for the schema.

Stations communicate via one of:
- **REST/JSON API** — most modern stations (HTTP GET, returns JSON)
- **CSV push** — legacy stations write to a shared network path

Polling interval: **hourly** (or on-demand for latest conditions)

### 2. data.gov.sg Realtime Weather API (fallback)

Base URL: `https://api.data.gov.sg/v1/environment/`

```bash
# Air temperature — nearest Clementi / Queenstown station
curl "https://api.data.gov.sg/v1/environment/air-temperature?date=2024-08-01"

# Relative humidity
curl "https://api.data.gov.sg/v1/environment/relative-humidity?date=2024-08-01"

# Wind speed
curl "https://api.data.gov.sg/v1/environment/wind-speed?date=2024-08-01"

# Wind direction
curl "https://api.data.gov.sg/v1/environment/wind-direction?date=2024-08-01"

# Rainfall
curl "https://api.data.gov.sg/v1/environment/rainfall?date=2024-08-01"
```

Response format: `{ "items": [{ "timestamp": "...", "readings": [{ "station_id": "...", "value": ... }] }] }`

Closest stations to NUS Kent Ridge: `S121` (Clementi), `S116` (Lower Pierce), `S107` (East Coast)
Use `S121` (Clementi) as the primary API fallback — closest to campus.

### 3. MSS Historical Normals (validation reference)

- Monthly climatological normals: `http://www.weather.gov.sg/climate-historical-daily/`
- Useful for cross-checking: mean DBT should be 26.5–28.5°C for all months

---

## Python Dependencies

```bash
pip install requests pandas numpy pyarrow python-dotenv eppy --break-system-packages
```

| Package | Purpose |
|---|---|
| `requests` | HTTP calls to station REST APIs and data.gov.sg |
| `pandas` / `numpy` | Timeseries processing and gap-filling |
| `pyarrow` | Parquet archive format for observed data |
| `eppy` | EPW file read/write (reuses Forge's existing install) |
| `python-dotenv` | Load `WEATHER_STATION_API_KEY` from `.env` |

---

## Scripts

| Script | Purpose | Key args |
|---|---|---|
| `fetch_weather.py` | Pull hourly data from stations or API | `--month YYYY-MM`, `--station all\|<id>`, `--source station\|api\|auto` |
| `validate_weather.py` | Spike detection, gap filling, quality flags | `--month YYYY-MM`, `--strict` |
| `build_epw.py` | Patch base TMY EPW with observed data | `--month YYYY-MM`, `--base-epw <path>`, `--out <path>` |
| `serve_conditions.py` | Write `latest_conditions.json` (called by heartbeat) | `--station all` |

---

## Station Config Schema

`$NUS_PROJECT_DIR/weather/station_config.json`:

```json
{
  "stations": [
    {
      "id": "NUS_KR_01",
      "name": "Kent Ridge Main Weather Mast",
      "location": "Kent Ridge Campus — near E1 engineering block",
      "lat": 1.2966,
      "lon": 103.7764,
      "type": "rest",
      "url": "http://<station-ip>/api/v1/latest",
      "api_key_env": "NUS_KR_01_API_KEY",
      "sensors": ["DBT", "RH", "GHI", "WS", "WD", "Precip"],
      "poll_interval_min": 10,
      "fallback_station_id": "S121"
    },
    {
      "id": "NUS_BT_01",
      "name": "Bukit Timah Campus Station",
      "location": "Bukit Timah Campus — near law faculty",
      "lat": 1.3204,
      "lon": 103.8153,
      "type": "csv_push",
      "csv_path": "/mnt/nusweather/bt_campus/latest.csv",
      "sensors": ["DBT", "RH", "WS", "WD"],
      "poll_interval_min": 60,
      "fallback_station_id": "S121"
    }
  ],
  "api_fallback": {
    "provider": "data.gov.sg",
    "preferred_station_id": "S121",
    "base_url": "https://api.data.gov.sg/v1/environment/"
  }
}
```

---

## EPW File Format Reference

EPW is a fixed-column text format. Each data row = one hour (8760 rows total for a full year).

Key columns (1-indexed, comma-delimited):

| Col | Field | Unit | Notes |
|---|---|---|---|
| 7 | Dry Bulb Temperature | °C | DBT — primary calibration target |
| 8 | Dew Point Temperature | °C | Derived from RH if not directly measured |
| 9 | Relative Humidity | % | RH |
| 14 | Global Horizontal Irradiance | Wh/m² | GHI |
| 15 | Direct Normal Irradiance | Wh/m² | DNI |
| 16 | Diffuse Horizontal Irradiance | Wh/m² | DHI |
| 21 | Wind Direction | degrees | 0=N, 90=E, 180=S, 270=W |
| 22 | Wind Speed | m/s | WS |

Patching strategy: overwrite only columns with observed sensor coverage ≥80%.
Columns without sufficient coverage retain the base TMY values.

```python
# Quick EPW read with eppy
from eppy.weather import EPW
epw = EPW()
epw.read("/Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw")
# epw.epwfile.dataframe has all 8760 rows
```

---

## Output Files

| File | Format | Contents |
|---|---|---|
| `observed/{YYYY-MM}.parquet` | Parquet | Raw hourly readings, all sensors, for one month |
| `observed/latest_conditions.json` | JSON | Current readings from all active stations |
| `calibrated/{YYYY-MM}_site_calibrated.epw` | EPW | Base TMY patched with observed data |
| `calibrated/{YYYY-MM}_patch_report.json` | JSON | Coverage %, delta vs TMY, quality flags per column |

---

## Environment Variables

Store in `/Users/ye/.openclaw/workspace-weatheragent/.env`:

```
NUS_KR_01_API_KEY=<station-api-key>
NUS_BT_01_API_KEY=<station-api-key>
DATA_GOV_SG_API_KEY=<optional-if-needed>
```

Load in scripts with:
```python
from dotenv import load_dotenv
load_dotenv("/Users/ye/.openclaw/workspace-weatheragent/.env")
```

---

## Common Commands

```bash
# Fetch current conditions from all stations
python scripts/fetch_weather.py --source station --station all

# Fetch last month's data and validate
python scripts/fetch_weather.py --month 2024-08 --source auto
python scripts/validate_weather.py --month 2024-08

# Build a calibrated EPW for August 2024
python scripts/build_epw.py --month 2024-08 \
  --base-epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw \
  --out /Users/ye/nus-energy/weather/calibrated/2024-08_site_calibrated.epw

# Build a full-year calibrated EPW (patches all available months)
python scripts/build_epw.py --year 2024 \
  --base-epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw \
  --out /Users/ye/nus-energy/weather/calibrated/2024_site_calibrated.epw
```
