#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import math
import os
import subprocess
import sys
from pathlib import Path
from statistics import mean

PROJECT = Path(os.environ.get('NUS_PROJECT_DIR', '/Users/ye/nus-energy'))
OUT = PROJECT / 'outputs'
GT = PROJECT / 'ground_truth' / 'parsed'
BOUNDS = json.loads((PROJECT / 'parameter_bounds.json').read_text())
SIMULATE = Path('/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/scripts/simulate.py')
PATCH = Path('/Users/ye/.openclaw/workspace-recalibrationagent/skills/nus-calibrate/scripts/patch_idf.py')
IDF_DIR = PROJECT / 'idfs'
CAL_IDF_DIR = PROJECT / 'calibration_idfs'
EPW = PROJECT / 'weather' / 'calibrated' / '2025_site_calibrated.epw'

GT_BUILDINGS = ['FOE6','FOE9','FOE13','FOE18','FOS43','FOS46','FOE1','FOE3','FOE5','FOE10','FOE11','FOE12','FOE15','FOE16','FOE19','FOE20','FOE23','FOE24','FOE26','FOS26','FOS35','FOS41','FOS44']

TARGETS = {
    'A1_H_L': {'Infiltration_ACH': 0.5, 'Equipment_W_per_m2': 12.0, 'Lighting_W_per_m2': 9.0},
    'A1_L_L': {'Infiltration_ACH': 0.5, 'Equipment_W_per_m2': 10.0, 'Lighting_W_per_m2': 9.0},
    'A1_M_H': {'Infiltration_ACH': 0.3, 'Equipment_W_per_m2': 35.0, 'Lighting_W_per_m2': 10.0},
    'A1_M_L': {'Infiltration_ACH': 0.5, 'Equipment_W_per_m2': 12.0, 'Lighting_W_per_m2': 9.0},
    'A5':     {'Infiltration_ACH': 0.5, 'Equipment_W_per_m2': 10.0, 'Lighting_W_per_m2': 9.0},
}
ARCH = {
    'FOE6':'A1_H_L','FOE9':'A1_H_L','FOE13':'A1_H_L','FOE18':'A1_H_L','FOS43':'A1_H_L','FOS46':'A1_H_L',
    'FOE1':'A1_L_L','FOE3':'A1_L_L','FOE5':'A1_L_L','FOE15':'A1_L_L','FOE24':'A1_L_L','FOE26':'A1_L_L','FOS26':'A1_L_L',
    'FOS35':'A1_M_H','FOS41':'A1_M_H','FOS44':'A1_M_H',
    'FOE11':'A1_M_L','FOE12':'A1_M_L','FOE16':'A1_M_L','FOE19':'A1_M_L','FOE20':'A1_M_L','FOE23':'A1_M_L',
    'FOE10':'A5'
}


def run(cmd):
    p = subprocess.run(cmd, text=True, capture_output=True)
    return p.returncode, p.stdout, p.stderr


def find_idf(building: str) -> Path:
    cal = CAL_IDF_DIR / f'{building}.idf'
    if cal.exists():
        return cal
    direct = IDF_DIR / f'{building}.idf'
    if direct.exists():
        return direct
    matches = list(IDF_DIR.rglob(f'{building}.idf'))
    if not matches:
        raise FileNotFoundError(building)
    return matches[0]


def parse_current_values(building: str):
    idf = find_idf(building)
    text = idf.read_text(errors='ignore').splitlines()
    vals = {}
    for line in text:
        if 'InfiltrationAch' in line and '!-' in line and 'Infiltration_ACH' not in vals:
            import re
            m = re.match(r'\s*([\d.]+)\s*,', line)
            if m:
                vals['Infiltration_ACH'] = float(m.group(1))
                break
    def extract_density(obj_name):
        in_obj = False
        field_count = 0
        for line in text:
            s = line.strip()
            if s.startswith(obj_name):
                in_obj = True
                field_count = 0
                continue
            if in_obj and s and not s.startswith('!'):
                field_count += 1
                if field_count == 6:
                    import re
                    m = re.search(r'([\d.]+)\s*[,;]', line)
                    if m:
                        return float(m.group(1))
                if s.endswith(';'):
                    in_obj = False
        return None
    vals['Lighting_W_per_m2'] = extract_density('Lights,')
    vals['Equipment_W_per_m2'] = extract_density('ElectricEquipment,')
    return vals


def compute_metrics(building: str):
    mpath = OUT / building / 'parsed' / f'{building}_monthly.csv'
    gpath = GT / f'{building}_ground_truth.csv'
    if not mpath.exists() or not gpath.exists():
        return None
    with mpath.open() as f:
        sim_rows = list(csv.DictReader(f))
    with gpath.open() as f:
        gt_rows = list(csv.DictReader(f))
    sim = {}
    for row in sim_rows:
        if str(row.get('month_name','')).upper() == 'ANNUAL':
            continue
        try:
            month = int(float(row['month']))
            val = float(row.get('electricity_facility_kwh') or 0)
        except Exception:
            continue
        sim[month] = sim.get(month, 0.0) + val
    gt_by_month = {}
    for row in gt_rows:
        try:
            month = int(str(row['month'])[5:7])
            val = float(row['measured_kwh'])
        except Exception:
            continue
        gt_by_month.setdefault(month, []).append(val)
    comp = []
    for m in range(1,13):
        if m in sim and m in gt_by_month:
            comp.append((sim[m], mean(gt_by_month[m])))
    if not comp:
        return None
    measured = [g for _,g in comp]
    simulated = [s for s,_ in comp]
    mse = sum((s-g)**2 for s,g in comp)/len(comp)
    rmse = math.sqrt(mse)
    mean_measured = sum(measured)/len(measured)
    cvrmse = rmse / mean_measured * 100 if mean_measured else math.inf
    nmbe = (sum(s-g for s,g in comp) / sum(measured)) * 100 if sum(measured) else math.inf
    return {'cvrmse': round(cvrmse,2), 'nmbe': round(nmbe,2), 'pairs': comp}


