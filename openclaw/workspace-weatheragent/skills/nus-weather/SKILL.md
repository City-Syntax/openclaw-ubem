---
name: nus-weather
description: Fetch live and historical weather data from NUS onsite stations and the data.gov.sg API, validate sensor readings, and build site-calibrated EPW files for EnergyPlus simulations. Use when asked for current conditions, observed weather data for a past month, a calibrated EPW for Forge, or weather context for anomaly diagnosis.
metadata: {"openclaw": {"emoji": "🌦️", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Weather Skill

## Trigger phrases

"current weather on campus", "fetch weather for August", "build calibrated EPW",
"what's the temperature now", "weather data for FOE6 simulation", "site EPW for 2024",
"station data", "is the weather station online", "weather context for anomaly",
"observed vs TMY", "rainfall this month", "DBT today"

---

## Data sources (priority order)

### Trusted calibration period (NUS localized API)

Empirically tested API behaviour:
- 2023-10 and 2024-01: MET_E1A returns 500s → pipeline falls back to data.gov.sg S121.
- 2026-04 (FOE1 run): MET_E1A 500 + gaps in data.gov.sg.
- **2025-01 to 2025-11**: MET_E1A returns clean data for all 11 months.
  - Source: `nus_localized:MET_E1A` for every month.
  - Rows: 720–744 hourly rows.
  - DBT coverage: typically ~99–100%.
- 2025-12: still `nus_localized:MET_E1A` but DBT coverage ~57% (overall ~52%).

**Workflow guidance:**
- Treat **2025-01 → 2025-11** as the primary trusted window for calibration,
  benchmarking, and backtesting.
- Use 2025-12 only with a `⚠ lower-quality weather` flag.
- For other years, prefer months where `validate_weather.py` reports
  `data_quality = "good"` before using them for calibration.


| Priority | Source | What it provides | When used |
|---|---|---|---|
| 1 | **NUS localized API (MET_E1A)** | DBT, RH, GHI, WS, WD (hourly, on-campus) | Default — always tried first |
| 2 | **NUS onsite stations (REST/CSV)** | DBT, RH, GHI, WS, WD, Precip (10-min) | Localized API unavailable |
| 3 | **data.gov.sg API** | DBT, RH, WS, WD, Precip (5-min, Clementi S121) | Both station sources offline or data gap |
| 4 | **Base TMY EPW (IWEC)** | All variables (synthetic) | No observed data available |

---

## Pipeline position

```
Orchestrator
    │
    ▼
Nimbus (WeatherAgent)          ← Phase 1, T1 (before simulation)
    │
    ├── observed/{YYYY-MM}.parquet    (raw hourly archive)
    ├── latest_conditions.json        (current reading — polled by Radar, Compass)
    └── calibrated/{YYYY-MM}_site_calibrated.epw
                │
                ▼
            Forge (SimulationAgent)   ← uses calibrated EPW via --epw flag
```

Radar (AnomalyAgent) also calls Nimbus directly to get observed weather deltas when diagnosing consumption anomalies.

---

## Workflows

All commands run from `SKILL_DIR`:
```bash
cd /Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather
```

### Fetch current conditions (heartbeat)

```bash
python scripts/fetch_weather.py --source station --station all
```

Updates `observed/latest_conditions.json`. Falls back to data.gov.sg if stations are offline.

### Fetch a full month's historical data

```bash
# Auto: try onsite station first, fall back to data.gov.sg
python scripts/fetch_weather.py --month 2024-08 --source auto

# Force API-only
python scripts/fetch_weather.py --month 2024-08 --source api

# Specific station
python scripts/fetch_weather.py --month 2024-08 --source station --station NUS_KR_01
```

Output: `weather/observed/2024-08.parquet`

### Validate a month (spike detection + gap filling)

```bash
python scripts/validate_weather.py --month 2024-08

# Fail with non-zero exit if quality is "poor" (use in CI / strict pipeline)
python scripts/validate_weather.py --month 2024-08 --strict
```

Outputs:
- `weather/observed/2024-08_validated.parquet` — cleaned hourly data
- `weather/observed/2024-08_quality_report.json` — coverage %, flags, spike log

### Build a site-calibrated EPW

```bash
# Single month
python scripts/build_epw.py --month 2024-08 \
    --base-epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw \
    --out /Users/ye/nus-energy/weather/calibrated/2024-08_site_calibrated.epw

# Full year (patches all 12 months; retains TMY for any "poor" months)
python scripts/build_epw.py --year 2024 \
    --base-epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw \
    --out /Users/ye/nus-energy/weather/calibrated/2024_site_calibrated.epw
```

Outputs:
- `weather/calibrated/2024_site_calibrated.epw` — patched EPW
- `weather/calibrated/2024_site_calibrated.json` — manifest with per-month patch status

### Full pipeline: fetch → validate → build EPW

```bash
MONTH=2024-08
python scripts/fetch_weather.py --month $MONTH --source auto
python scripts/validate_weather.py --month $MONTH
python scripts/build_epw.py --month $MONTH \
    --base-epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw \
    --out /Users/ye/nus-energy/weather/calibrated/${MONTH}_site_calibrated.epw
```

Then pass the calibrated EPW to Forge:
```bash
python simulate.py --idf /Users/ye/nus-energy/idfs/FOE13.idf \
    --epw /Users/ye/nus-energy/weather/calibrated/2024-08_site_calibrated.epw
```

---

## What the scripts produce

### fetch_weather.py

- `weather/observed/{YYYY-MM}.parquet` — raw hourly DataFrame with columns: `DBT`, `RH`, `GHI`, `WS`, `WD`, `Precip`
- `weather/observed/latest_conditions.json` — current snapshot for downstream agents

### validate_weather.py

- `weather/observed/{YYYY-MM}_validated.parquet` — cleaned data (spikes replaced, gaps filled ≤3h)
- `weather/observed/{YYYY-MM}_quality_report.json`:

```json
{
  "month": "2024-08",
  "expected_hours": 744,
  "actual_rows": 744,
  "data_quality": "good",
  "variables": {
    "DBT": { "valid_hours": 739, "coverage_pct": 99.3, "obs_mean_c": 28.1, "mss_normal_c": 28.1, "delta_c": 0.0 },
    "RH":  { "valid_hours": 741, "coverage_pct": 99.6 },
    "GHI": { "valid_hours": 510, "coverage_pct": 68.5 },
    "WS":  { "valid_hours": 744, "coverage_pct": 100.0 },
    "WD":  { "valid_hours": 744, "coverage_pct": 100.0 },
    "Precip": { "valid_hours": 744, "coverage_pct": 100.0 }
  },
  "spikes_removed": { "DBT": 2, "RH": 0 },
  "gaps_filled":    { "DBT": { "filled": 5, "remaining_gaps": 0 } },
  "flags": [ "GHI coverage 68.5% < 80% threshold" ]
}
```

`data_quality` values: `"good"` | `"drift_alert"` | `"poor"` | `"missing"`

### build_epw.py

- `weather/calibrated/{out}.epw` — site-calibrated EPW
- `weather/calibrated/{out}.json` — patch manifest:

```json
{
  "base_epw": "...SGP_Singapore.486980_IWEC.epw",
  "months": {
    "2024-08": {
      "status": "patched",
      "data_quality": "good",
      "patched_vars": ["DBT", "RH", "DPT", "WS", "WD"],
      "tmy_retained": ["GHI", "DNI", "DHI"],
      "flags": ["GHI coverage 68.5% < 80% threshold"]
    }
  }
}
```

---

## EPW patching rules

| Variable | Patched when | Method |
|---|---|---|
| DBT | observed coverage ≥ 80% | Direct overwrite |
| RH | observed coverage ≥ 80% | Direct overwrite |
| DPT | RH patched | Derived via Magnus formula from DBT + RH |
| GHI / DNI / DHI | GHI coverage ≥ 80% | GHI direct; DNI+DHI via Erbs decomposition model |
| WS | observed coverage ≥ 80% | Direct overwrite |
| WD | observed coverage ≥ 80% | Direct overwrite |
| Precip | observed coverage ≥ 80% | Direct overwrite |
| Any variable | coverage < 80% | **TMY value retained — not patched** |
| Any month | `data_quality = "poor"` | **Entire month uses TMY — not patched** |

---

## Data quality thresholds

| Parameter | Threshold | Action |
|---|---|---|
| Spike detection | >4σ from 24h rolling mean | Replace with NaN, log event |
| Gap filling | ≤3 consecutive missing hours | Linear interpolation |
| Gap filling | >3 consecutive hours | Left as NaN (counts against coverage) |
| Monthly coverage | <80% valid hours per variable | Flag variable; retain TMY for that variable |
| Monthly coverage | <80% across all vars | `data_quality = "poor"` — entire month uses TMY |
| DBT drift | >±2°C vs MSS monthly normal | `data_quality = "drift_alert"`, alert Signal |

---

## Output format to report back

After fetching + building a calibrated EPW, report:

```
🌦️ {YYYY-MM} weather — Nimbus

Source:    station:NUS_KR_01 (primary)
Coverage:  DBT 99.3% | RH 99.6% | GHI 68.5% | WS 100% | WD 100% | Precip 100%
Quality:   good

EPW patch:
  ✅ DBT patched   — mean 28.1°C (MSS normal 28.1°C, Δ=0.0°C)
  ✅ RH patched    — mean 81.4%
  ✅ WS/WD patched — mean 2.1 m/s
  ⬜ GHI/DNI/DHI  — TMY retained (coverage 68.5% < 80%)

⚠ Flags:
  GHI coverage 68.5% < 80% threshold — solar irradiance not patched

Calibrated EPW → weather/calibrated/2024-08_site_calibrated.epw
Manifest      → weather/calibrated/2024-08_site_calibrated.json
```

---

## Integration with Forge (SimulationAgent)

When Nimbus builds a calibrated EPW, it notifies the Orchestrator:

```json
{
  "agent": "Nimbus",
  "event": "calibrated_epw_ready",
  "month": "2024-08",
  "epw_path": "/Users/ye/nus-energy/weather/calibrated/2024-08_site_calibrated.epw",
  "overall_quality": "good",
  "patched_months": ["2024-08"],
  "tmy_months": []
}
```

Forge passes the path via `--epw` to every simulation run for that month.
If no calibrated EPW exists, Forge falls back to the base TMY with a Slack warning.

---

## Integration with Radar (AnomalyAgent)

Radar calls Nimbus to get weather context when flagging anomalies:

```python
# Radar reads latest_conditions.json directly
import json
conditions = json.load(open("/Users/ye/nus-energy/weather/observed/latest_conditions.json"))
# {"timestamp": "...", "source": "station:NUS_KR_01",
#  "conditions": {"DBT": 31.2, "RH": 88.4, "GHI": 320.5, "WS": 1.8, ...}}
```

Radar also reads the quality report for the month under investigation to flag whether
observed DBT was unusually high (explaining elevated cooling consumption).

---

## Error handling

| Condition | Response |
|---|---|
| Station unreachable | Log warning, fall back to data.gov.sg, note source in output |
| data.gov.sg API down | Fall back to TMY EPW, flag `"weather_source": "TMY_fallback"` |
| >20% hours missing in a month | `data_quality = "poor"` — TMY retained, Forge notified |
| Sensor spike (>4σ) | Replace with NaN, interpolate, log substitution |
| DBT drift >±2°C vs MSS normal | `drift_alert` flag, Signal notified |
| Corrupted EPW output | Abort write, never overwrite base TMY, report to Orchestrator |
| `station_config.json` missing | Warn and proceed with API-only mode |

---

## Station setup (first run)

1. Create `$NUS_PROJECT_DIR/weather/station_config.json` using the schema in `TOOLS.md`
2. Add API keys to `/Users/ye/.openclaw/workspace-weatheragent/.env`
3. Test station connectivity:
   ```bash
   python scripts/fetch_weather.py --source station --station NUS_KR_01
   ```
4. If station unreachable, verify IP, credentials, and network route
5. Run first month fetch: `python scripts/fetch_weather.py --month 2024-08 --source auto`
