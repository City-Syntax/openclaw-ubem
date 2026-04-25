#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path

PATCH = Path('/Users/ye/.openclaw/workspace-recalibrationagent/skills/nus-calibrate/scripts/patch_idf.py')

PROJECT = Path(os.environ.get('NUS_PROJECT_DIR', '/Users/ye/nus-energy'))
OUT = PROJECT / 'outputs'
GT_BUILDINGS = ['FOE6','FOE9','FOE13','FOE18','FOS43','FOS46','FOE1','FOE3','FOE5','FOE10','FOE11','FOE12','FOE15','FOE16','FOE19','FOE20','FOE23','FOE24','FOE26','FOS26','FOS35','FOS41','FOS44']
HANDOFF = Path('/Users/ye/.openclaw/workspace-diagnosisagent/skills/nus-diagnose/scripts/diagnosis_handoff.py')

ARCH = {
    'FOE6':'A1_H_L','FOE9':'A1_H_L','FOE13':'A1_H_L','FOE18':'A1_H_L','FOS43':'A1_H_L','FOS46':'A1_H_L',
    'FOE1':'A1_L_L','FOE3':'A1_L_L','FOE5':'A1_L_L','FOE15':'A1_L_L','FOE24':'A1_L_L','FOE26':'A1_L_L','FOS26':'A1_L_L',
    'FOS35':'A1_M_H','FOS41':'A1_M_H','FOS44':'A1_M_H',
    'FOE11':'A1_M_L','FOE12':'A1_M_L','FOE16':'A1_M_L','FOE19':'A1_M_L','FOE20':'A1_M_L','FOE23':'A1_M_L',
    'FOE10':'A5'
}
BOUNDS = {
    'Infiltration_ACH': (0.2, 6.0),
    'Lighting_W_per_m2': (5.0, 20.0),
    'Equipment_W_per_m2': (3.0, 60.0),
}

ARCH_DEFAULTS = {
    'A1_H_L': {'Infiltration_ACH': 2.0, 'Equipment_W_per_m2': 12.0, 'Lighting_W_per_m2': 9.0},
    'A1_L_L': {'Equipment_W_per_m2': 10.0, 'Lighting_W_per_m2': 9.0, 'Infiltration_ACH': 2.0},
    'A1_M_H': {'Equipment_W_per_m2': 35.0, 'Lighting_W_per_m2': 10.0, 'Infiltration_ACH': 2.0},
    'A1_M_L': {'Equipment_W_per_m2': 12.0, 'Lighting_W_per_m2': 9.0, 'Infiltration_ACH': 2.0},
    'A5': {'Equipment_W_per_m2': 10.0, 'Lighting_W_per_m2': 9.0, 'Infiltration_ACH': 2.0},
}


def run(cmd):
    p = subprocess.run(cmd, text=True, capture_output=True)
    return p.returncode, p.stdout, p.stderr


def metrics(building: str):
    summary_path = OUT / 'simulation_summary.csv'
    if summary_path.exists():
        try:
            lines = summary_path.read_text().splitlines()
            if lines:
                import csv
                rows = list(csv.DictReader(lines))
                matches = [r for r in rows if r.get('building') == building and r.get('cvrmse') not in (None, '', 'nan') and r.get('nmbe') not in (None, '', 'nan')]
                if matches:
                    rec = matches[-1]
                    return float(rec['cvrmse']), float(rec['nmbe'])
        except Exception:
            pass

    auto_results = OUT / 'auto_calibration_results.json'
    if auto_results.exists():
        try:
            data = json.loads(auto_results.read_text())
            rec = next((x for x in data if x.get('building') == building and x.get('cvrmse') is not None and x.get('nmbe') is not None), None)
            if rec:
                return float(rec['cvrmse']), float(rec['nmbe'])
        except Exception:
            pass

    state = json.loads((OUT / 'pipeline_state.json').read_text()) if (OUT / 'pipeline_state.json').exists() else {}
    b = state.get('buildings', {}).get(building, {})
    if b.get('cvrmse') is not None and b.get('nmbe') is not None:
        return float(b['cvrmse']), float(b['nmbe'])

    candidates = [
        OUT / building / f'{building}_calibration_metrics.json',
        PROJECT / 'ground_truth' / building / f'{building}_calibration_metrics.json',
    ]
    for p in candidates:
        if p.exists():
            data = json.loads(p.read_text())
            c = data.get('cvrmse') or data.get('cvrmse_pct')
            n = data.get('nmbe')
            if n is None:
                n = data.get('mbe_pct')
            if c is not None and n is not None:
                return float(c), float(n)
    return None, None


def read_current_params(building: str):
    current = {}
    for param, probe in [('Infiltration_ACH', 2.0), ('Equipment_W_per_m2', 12.0), ('Lighting_W_per_m2', 9.0)]:
        rc, out, err = run(['python3', str(PATCH), '--building', building, '--dry-run', '--set', f'{param}={probe}'])
        text = out + '\n' + err
        for line in text.splitlines():
            if f'[DRY RUN] {param}:' in line:
                try:
                    old = line.split(':', 1)[1].split('→', 1)[0].strip()
                    current[param] = float(old)
                except Exception:
                    pass
                break

    arch = ARCH.get(building)
    defaults = ARCH_DEFAULTS.get(arch, {}) if arch else {}
    for param, value in defaults.items():
        current.setdefault(param, value)
    return current


def clamp(param: str, value: float) -> float:
    lo, hi = BOUNDS[param]
    return max(lo, min(hi, round(value, 3)))


def step_toward(current: float, target: float, fraction: float = 0.5, min_step: float = 0.2) -> float:
    delta = target - current
    if abs(delta) < min_step:
        return target
    step = delta * fraction
    if abs(step) < min_step:
        step = min_step if delta > 0 else -min_step
    return current + step