def within_thresholds(m):
    return m and m['cvrmse'] <= 15 and abs(m['nmbe']) <= 5


def propose(building: str, metrics: dict, current: dict):
    arch = ARCH[building]
    target = TARGETS[arch]
    props = []
    nmbe = metrics['nmbe']
    # Bias correction first
    if nmbe < -5:
        # simulated too low, increase loads and/or infiltration toward target but not above cap/target
        if current.get('Equipment_W_per_m2') is not None:
            cur = current['Equipment_W_per_m2']
            tgt = max(cur, target['Equipment_W_per_m2'])
            if cur < tgt:
                step = min(BOUNDS['Equipment_W_per_m2']['step_size'], tgt-cur)
                props.append(('Equipment_W_per_m2', round(cur + step, 2)))
        if len(props) < 2 and current.get('Infiltration_ACH') is not None:
            cur = current['Infiltration_ACH']
            tgt = max(cur, target['Infiltration_ACH'])
            upper = min(BOUNDS['Infiltration_ACH']['max'], tgt)
            if cur < upper:
                step = min(BOUNDS['Infiltration_ACH']['step_size'], upper-cur)
                props.append(('Infiltration_ACH', round(cur + step, 2)))
    elif nmbe > 5:
        # simulated too high, reduce loads and/or infiltration toward lower target
        if current.get('Infiltration_ACH') is not None:
            cur = current['Infiltration_ACH']
            tgt = target['Infiltration_ACH']
            lower = max(BOUNDS['Infiltration_ACH']['min'], tgt)
            if cur > lower:
                step = min(BOUNDS['Infiltration_ACH']['step_size'], cur-lower)
                props.append(('Infiltration_ACH', round(cur - step, 2)))
        if len(props) < 2 and current.get('Equipment_W_per_m2') is not None:
            cur = current['Equipment_W_per_m2']
            tgt = target['Equipment_W_per_m2']
            lower = max(BOUNDS['Equipment_W_per_m2']['min'], tgt)
            if cur > lower:
                step = min(BOUNDS['Equipment_W_per_m2']['step_size'], cur-lower)
                props.append(('Equipment_W_per_m2', round(cur - step, 2)))
    # Residual cvrmse cleanup with lighting
    if len(props) < 2 and metrics['cvrmse'] > 15 and current.get('Lighting_W_per_m2') is not None:
        cur = current['Lighting_W_per_m2']
        tgt = target['Lighting_W_per_m2']
        if nmbe < -5 and cur < tgt:
            step = min(BOUNDS['Lighting_W_per_m2']['step_size'], tgt-cur)
            props.append(('Lighting_W_per_m2', round(cur + step, 2)))
        elif nmbe > 5 and cur > tgt:
            step = min(BOUNDS['Lighting_W_per_m2']['step_size'], cur-tgt)
            props.append(('Lighting_W_per_m2', round(cur - step, 2)))
    # unique keep first 2
    out=[]; seen=set()
    for k,v in props:
        if k not in seen:
            seen.add(k); out.append((k,v))
    return out[:2]


def patch(building: str, iteration: int, changes):
    cmd = ['python3', str(PATCH), '--building', building, '--iteration', str(iteration), '--approver', 'Ember-auto']
    for k,v in changes:
        cmd += ['--set', f'{k}={v}']
    return run(cmd)


def simulate(building: str):
    idf = find_idf(building)
    cmd = ['python3', str(SIMULATE), '--idf', str(idf), '--month', '2025-10', '--gt-dir', str(GT), '--output', str(OUT), '--epw', str(EPW)]
    return run(cmd)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--buildings', nargs='*', default=GT_BUILDINGS)
    ap.add_argument('--max-iterations', type=int, default=7)
    args = ap.parse_args()

    results = []
    for b in args.buildings:
        print(f'\n=== {b} ===', flush=True)
        metrics = compute_metrics(b)
        if metrics is None:
            print('missing metrics, skipping')
            results.append({'building': b, 'status': 'missing_metrics'})
            continue
        print('start', metrics, flush=True)
        if within_thresholds(metrics):
            results.append({'building': b, 'status': 'already_calibrated', **metrics, 'iterations': 0})
            continue
        status = 'max_iterations'
        for iteration in range(1, args.max_iterations + 1):
            current = parse_current_values(b)
            changes = propose(b, metrics, current)
            if not changes:
                status = 'stalled'
                break
            print('iter', iteration, 'current', current, 'changes', changes, flush=True)
            rc, out, err = patch(b, iteration, changes)
            print(out[-500:] if out else '', flush=True)
            if rc != 0:
                print(err[-500:] if err else '', flush=True)
                status = f'patch_failed_{rc}'
                break
            rc, out, err = simulate(b)
            print((out or '')[-500:], flush=True)
            if rc != 0:
                print((err or '')[-500:], flush=True)
                status = f'sim_failed_{rc}'
                break
            metrics = compute_metrics(b)
            print('after', metrics, flush=True)
            if within_thresholds(metrics):
                status = 'calibrated'
                break
        results.append({'building': b, 'status': status, **(metrics or {}), 'iterations': iteration if 'iteration' in locals() else 0})
        (OUT / 'auto_calibration_results.json').write_text(json.dumps(results, indent=2))
    print('\nDONE')
    print(json.dumps(results, indent=2))

if __name__ == '__main__':
    main()
