---
name: nus-diagnose
description: Diagnose root causes for NUS buildings that fail ASHRAE calibration thresholds. Use when a building's CVRMSE > 15% or NMBE > ±5%, when asked why a building is over/under-predicting, or when proposing parameters for RecalibrationAgent.
metadata: {"openclaw": {"emoji": "🩺", "requires": {"bins": ["python3"], "env": ["NUS_PROJECT_DIR"]}}}
---

# NUS Diagnose Skill

## Trigger phrases
"why is FOE6 over-predicting", "diagnose FOE13", "what's wrong with FOS43",
"root cause for MAPE 28%", "what parameters should I change", "diagnose calibration failure"

---

## Input required
- Building name
- MAPE and CVRMSE (from `nus-groundtruth` output)
- Monthly error breakdown (`*_verification.csv` or `*_mape_comparison.csv`)
- Current IDF parameter values (read from prepared IDF if available)
- Calibration iteration count (from `$NUS_PROJECT_DIR/calibration_log.md`)

---

## Step 1 — Read inputs

```bash
# Calibration metrics
cat $NUS_PROJECT_DIR/outputs/{BUILDING}/{BUILDING}_calibration_metrics.json

# Monthly error breakdown (per-month abs_error_pct and ashrae_flag)
cat $NUS_PROJECT_DIR/outputs/{BUILDING}/parsed/{BUILDING}_mape_comparison.csv

# Calibration history (check iteration count)
grep -A6 "{BUILDING}" $NUS_PROJECT_DIR/calibration_log.md | head -30
```

---

## Step 2 — Check iteration count first

Read `$NUS_PROJECT_DIR/calibration_log.md` and count how many iterations already exist for this building.
- If **≥ 3 iterations**: set `engineer_review_required: true`. Do not propose further changes. Output:
  > "⚠️ {BUILDING} has reached the 3-iteration limit without convergence. Human/engineer review required."

---

## Step 3 — Diagnose root cause (in order)

Work through these checks sequentially. Stop at 2 high-confidence causes.

### Check 1 — Cooling setpoint (most common)
- Read `ZoneControl:Thermostat` setpoint schedules from the prepared IDF
- If value is 25.0°C (Climate Studio default): **high confidence (0.92)** this is a cause
- Pattern signal: MAPE consistently elevated across **all 12 months** (no seasonal skew)
- Effect: If setpoint too high → AC runs less → simulated energy LOWER than measured (under-prediction)
- Effect: If setpoint too low → AC runs more → simulated energy HIGHER than measured (over-prediction)

### Check 2 — Infiltration ACH
- Read `ZoneInfiltration:DesignFlowRate` from the prepared IDF
- If ACH is 6.0 (Climate Studio default): **high confidence (0.88)** this is a cause
- Pattern signal: error is **larger in shoulder months** (Jan, Feb, Nov, Dec) when outdoor-indoor delta is larger
- Effect: High infiltration → more heat gain → over-prediction of cooling energy

### Check 3 — Seasonal occupancy/equipment mismatch
- Pattern signal: error concentrated in **semester months** (Aug–Nov, Jan–May) → occupancy/plug load issue
- Pattern signal: error in **vacation months only** (Jun–Jul, Dec) → base load / HVAC scheduling issue
- Check `ElectricEquipment` W/m² in IDF vs building type:
  - Office: 12 W/m² baseline
  - Lab/research: 30–50 W/m² common
  - If building is a lab and IDF has 12 W/m²: high confidence mismatch

### Check 4 — Lighting
- Check `Lights` W/m² in IDF
- Older buildings: 12–15 W/m² (pre-LED)
- LED-retrofitted buildings: 5–7 W/m²
- NUS baseline: 9 W/m²
- Only flag if other causes don't explain residual error

### Check 5 — Envelope / glazing (rare)
- Pattern signal: error tracks **outdoor temperature extremes** month-by-month
- Requires OED survey data to confirm. Set confidence ≤ 0.60 unless strong evidence.

---

## Step 4 — Produce structured output

Return **at most 2 causes**, ranked by confidence. Never propose values outside bounds.

```json
{
  "building": "FOE13",
  "mape": 18.3,
  "cvrmse": 24.1,
  "iteration_count": 1,
  "likely_causes": [
    {
      "parameter": "Cooling_Setpoint_C",
      "current": 25.0,
      "suggested": 23.0,
      "confidence": 0.92,
      "rationale": "Setpoint at Climate Studio default 25°C; NUS policy is 23°C. MAPE uniformly elevated across all months."
    },
    {
      "parameter": "Infiltration_ACH",
      "current": 6.0,
      "suggested": 0.5,
      "confidence": 0.85,
      "rationale": "ACH at Climate Studio default 6.0; NUS sealed-building target is 0.5. Higher errors in Jan/Feb shoulder months."
    }
  ],
  "recommend_recalibration": true,
  "engineer_review_required": false,
  "notes": "Priority fix order: setpoint first, infiltration second."
}
```

---

## Step 5 — Pass to RecalibrationAgent

If `recommend_recalibration: true` and `engineer_review_required: false`:
- Forward the structured output above to **RecalibrationAgent (Chisel)** via Orchestrator
- Chisel will format the approval request and await human sign-off before touching any IDF

---

## Safety rules — never break these

| Rule | Detail |
|---|---|
| Max 2 parameters | Never suggest more than 2 changes per iteration |
| Bounds | Never suggest values outside `$NUS_PROJECT_DIR/parameter_bounds.json` |
| Confidence floor | If all causes have confidence < 0.70 → set `engineer_review_required: true` |
| Iteration cap | If building already has ≥ 3 iterations in `calibration_log.md` → engineer review |
| No IDF writes | DiagnosisAgent never touches IDF files — that is Chisel's job |

---

## Parameter bounds (quick reference)

| Parameter | Min | Max | NUS standard |
|---|---|---|---|
| Cooling_Setpoint_C | 22.0 | 26.0 | 23.0 (office), 22.0 (lab) |
| Infiltration_ACH | 0.2 | 6.0 | 0.5 (sealed modern building) |
| Lighting_W_per_m2 | 5.0 | 20.0 | 9.0 baseline |
| Equipment_W_per_m2 | 3.0 | 60.0 | 12.0 (office), 30–50 (lab) |

---

## After diagnosis — what to say

Format the finding for Orchestrator in one block:

```
🩺 {BUILDING} — Diagnosis complete (Iteration {N}/3)

MAPE: {mape}%  CVRMSE: {cvrmse}%  Status: ⚠️ Needs recalibration

Root causes identified:
  1. Cooling_Setpoint_C: 25.0°C → 23.0°C  (confidence 0.92)
     Reason: uniform over-prediction, Climate Studio default
  2. Infiltration_ACH: 6.0 → 0.5  (confidence 0.85)
     Reason: shoulder-month error spike, sealed building

→ Forwarding to Chisel for approval request.
```
