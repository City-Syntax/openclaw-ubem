# WORKFLOW.md — NUS Energy Management Pipeline

_Last updated: 2026-03-30_

---

## Overview

Seven-phase pipeline for building energy model calibration and decarbonisation planning across NUS campus.

---

## Phase 1 — Input

Data sources fed into the pipeline at the start:

| Input | Description |
|---|---|
| Ground truth energy CSV | Real metered monthly energy consumption per building |
| Singapore weather (EPW) | EnergyPlus weather file — site-calibrated by Nimbus if available, else base TMY (IWEC) |
| Building context registry JSON | Metadata: archetype, area, occupancy, etc. |
| Building IDF files (e.g. `CLB6.idf`) | EnergyPlus energy model definitions |
| Campus spatial topology shapefile | Campus building geometry and spatial relationships |

### Phase 1a — Weather (Nimbus 🌦️, T1, parallel to registry check)

Before simulation runs, Nimbus produces a site-calibrated EPW for the target month:

1. **Fetch** hourly observations — priority order: (1) NUS localized station API (`MET_E1A`); (2) NUS onsite weather stations (REST/CSV-push); (3) data.gov.sg API (Clementi S121) if all station sources are unreachable
2. **Validate** sensor data — spike detection (>4σ rolling), gap-fill (≤3h linear interpolation), flag months with <80% coverage as `"poor"`
3. **Build calibrated EPW** — patch base TMY (IWEC) columns with observed DBT, RH, GHI, WS, WD for months with quality `"good"` or `"drift_alert"`; retain TMY for any variable with coverage <80% or month quality `"poor"`
4. **Notify Orchestrator** via `calibrated_epw_ready` event with path + quality summary
5. **Forge uses the calibrated EPW** via `--epw` flag; if no calibrated EPW is available, falls back to base TMY with a Slack warning

Key paths:
- Observed archive: `/Users/ye/nus-energy/weather/observed/{YYYY-MM}.parquet`
- Calibrated EPW: `/Users/ye/nus-energy/weather/calibrated/{YYYY-MM}_site_calibrated.epw`
- Latest conditions: `/Users/ye/nus-energy/weather/observed/latest_conditions.json`
- Scripts: `/Users/ye/.openclaw/workspace-weatheragent/skills/nus-weather/scripts/`

Radar (AnomalyAgent) also calls Nimbus to get observed weather deltas when diagnosing consumption anomalies (reads `latest_conditions.json` and quality reports directly).

### Phase 1b — Registry Validation (Forge ⚡, pre-flight)

Before simulation runs, Forge checks each target building against `building_registry.json`:

1. **Building registry entry exists?**
   - ✅ Yes → proceed to simulation
   - ❌ No → attempt auto-population via `generate_registry.py` (extracts `floor_area_m2` from IDF Zone geometry)

2. **Auto-population result:**
   - ✅ Floor area extracted → entry written to registry, proceed to simulation
   - ⚠️ Floor area not found in IDF → warn, log `floor_area_m2: null`, proceed (EUI will be unavailable)

3. **Policy:** Warn-and-proceed (not a hard gate). Simulation runs regardless; EUI and Green Mark tier simply show as `—` if floor area is unknown. Flag in the Slack notification so facilities team can supply the value.

Script: `workspace-simulationagent/skills/nus-registry/scripts/generate_registry.py`
Registry: `/Users/ye/nus-energy/building_registry.json`

---

## Phase 2 — Simulation (Forge ⚡, T3)

- Runs EnergyPlus on IDF files at **monthly time resolution**
- Produces simulated energy consumption figures
- Outputs flow to Detection alongside the ground truth CSV

### Phase 2a — Monthly output contract (authoritative)

Forge's `simulate.py` monthly CSV is the **single source of truth** for downstream workflow steps.

**Rule:** do **not** reparse or overwrite `outputs/{BUILDING}/parsed/{BUILDING}_monthly.csv` with the older standalone `parse_eso.py` format after simulation.

The authoritative monthly CSV must preserve the rich schema produced by `simulate.py`, including when available:
- `electricity_facility_kwh`
- `lighting_kwh`
- `equipment_kwh`
- `fans_kwh`
- `pumps_kwh`
- `heat_rejection_kwh`
- `exterior_lights_kwh`
- `cooling_elec_kwh`
- `cooling_thermal_kwh`
- `cooling_elec_adj_kwh`
- `other_electricity_kwh`
- `carbon_tco2e`
- `eui_kwh_m2`
- `eui_adj_kwh_m2`
- annual summary row (`month_name = ANNUAL`)

**District cooling policy:**
- `cooling_thermal_kwh` stores chilled water thermal energy from `DistrictCooling:Facility`
- `cooling_elec_adj_kwh = cooling_thermal_kwh / COP` is used for adjusted EUI and BCA-comparable reporting
- monthly report visualisations for cooling must use `cooling_elec_adj_kwh` (or thermal/COP fallback), not `cooling_elec_kwh`, for district-cooled buildings

**Reporting policy:**
- Use **adjusted EUI** for intervention quantification and BCA-facing visualisations
- Use base electrical EUI only when explicitly labelled as such

---

## Phase 3 — Detection (Radar 🔍, T2)

Compares simulated vs. real energy data against two accuracy thresholds:

- **CV(RMSE) ≤ 15%**
- **NMBE within ±5%**

