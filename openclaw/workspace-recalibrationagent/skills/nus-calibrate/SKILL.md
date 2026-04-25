---
name: nus-calibrate
description: Propose and apply IDF parameter changes to improve calibration for NUS buildings. Use when ASHRAE metrics fail, when asked to fix setpoints or infiltration, or when the model over/under-predicts. ALWAYS requires human approval before writing any IDF change.
metadata: {"openclaw": {"emoji": "🔧", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Calibrate Skill

## Trigger phrases
"calibrate FOE6", "fix the setpoint", "reduce infiltration", "propose IDF changes",
"recalibrate", "why is FOE6 over-predicting", "fix the model for FOS43",
"apply priority fixes", "correct cooling setpoint", "adjust ACH"

## ABSOLUTE RULES — never break these
1. **Max 2 parameter changes per iteration.**
2. **Iterate until CVRMSE ≤ 15% AND NMBE ≤ ±5% (ASHRAE Guideline 14).** No fixed iteration cap — keep going until thresholds are met. Stop and flag for human review only if all calibration parameters have hit their bounds (min or max) without convergence.
3. **Never propose values outside parameter_bounds.json.**
4. **Confidence < 0.70 → say "refer to engineer" instead of proposing a fix.**
5. **NEVER change Cooling_Setpoint_C.** There is no NUS-wide setpoint policy. IDF setpoints reflect each building's actual operational settings and must be left exactly as found. This applies in all phases — pre-flight, calibration loop, and post-calibration.

## Parameter bounds reference
| Parameter | Min | Max | Calibration? |
|---|---|---|---|
| Cooling_Setpoint_C | — | — | 🔒 NEVER TOUCH — leave as found in IDF |
| Infiltration_ACH | 0.2 | 3.0 | ✅ Yes |
| Lighting_W_per_m2 | 5.0 | 20.0 | ✅ Yes |
| Equipment_W_per_m2 | 5.0 | 80.0 | ✅ Yes |
| HVAC_Daily_Hours | 8.0 | 24.0 | ✅ Yes — only after Eq/Lt/ACH at bounds |
| People_per_m2 | 0.01 | 0.25 | ❌ DO NOT CHANGE without OED data |
| Occupancy_Schedule | 0.0 | 1.0 | ❌ DO NOT CHANGE without OED data |

## Priority fix order (always propose in this sequence)
1. Infiltration_ACH: reduce from Climate Studio default (typically 6.0)
2. Equipment_W_per_m2: adjust based on building type and NMBE direction
3. Lighting_W_per_m2: adjust if still over/under-predicting after EPD fix
4. HVAC_Daily_Hours: extend/shorten schedule only when Eq/Lt/ACH are all at bounds

## Workflow

### Step 1 — Load current metrics
Read `outputs/{BUILDING}/{BUILDING}_calibration_metrics.json` and
`outputs/{BUILDING}/parsed/{BUILDING}_monthly.csv`

**Ground-truth comparison policy:**
- Prefer `ground_truth/ground-truth.csv`
- Use parsed per-building ground truth only as fallback
- Convert measured 2024 energy to annual EUI using `building_registry.json`
- Use that annual EUI series reduction as the calibration target

**Comparison basis (corrected):**
- **Sim side:** `eui_kwh_m2` column from monthly CSV = `electricity_facility_kwh / floor_area_m2`
  (lighting + equipment + own-chiller HVAC electricity, per m²)
- **GT side:** GT total electricity kWh (Section 3 of ground-truth.csv, Jan–Dec 2024) / `floor_area_m2`
  (full building electricity meter, includes cooling electricity)
- Do **NOT** use `eui_adj_kwh_m2` for calibration comparison — that adds district-cooling thermal
  converted via COP and results in gross overestimation vs meter
- Do **NOT** use raw kWh totals — always normalise by floor area to get EUI (kWh/m²/month)

Ground-truth.csv structure:
- Section 1 (cols 2–12): Electrical Consumption (Feb–Dec only, non-cooling)
- Section 2 (cols 14–25): BTU Cooling Consumption (Jan–Dec, thermal)
- **Section 3 (cols 27–38): Electricity Consumption (Jan–Dec) ← USE THIS**

### Step 2 — Diagnose
State the root cause with evidence from the active comparison basis.
Do not mix mixed-year averaged GT with canonical 2024 GT in the same diagnosis.

Example:
"FOE6 under-predicts 2024 annual EUI by 21.6% on NMBE under the canonical 2024 ground-truth basis. Internal loads alone are unlikely to explain the residual pattern; inspect schedules and occupancy assumptions next."

### Step 3 — Request approval via Slack (simple message, no technical details)

Send a short approval request to `#openclaw-alerts` via Signal 📣. **No technical details** — no diffs, no parameter names, no values:

```
{BUILDING} needs recalibration (iteration {N}).
Reply "approve" to proceed or "reject" to skip.
```

**Wait for one of:**
- `"approve"` → proceed to Step 4
- `"reject"` → log deferral, fall through to Report (Phase 5)
- No reply after 4h → watchdog posts escalation to #private; pipeline waits indefinitely

### Step 4 — Write the patch (after Slack approval)

```bash
python3 {SKILL_DIR}/scripts/patch_idf.py --building {BUILDING} \
  --set Infiltration_ACH=0.5 \
  --set Equipment_W_per_m2=10.0 \
  --iteration {N} \
  --approver "{slack_username_who_approved}"
```

`patch_idf.py` handles backup, bounds checking, zone-level patching, and audit logging automatically.

> ⚠️ Never propose a setpoint change regardless of its current value. Setpoints reflect building operations and are not calibration parameters.

Supported parameters: `Infiltration_ACH`, `Lighting_W_per_m2`, `Equipment_W_per_m2`
(`Cooling_Setpoint_C` is never a valid parameter — do not pass it)

### Step 5 — Log the change
Append to `calibration_log.md`:
```markdown
## {BUILDING} — Iteration {N} — {timestamp}
- Approver: {name}
- Infiltration_ACH: 6.0 → 0.5 (Climate Studio default correction)
- Infiltration_ACH: 6.0 → 0.5 (Climate Studio default correction)
- Predicted impact: reduce over-prediction ~20%
```

### Step 6 — Re-simulate
Invoke `nus-simulate` for the patched building, then `nus-parse`, then `nus-groundtruth`.
Report new metrics vs previous iteration.

## Pre-flight checks (run before calibration loop)

### 1. District-cooling mismatch
If `cooling_elec_adj_kwh > 1.10 × electricity_facility_kwh` in the monthly CSV:
- Flag outcome as `district_cooling_mismatch`
- Do **not** iterate — parameters cannot resolve an accounting mismatch
- Recommend NUS OED inspection of district-cooling metering

### 2. Outlier-month detection
Before computing CVRMSE/NMBE, check per-month bias. If any month has `|bias| > 80%`:
- Exclude up to 2 such months from the metric calculation
- Log which months were excluded
- This catches meter anomalies (e.g. FOE11 December +111%) that mask real calibration signal
- Never exclude more than 2 months — if 3+ months are outliers, flag for human review

### 3. CVRMSE-only failure
If NMBE ≤ ±5% but CVRMSE > 15% (shape mismatch, not bias):
- Try reducing Infiltration_ACH first (dampens seasonal swings)
- Try reducing HVAC_Daily_Hours by 1h if ACH already at min
- Do **not** move Equipment or Lighting (would shift NMBE out of range)

## Proportional step sizing
Step size scales with NMBE magnitude — do not use fixed steps for large errors:
| |NMBE| range | Equipment step | Lighting step |
|---|---|---|
| > 50% | 8 W/m² | 4 W/m² |
| 30–50% | 6 W/m² | 3 W/m² |
| 15–30% | 4 W/m² | 2 W/m² |
| < 15% | 2 W/m² | 1 W/m² |

## When to stop iterating
Keep iterating until **CVRMSE ≤ 15% AND NMBE ≤ ±5%** are both met.

Stop only when ALL of the following are at bounds: Eq, Lt, ACH, HVAC_Daily_Hours. In that case, flag for human review:

"⚠️ {BUILDING} has not converged — all calibration parameters at bounds. Human review required.
Current CVRMSE: {value}%, NMBE: {value}%. Recommend NUS OED inspection of meter data and occupancy schedule."

## Multi-building calibration
If asked to calibrate GT buildings, process one at a time. There are 23 GT buildings across 5 archetypes — do not assume only 6.
Do not batch-approve changes across buildings — each requires individual approval.

## Automated calibration script
For bulk runs: `python3 /Users/ye/nus-energy/auto_calibrate.py [--buildings ...] [--max-iter 15]`

v2 upgrades active:
- Outlier-month exclusion (up to 2 months with |bias|>80%)
- DC mismatch pre-flight (skips buildings where cooling_adj > 110% of facility)
- CVRMSE-only branch (ACH/HVAC-hours when NMBE passes but shape fails)
- HVAC_Daily_Hours as 4th calibration variable (Schedule:Day:Hourly + Schedule:Compact)
- Proportional step sizing (scales with NMBE magnitude)
