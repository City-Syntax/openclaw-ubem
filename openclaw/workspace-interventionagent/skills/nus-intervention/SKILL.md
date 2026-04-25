---
name: nus-intervention
description: Carbon scenario engine for NUS buildings. Reads a building's parsed monthly CSV and IDF file, extracts an energy fingerprint, scores 7 interventions, assembles ranked reduction scenarios, and estimates Singapore-market retrofit costs (SGD) for shallow / medium / deep tiers. Use when asked for carbon scenarios, reduction potential, intervention analysis, cost estimates, payback periods, or net-zero pathways for any NUS building.
metadata: {"openclaw": {"emoji": "🌿", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Carbon Skill

## Trigger phrases

"carbon scenarios for BIZ8", "reduction potential for FOE13", "what interventions for CLB6",
"run carbon analysis", "net zero pathway", "how much can we reduce", "intervention ranking",
"cool paint impact", "PV sizing for BIZ8", "carbon for all buildings",
"how much will it cost", "retrofit budget for FOE6", "capex estimate", "payback period",
"SGD cost", "cost per tonne", "abatement cost", "grant eligible"

---

## Workflow rules (updated 2026-04-12)
- Use **simulation by default** wherever the intervention is patchable in the IDF.
- Use **estimation only when simulation is not feasible**.
- Always label each intervention/scenario source explicitly.
- Reported baseline EUI must use **total EUI including cooling electrical equivalent**.

## Intervention sources

Intervention savings come from two sources, selected automatically:

| Source | How | When used |
|---|---|---|
| `simulated` | Counterfactual EnergyPlus run on patched IDF | Default (all patchable interventions) |
| `estimated` | Literature-range midpoints from INTERVENTION_REDUCTIONS | Fallback if sim fails; IV5 always |

**Simulated by default** (counterfactual IDF patch — patched copies written to `intervention_idfs/`, source IDF never modified):
- Intervention 1 — Cooling setpoint +1 °C (temporary patch for sim; never persisted)
- Intervention 2 — Lighting upgrade (LPD → 5.5 W/m²)
- Intervention 3 — Dimming controls (LPD × 0.70 proxy)
- Intervention 4 — Shading (SHGC × 0.60 proxy)
- Intervention 6 — PV installation (inject Generator:Photovoltaic block)
- Intervention 7 — Cool painting (solar absorptance → 0.25 roof / 0.40 wall)

**Always estimated (not simulatable):**
- Intervention 5 — HVAC system upgrade: IdealLoads → VRF/chiller requires full model rebuild, not a param patch.

**Setpoint rule:** IV1 patches a *temporary* counterfactual IDF for simulation only. The source IDF setpoint is never changed. Recommendation to facilities team: raise by +1 °C from current value (capped — no larger jumps).

Each intervention in the JSON output carries a `"source": "simulated" | "estimated"` field.
Each scenario carries a `"sources"` list showing which sources were used.

---

## Retrofit scenario tiers

| Tier | Interventions | Capex level | Deployment |
|---|---|---|---|
| 🟢 **Shallow** | IV1 (setpoint only) | **Zero** — BAS edit only, no procurement, no works | Immediate |
| 🟡 **Medium** | IV1, IV2 (LED), IV4 (shading), IV7 (cool paint) | **Low–medium** — operational upgrades | 6–18 months |
| 🔴 **Deep** | IV1–IV7 (all 7 interventions) | **High** — major infrastructure programme | Phased 2–5 years |

---

## Singapore cost model

All intervention costs are expressed as **SGD/m² GFA** so that scenarios report a single comparable cost-intensity figure alongside tCO2e saved. Non-GFA reference rates (per zone, per window, per kWp) are converted using building-type ratios documented below.

| IV | Intervention | SGD/m² GFA (low–mid–high) | Reference rate | Conversion basis | Typical payback |
|---|---|---|---|---|---|
| 1 | Cooling setpoint increase | **$0/m²** | SGD 0 lump sum | In-house BAS edit, no external capex | Immediate |
| 2 | Lighting upgrade (LED) | **$25–$35–$45/m²** | SGD 35/m² GFA | Direct — BCA GBIC benchmark | 3–5 yr |
| 3 | Dimming control system | **$45–$60–$78/m²** | SGD 4,500/zone | ÷ 75 m² GFA/zone (NUS institutional avg) | 4–7 yr |
| 4 | Shading upgrades | **$45–$60–$78/m²** | SGD 480/window | ÷ 8 m² GFA/window (glazing ratio ~0.40) | 6–10 yr |
| 5 | HVAC system upgrade | **$200–$280–$360/m²** | SGD 280/m² GFA | Direct — BCA BCIS Singapore 2024 | 8–15 yr |
| 6 | PV installation | **$40–$50–$60/m²** | SGD 1,350/kWp | × 0.150 kWp/m² × 0.70 coverage × 0.35 roof/GFA | 7–10 yr |
| 7 | Cool painting | **$3–$4–$6/m²** | SGD 8/m² painted | × 0.50 area ratio (roof 30% + wall 20% of GFA) | 2–5 yr |

**Scenario cost intensities (SGD/m² GFA, mid-point sum of applied interventions):**

| Tier | Interventions | SGD/m² GFA |
|---|---|---|
| 🟢 Shallow | IV1 only | **$0/m²** |
| 🟡 Medium | IV1 + IV2 + IV4 + IV7 | **~$99/m²** |
| 🔴 Deep | IV1–IV7 (all) | **~$489/m²** |

**Cost basis notes:**
- All costs SGD, inclusive of supply, installation, commissioning, and one-year DLP where applicable.
- Low/high bounds are ±20–30% of mid-point; both per-m² GFA and total SGD ranges reported in JSON output.
- Electricity tariff assumption: **SGD 0.32/kWh** (SP Group commercial 2024) for payback calculations.

**Applicable Singapore grants (may reduce net capex 30–50%):**
- **NEA ENER+** — LED, DALI dimming, BAS upgrades
- **EDB Energy Efficiency Fund (E2F)** — HVAC, chiller plant (public sector / SME)
- **NEA SolarNova / EDB** — PV installations on NUS campus
- **BCA Green Mark Incentive Scheme** — whole-building retrofits achieving Green Mark uplift

**Cost per tonne of CO₂ (SGD/tCO2e):** reported as `cost_per_tco2e_sgd`, computed as annualised capex (÷15-year asset life) ÷ annual CO₂ saved.

---

## Steps

### Single building — with counterfactual simulations (default, accurate)

```bash
python3 /Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py \
  --building {BUILDING} \
  --outputs $NUS_PROJECT_DIR/outputs \
  --idfs $NUS_PROJECT_DIR/idfs \
  --epw /Users/ye/nus-energy/weather/SGP_Singapore.486980_IWEC.epw
```

> Patched IDFs and counterfactual run outputs are written to `/Users/ye/nus-energy/intervention_idfs/{BUILDING}/iv{N}/`.
> Original IDFs in `idfs/` are never modified.
> Override the directory with `--intervention-idfs /path/to/dir` if needed.
> This is the **default and preferred mode** for workflow runs.

### Single building — estimates only (fallback only, less accurate)

```bash
python3 /Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py \
  --building {BUILDING} \
  --outputs $NUS_PROJECT_DIR/outputs \
  --idfs $NUS_PROJECT_DIR/idfs \
  --no-simulate
```

### All buildings (simulated, portfolio-level accuracy)

```bash
cd $NUS_PROJECT_DIR
for b in $(ls outputs/); do
  parsed="outputs/$b/parsed/${b}_monthly.csv"
  if [ -f "$parsed" ]; then
    python3 /Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py \
      --building $b --outputs outputs --idfs idfs \
      --epw weather/SGP_Singapore.486980_IWEC.epw
  fi
done
```

### All buildings — estimates only (fast, less accurate)

```bash
cd $NUS_PROJECT_DIR
for b in $(ls outputs/); do
  parsed="outputs/$b/parsed/${b}_monthly.csv"
  if [ -f "$parsed" ]; then
    python3 /Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py \
      --building $b --outputs outputs --idfs idfs --no-simulate
  fi
done
```

### With scenario mode filter

```bash
python3 /Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py \
  --building {BUILDING} \
  --outputs $NUS_PROJECT_DIR/outputs \
  --idfs $NUS_PROJECT_DIR/idfs \
  --mode shallow        # options: shallow | medium | deep
```

### With custom intervention IDFs directory

```bash
python3 /Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py \
  --building {BUILDING} \
  --outputs $NUS_PROJECT_DIR/outputs \
  --idfs $NUS_PROJECT_DIR/idfs \
  --intervention-idfs /Users/ye/nus-energy/intervention_idfs
```

---

## What `carbon_scenarios.py` produces

- `outputs/{BUILDING}/carbon/{BUILDING}_carbon_scenarios.json` — full scenario output including per-intervention capex and scenario-level cost rollup
- Console summary (see output format below)

**New fields in JSON output (per intervention):**
```json
"capex_sgd": {
  "cost_per_m2_gfa":      35,
  "cost_per_m2_gfa_low":  25,
  "cost_per_m2_gfa_high": 45,
  "capex_sgd":            420000,
  "capex_sgd_low":        300000,
  "capex_sgd_high":       540000,
  "floor_area_m2":        12000,
  "unit_desc":            "SGD/m² GFA (LED panel supply & install, incl. driver & labour)",
  "conversion_note":      "Direct GFA rate from BCA GBIC benchmarks — no conversion needed.",
  "ref_unit_cost":        35,
  "ref_unit":             "SGD/m² GFA",
  "payback_years":        "3–5",
  "data_quality":         "floor_area_known"
},
"simple_payback_years": 4.6
```

**New fields in JSON output (per scenario):**
```json
"capex_summary": {
  "total_sgd":            1188000,
  "total_sgd_low":        900000,
  "total_sgd_high":       1476000,
  "cost_per_m2_gfa":      99,
  "cost_per_m2_gfa_low":  73,
  "cost_per_m2_gfa_high": 123,
  "label":                "SGD 900k – 1.5M",
  "cost_per_tco2e_sgd":   420,
  "annualisation_years":  15,
  "currency":             "SGD",
  "tariff_assumption":    "SGD 0.32/kWh (SP commercial 2024)"
}
```

---

## Output format to report back

After running, show this summary:

```
🧭 {BUILDING} — carbon scenario analysis

Baseline: {annual_tco2e:.1f} tCO2e/year  |  EUI: {eui:.0f} kWh/m²  |  BCA: {bca_rating}

IDF fingerprint:
  Cooling SP:  {cooling_sp} °C (all zones)
  LPD:         {lpd} W/m²
  Solar abs.:  {solar_abs} (roof + walls)
  HVAC:        {hvac_type}
  PV:          {pv_status}

Scenario           Reduction   CO2 saved    SGD/m² GFA    Capex (SGD)              SGD/tCO2e   Sources
──────────────────────────────────────────────────────────────────────────────────────────────────
🟢 Shallow         {pct:.0f}%  {saved:.1f} tCO2e   $0/m²        SGD 0 (zero capex)           —       simulated+estimated
🟡 Medium          {pct:.0f}%  {saved:.1f} tCO2e   $73–$123/m²  SGD {low}k – {high}k      ${cpt}    simulated+estimated
🔴 Deep            {pct:.0f}%  {saved:.1f} tCO2e   $369–$617/m² SGD {low}M – {high}M      ${cpt}    estimated

  Cost basis: Singapore market rates (BCA BCIS / NEA ENER+ 2024, SGD).
  Grants (NEA, EDB E2F, BCA Green Mark Incentive) may reduce net capex by 30–50%.
  SGD/tCO2e = annualised capex (÷15 yr) ÷ annual CO2 saved.

⚠ Flags:
  {flags}

Full results → outputs/{BUILDING}/carbon/{BUILDING}_carbon_scenarios.json
```

---

## Error handling

| Condition | Response |
|---|---|
| No parsed CSV found | "No parsed data for {BUILDING}. Run `nus-parse` first." |
| No IDF found | "No IDF found for {BUILDING}. Check `$NUS_PROJECT_DIR/idfs/`." |
| IDF is IdealLoads | Add flag: "HVAC is IdealLoadsAirSystem — Intervention 5 saving estimated, not simulated." |
| VT < 0.3 | Add flag: "Glazing VT={vt} limits dimming (Intervention 3) to perimeter zones only." |
| No floor area in registry | EUI shown as N/A; capex shown as minimum-cost placeholder with `"data_quality": "floor_area_estimated"` — flag: "Floor area unknown — capex estimates use minimum cost placeholders." |
| Building not calibrated (CVRMSE > 15% or `engineer_review_required`) | Proceed normally; add flag: "⚠ Uncalibrated baseline — treat savings and costs as indicative" |
| `capex_sgd = 0` for non-zero-cost intervention | Indicates floor area and zone/window counts all missing — flag for manual review |

---

## After running all buildings

Chain to `nus-report` to generate a portfolio-level carbon ranking table across all analysed buildings, including a cost-ranked abatement curve (SGD/tCO2e vs cumulative CO2 saved).
Notify Signal 📣 if any building has > 40% reduction potential in the `shallow` or `medium` scenario.