def diagnosed_output(building: str, iteration: int):
    c, n = metrics(building)
    if c is None or n is None:
        return None
    if c <= 15 and abs(n) <= 5:
        return {'building': building, 'likely_causes': [], 'recommend_recalibration': False, 'engineer_review_required': False}

    arch = ARCH[building]
    defaults = ARCH_DEFAULTS[arch]
    current = read_current_params(building)
    causes = []

    near_pass = c <= 15 and 5 < abs(n) <= 10
    residual_high_variance = c > 15 and abs(n) <= 5

    if n < -5:
        order = ['Infiltration_ACH'] if near_pass else (['Infiltration_ACH', 'Equipment_W_per_m2', 'Lighting_W_per_m2'] if abs(n) > 10 else ['Infiltration_ACH'])
        rationale = 'Simulation under-predicts meter data; raise bounded internal load/infiltration incrementally.' if not near_pass else 'Near-pass fine tuning: nudge a single parameter upward to bring NMBE into range.'
        up_targets = {
            'Infiltration_ACH': min(BOUNDS['Infiltration_ACH'][1], max(defaults.get('Infiltration_ACH', 2.0), current.get('Infiltration_ACH', 2.0)) + (0.2 if near_pass else 0.5)),
            'Equipment_W_per_m2': min(BOUNDS['Equipment_W_per_m2'][1], max(defaults.get('Equipment_W_per_m2', 12.0), current.get('Equipment_W_per_m2', 12.0)) + (0.5 if near_pass else 2.0)),
            'Lighting_W_per_m2': min(BOUNDS['Lighting_W_per_m2'][1], max(defaults.get('Lighting_W_per_m2', 9.0), current.get('Lighting_W_per_m2', 9.0)) + (0.5 if near_pass else 1.0)),
        }
        for param in order:
            cur = current.get(param)
            if cur is None:
                continue
            target = up_targets[param]
            suggested = clamp(param, step_toward(cur, target, fraction=1.0, min_step=0.1 if param == 'Infiltration_ACH' else (0.5 if near_pass else 1.0)))
            if suggested != cur:
                causes.append({'parameter': param, 'current': cur, 'suggested': suggested, 'confidence': 0.85 if near_pass else 0.8, 'rationale': rationale})
    elif n > 5:
        order = [('Infiltration_ACH', 0.5)] if near_pass else ([('Infiltration_ACH', 0.5), ('Equipment_W_per_m2', 5.0), ('Lighting_W_per_m2', 5.0)] if abs(n) > 10 else [('Infiltration_ACH', 0.5)])
        rationale = 'Simulation over-predicts meter data; lower bounded load/infiltration incrementally.' if not near_pass else 'Near-pass fine tuning: nudge a single parameter downward to bring NMBE into range.'
        for param, target in order:
            cur = current.get(param)
            if cur is None:
                continue
            suggested = clamp(param, step_toward(cur, target, fraction=0.5 if near_pass else 1.0, min_step=0.1 if param == 'Infiltration_ACH' else (0.5 if near_pass else 1.0)))
            if suggested != cur:
                causes.append({'parameter': param, 'current': cur, 'suggested': suggested, 'confidence': 0.85 if near_pass else 0.8, 'rationale': rationale})
    elif residual_high_variance:
        rationale = 'NMBE is already within range, but CVRMSE is still high. Continue autonomous variance reduction by nudging internal load densities upward in small bounded steps until CVRMSE improves or bounds are reached.'
        for param, step in [('Lighting_W_per_m2', 1.0), ('Equipment_W_per_m2', 2.0)]:
            cur = current.get(param)
            if cur is None:
                continue
            ceiling = min(BOUNDS[param][1], max(defaults.get(param, cur), cur) + step)
            suggested = clamp(param, step_toward(cur, ceiling, fraction=1.0, min_step=step))
            if suggested != cur:
                causes.append({'parameter': param, 'current': cur, 'suggested': suggested, 'confidence': 0.72, 'rationale': rationale})

    causes = causes[:2]
    return {
        'building': building,
        'cvrmse': c,
        'nmbe': n,
        'iteration_count': iteration,
        'likely_causes': causes,
        'recommend_recalibration': True if causes else False,
        'engineer_review_required': False if causes else True,
        'notes': 'Auto-generated diagnosis proxy with current-value-aware incremental calibration.'
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--buildings', nargs='*', default=GT_BUILDINGS)
    ap.add_argument('--max-iterations', type=int, default=5)
    args = ap.parse_args()
    results = []
    for b in args.buildings:
        print(f'\n=== {b} ===', flush=True)
        final_status = 'stalled'
        for i in range(1, args.max_iterations + 1):
            c, n = metrics(b)
            print('metrics', c, n, flush=True)
            if c is not None and c <= 15 and abs(n) <= 5:
                final_status = 'calibrated'
                break
            lens = diagnosed_output(b, i)
            if not lens:
                final_status = 'missing_metrics'
                break
            out_path = OUT / b / 'lens_output.json'
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(lens, indent=2))
            rc, out, err = run(['python3', str(HANDOFF), '--building', b, '--iteration', str(i), '--approver', 'Ye-auto', '--apply'])
            print(out[-1500:] if out else '', flush=True)
            if err:
                print(err[-800:], flush=True)
            if rc != 0:
                final_status = f'handoff_failed_{rc}'
                break
        c, n = metrics(b)
        results.append({'building': b, 'status': final_status, 'cvrmse': c, 'nmbe': n})
        (OUT / 'gt_calibration_loop_results.json').write_text(json.dumps(results, indent=2))
    print('\nDONE')
    print(json.dumps(results, indent=2))

if __name__ == '__main__':
    main()
