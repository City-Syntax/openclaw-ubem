# TOOLS.md — RecalibrationAgent

## Project

| Item | Value |
|---|---|
| NUS_PROJECT_DIR | `/Users/ye/.openclaw` |
| Matched buildings | FOE6, FOE13, FOE18, FOS43, FOS46 |

## Key files

| File | Purpose |
|---|---|
| `idfs/{BUILDING}.idf` | IDF to be patched — always back up before patching |
| `patch_idf.py` | Patching script — only way to modify IDFs |
| `calibration_log.md` | Append-only change log — update after every patch |
| `parameter_bounds.json` | Safe ranges — never exceed these |

## Parameter bounds quick reference

| Parameter | Min | Max | NUS standard |
|---|---|---|---|
| Cooling_Setpoint_C | 22.0 | 26.0 | 23.0 (office), 22.0 (lab) |
| Infiltration_ACH | 0.2 | 6.0 | 0.5 |
| Lighting_W_per_m2 | 5.0 | 20.0 | 9.0 baseline |
| Equipment_W_per_m2 | 3.0 | 60.0 | 12.0 baseline |

## Patch command

```bash
cd $NUS_PROJECT_DIR
python3 patch_idf.py --building {BUILDING} \
  --set {PARAMETER}={VALUE} \
  --iteration {N} \
  --approver "Ye (Slack)"
```

## Calibration log format

```markdown
## {BUILDING} — Iteration {N} — {YYYY-MM-DD HH:MM}
- Approver: Ye (Slack)
- {Parameter}: {old} → {new}  ({reason})
- Predicted impact: {description}
- Result: pending re-simulation
```

## Iteration state file

Track per-building iteration count to enforce the 3-iteration limit:
```
outputs/{BUILDING}/{BUILDING}_pipeline_state_{run_id}.json
```
