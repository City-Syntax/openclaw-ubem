#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
import csv

PROJECT = Path(os.environ.get('NUS_PROJECT_DIR', '/Users/ye/nus-energy'))
OUT = PROJECT / 'outputs'
GT = PROJECT / 'ground_truth' / 'parsed'
STATE = OUT / 'pipeline_state.json'
PATCH = Path('/Users/ye/.openclaw/workspace-recalibrationagent/skills/nus-calibrate/scripts/patch_idf.py')
SIMULATE = Path('/Users/ye/.openclaw/workspace-simulationagent/skills/nus-simulate/scripts/simulate.py')
EPW = PROJECT / 'weather' / 'calibrated' / '2025-10_site_calibrated.epw'
IDF_DIR = PROJECT / 'idfs'

ALLOWED = {'Infiltration_ACH', 'Lighting_W_per_m2', 'Equipment_W_per_m2'}


def run(cmd):
    p = subprocess.run(cmd, text=True, capture_output=True)
    return p.returncode, p.stdout, p.stderr


def find_idf(building: str) -> Path:
    cal = PROJECT / 'calibration_idfs' / f'{building}.idf'
    if cal.exists():
        return cal
    direct = IDF_DIR / f'{building}.idf'
    if direct.exists():
        return direct
    matches = list(IDF_DIR.rglob(f'{building}.idf'))
    if not matches:
        raise FileNotFoundError(building)
    return matches[0]


def lens_to_sets(lens_output: dict):
    sets = []
    for item in lens_output.get('likely_causes', []):
        p = item.get('parameter')
        s = item.get('suggested')
        cur = item.get('current')
        if p in ALLOWED and isinstance(s, (int, float)):
            if isinstance(cur, (int, float)) and float(cur) == float(s):
                continue
            sets.append((p, float(s)))
    uniq = []
    seen = set()
    for p, v in sets:
        if p not in seen:
            seen.add(p)
            uniq.append((p, v))
    return uniq[:2]


def update_state(building: str, status: str, iteration_inc: bool = False, metrics: dict | None = None):
    if not STATE.exists():
        return
    state = json.loads(STATE.read_text())
    b = state.setdefault('buildings', {}).setdefault(building, {})
    b['status'] = status
    if iteration_inc:
        b['iteration'] = b.get('iteration', 0) + 1
    if metrics:
        if metrics.get('cvrmse') is not None:
            b['cvrmse'] = metrics['cvrmse']
        if metrics.get('nmbe') is not None:
            b['nmbe'] = metrics['nmbe']
    STATE.write_text(json.dumps(state, indent=2))


def latest_summary_metrics(building: str):
    summary_path = OUT / 'simulation_summary.csv'
    if not summary_path.exists():
        return None
    try:
        rows = list(csv.DictReader(summary_path.read_text().splitlines()))
        matches = [r for r in rows if r.get('building') == building and r.get('cvrmse') not in (None, '', 'nan') and r.get('nmbe') not in (None, '', 'nan')]
        if not matches:
            return None
        rec = matches[-1]
        return {'cvrmse': float(rec['cvrmse']), 'nmbe': float(rec['nmbe'])}
    except Exception:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--building', required=True)
    ap.add_argument('--approver', default='Ye')
    ap.add_argument('--iteration', type=int, default=1)
    ap.add_argument('--apply', action='store_true')
    args = ap.parse_args()

    building = args.building.upper()
    lens_path = OUT / building / 'lens_output.json'
    if not lens_path.exists():
        raise SystemExit(f'missing {lens_path}')
    lens = json.loads(lens_path.read_text())
    if not lens.get('recommend_recalibration', False):
        update_state(building, 'diagnosed_no_recalibration')
        print(f'{building}: no recalibration recommended')
        return
    if lens.get('engineer_review_required', False):
        update_state(building, 'engineer_review_required')
        print(f'{building}: engineer review required')
        return
    sets = lens_to_sets(lens)
    if not sets:
        update_state(building, 'stalled_no_change')
        print(f'{building}: no actionable parameter changes, proposed values already match current IDF')
        return

    cmd = ['python3', str(PATCH), '--building', building, '--iteration', str(args.iteration), '--approver', args.approver]
    for p, v in sets:
        cmd += ['--set', f'{p}={v}']
    if not args.apply:
        cmd += ['--dry-run']
    rc, out, err = run(cmd)
    print(out)
    if err:
        print(err)
    if rc != 0:
        update_state(building, 'patch_error')
        raise SystemExit(rc)

    if not args.apply:
        update_state(building, 'diagnosis_handoff_previewed')
        return

    sim_cmd = ['python3', str(SIMULATE), '--idf', str(find_idf(building)), '--month', '2025-10', '--gt-dir', str(GT), '--output', str(OUT), '--epw', str(EPW)]
    rc, out, err = run(sim_cmd)
    print(out)
    if err:
        print(err)
    if rc != 0:
        update_state(building, 'resim_error', iteration_inc=True)
        raise SystemExit(rc)

    fresh = latest_summary_metrics(building)
    update_state(building, 'recalibrated', iteration_inc=True, metrics=fresh)
    print(f'{building}: handoff complete')

if __name__ == '__main__':
    main()