**Decision gate:**
- ✅ Thresholds met → model is accurate → **proceed to Report**
- ❌ Thresholds not met → model needs correction → **proceed to Diagnosis**

---

## Phase 4 — Diagnosis Loop (Lens 🩺 → Slack Approval → Chisel 🔧 → Forge ⚡)

Triggered when the model is inaccurate. Loops until calibrated or rejected.

1. **Lens 🩺 (T2)** analyses the discrepancy and generates root-cause hypotheses with specific parameter changes (max 2 per iteration)
2. **Chisel 🔧 runs in `--dry-run` mode** — produces the exact IDF diff (object name, field, old value → new value) without writing anything
3. **Signal 📣 sends a minimal approval request to `#openclaw-alerts`** (building name + iteration number only — no diffs, no parameter details) and writes a `"pending"` entry to `/tmp/nus_pending_approvals.json` (keyed by Slack `thread_ts`) — pipeline **pauses here**
4. **Human engineer replies in the Slack thread:**
   - ✅ `"approve"` → the **Slack server** (`slack_server.py`) intercepts the reply, runs `patch_idf.py` (write mode) with the approver's Slack user ID, then re-triggers Forge → loop repeats from Phase 2
   - ❌ `"reject"` → server posts deferral confirmation, marks entry rejected → falls through to **Report**
   - No reply after 4h → watchdog posts escalation to #private; pipeline waits indefinitely

**Key constraint:** Chisel 🔧 must never write IDF changes without an explicit human `"approve"` reply in Slack (detected by the Slack server). In-conversation approval is not sufficient.

**Approval message format** (via Signal / nus-notify "Calibration Approval Request" template):
```
{building} 🔧 Recalibration needed (iteration {N})
Reply "approve" or "reject".
No reply after 4h → watchdog posts escalation to #private; pipeline waits indefinitely.
```
> Minimal by design — no parameter names, no diffs, no predicted metrics. Consent only.

---

## Phase 5 — Report (Ledger 📊, T3)

- All findings logged and archived into **`Calibration_log.md`**
- Documents root-cause hypotheses from Lens for future reference
- Runs regardless of whether the diagnosis loop succeeded or was rejected

---

## Phase 6 — Notification (Signal 📣, T2)

- Sends calibration results to stakeholders via Slack (#openclaw-alerts, #private)

**Decision gate:**
- ✅ **Decarbonisation required?** → Signal sends approval request → **pipeline pauses for human approval**
- ❌ **Not required** → workflow ends

**Decarbonisation approval flow:**
1. Signal sends to `#openclaw-alerts`: *"⚡ Should we run intervention scenarios for {BUILDING}? Reply `intervene` to simulate 🟢 Shallow → 🟡 Medium → 🔴 Deep tiers. Reply `skip` to end the pipeline here."*
2. Writes a `"pending"` entry to `/tmp/nus_pending_approvals.json` (type: `"intervention"`)
3. Human engineer replies in the Slack thread:
   - ✅ `"intervene"` → Slack server triggers Compass 🧭 scenarios + posts full results
   - ❌ `"skip"` → workflow ends, entry marked deferred
   - No reply after 4h → watchdog posts escalation to #private; pipeline waits indefinitely

---

## Phase 7 — Intervention (Compass 🧭, T2) + Oracle 🔮 (T2)

If decarbonisation action is needed:

- **Compass (T2)** generates a decarbonisation strategy for the affected buildings
  - Scenarios classified into three types:
    - 🟢 **Shallow** — low-effort, quick wins
    - 🟡 **Medium** — moderate retrofit / operational changes
    - 🔴 **Deep** — major retrofit or infrastructure changes
  - Runs on **all GT buildings regardless of calibration status**
  - If the building is uncalibrated, output carries a `⚠ Uncalibrated baseline — treat savings as indicative` warning so stakeholders know the numbers may not be precise
- **Oracle (T2)** simultaneously answers stakeholder questions and clarifies requirements in natural language via Slack

---

## Agent Trust Tiers

| Tier | Agents |
|---|---|
| T1 | Orchestrator 🎯 |
| T2 | Lens 🩺, Chisel 🔧, Oracle 🔮 |
| T3 | Nimbus 🌦️, Forge ⚡, Radar 🔍, Ledger 📊, Signal 📣, Compass 🧭 |

---

## Pipeline Flow (summary)

```
[Input: GT CSV + Registry + IDFs + Shapefile]
        |
        +--→  Nimbus 🌦️ (T1) — fetch → validate → calibrated EPW
        |              ↓ (calibrated_epw_ready, or falls back to base TMY)
        ↓
     Forge ⚡ (T3) — EnergyPlus simulation (uses calibrated EPW if available)
        ↓
     Radar 🔍 (T2) — CV(RMSE) / NMBE check
       /       \
  ✅ Pass     ❌ Fail
     ↓              ↓
  Ledger 📊    Lens 🩺 → Chisel 🔧 → Slack Approval
  (T3)              ↓               ↓
                ❌ Reject      ✅ Approve → Oracle 🔮 → Forge ⚡ (loop)
                    ↓
                Ledger 📊 (T3)
                    ↓
                Signal 📣 (T2)
               /         \
    ✅ Decarb needed    ❌ End
           ↓
    Slack Approval (human)
      /        \
 ✅ proceed  ❌ skip/End
      ↓
    Compass 🧭 (T2) + Oracle 🔮 (T2)
    [Shallow / Medium / Deep scenarios]
```
