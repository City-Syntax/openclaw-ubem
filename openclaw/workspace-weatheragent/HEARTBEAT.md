# HEARTBEAT.md — WeatherAgent (Nimbus)

WeatherAgent runs on a **10-minute polling heartbeat** for live station data,
plus a **daily 07:00 SGT** data quality and archive check.

---

## 10-minute heartbeat (live conditions)

1. Poll all active stations in `station_config.json`
2. If any station is unreachable → fall back to data.gov.sg API for that station's variables
3. Write `observed/latest_conditions.json` with current DBT, RH, GHI, WS, WD, Precip
4. If DBT > 36°C or DBT < 22°C → alert Signal (Slack `#openclaw-alerts`): unusual temperature reading
5. If any station offline >30 minutes → alert Signal once (suppress repeat until recovered)

**Reply `HEARTBEAT_OK`** if all stations are live and readings are within normal bounds.

---

## Daily 07:00 SGT — data quality and archive check

1. Confirm yesterday's hourly data is fully archived in `observed/{YYYY-MM}.parquet`
2. Run `validate_weather.py --month <current-month>` — check for growing gaps or sensor drift
3. Check if a calibrated EPW exists for the current simulation month in `calibrated/`
   - If not → build one and notify Forge: "🌦️ Calibrated EPW ready for {YYYY-MM}"
4. Cross-check current month's mean DBT against MSS monthly normal (±2°C tolerance)
   - If outside tolerance → flag `"data_quality": "drift_alert"` in `latest_conditions.json`
   - Notify Signal: "⚠️ DBT drift detected — {month} mean {obs:.1f}°C vs MSS normal {norm:.1f}°C"
5. Report to Orchestrator:
   - Stations online/offline
   - Current month coverage %
   - Whether a calibrated EPW is available for the next simulation run

---

## Monthly (1st of month, 08:00 SGT)

1. Finalise last month's observed archive (close the parquet file)
2. Build and save the site-calibrated EPW for last month
3. Notify Forge: calibrated EPW path is available for re-simulation if needed
4. Update `calibrated/manifest.json` with coverage and quality summary

---

## Silence rules

- **22:00–07:00 SGT:** only alert for station-down events — no routine reports
- Suppress repeated station-offline alerts; send one alert, then one recovery notice
- Never alert for missing GHI data at night (hours 20:00–06:00)

---

`HEARTBEAT_OK` if:
- All stations live and within bounds
- No new quality flags since last check
- Calibrated EPW for current month already built
