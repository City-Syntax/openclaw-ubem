#!/usr/bin/env python3
"""
prepare_ground_truth.py — Compares ground truth meter data to simulation output.
Usage:
  python3 prepare_ground_truth.py --building FOE1 --meter <meter.csv> --sim <sim.csv>
Outputs: Prints CVRMSE, NMBE, and saves a result CSV/JSON.
"""
import argparse
import pandas as pd
import json
from pathlib import Path
import re

# Helper to match month abbreviations
MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec']
def month_str(m, y):
    return f"{MONTHS[m-1]}-{str(y)[-2:]}"

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--building', required=True)
    parser.add_argument('--meter', required=True)
    parser.add_argument('--sim', required=True)
    args = parser.parse_args()

    building = args.building
    meter_csv = args.meter
    sim_csv = args.sim

    # Load sim output: DataFrame with 'month_name' + 'electricity_facility_kwh'
    sim_df = pd.read_csv(sim_csv)
    sim_df['month_name'] = sim_df['month_name'].str.strip()
    # We'll assume sim covers a full year, map 1-12
    sim_months = {}
    for _, row in sim_df.iterrows():
        m = row['month_name'][:3]
        sim_months[m] = row['electricity_facility_kwh']

    # Load meter ground truth, find row for building
    meter_df = pd.read_csv(meter_csv, dtype=str)
    b_row = meter_df[meter_df.iloc[:, 0]==building]
    if b_row.empty:
        print(f"ERROR: No meter row for {building}")
        return
    b_row = b_row.iloc[0]
    # Find all columns that look like 'Oct-24', etc.
    gt_by_month = {}
    for col in meter_df.columns:
        m = re.match(r'(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)-([\d]{2,4})', col)
        if m:
            val = b_row[col]
            try:
                valf = float(val.replace(',','').replace('"','').strip())
            except:
                continue
            gt_by_month[m.group(0)] = valf

    # For Oct-24, Nov-24, Dec-24 only
    out = {}
    for m in [10,11,12]:
        col = month_str(m, 2024) # e.g., 'Oct-24'
        sim = sim_months.get(MONTHS[m-1])
        gt = gt_by_month.get(col)
        if sim is None or gt is None:
            continue
        abs_err = sim - gt
        nmbe = 100 * abs_err / gt
        cvrmse = 100 * (abs_err ** 2) ** 0.5 / gt
        out[col] = {'sim_kwh': sim, 'meter_kwh': gt, 'NMBE_%': nmbe, 'CVRMSE_%': cvrmse}
        print(f"{building} {col}:\tSim {sim:.0f} kWh, Meter {gt:.0f} kWh\tNMBE {nmbe:+.2f}%, CVRMSE {cvrmse:.2f}%")
    # Optionally save summary
    out_path = Path(sim_csv).parent / f"{building}_oct-dec24_comparison.json"
    with open(out_path, 'w') as f:
        json.dump(out, f, indent=2)

    print(f"Saved result comparison to {out_path}")

if __name__ == '__main__':
    main()
