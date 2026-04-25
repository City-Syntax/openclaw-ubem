"""
parse_eso.py — parses CLB6out.mtr into monthly CSV
Reads the EnergyPlus .mtr format directly — no extra libraries needed.
Run: python3 parse_eso.py --building CLB6
"""
import argparse
import pandas as pd
from pathlib import Path

MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
          "Jul","Aug","Sep","Oct","Nov","Dec"]
GRID_FACTOR = 0.4168  # kgCO2e/kWh (EMA Singapore 2023)

def parse_mtr(mtr_path: str, building: str, outputs_dir: str):
    lines = Path(mtr_path).read_text(errors="ignore").splitlines()

    monthly = {}
    current_month = None

    for line in lines:
        line = line.strip()
        if not line or line.startswith("End") or line.startswith("Program"):
            continue
        parts = line.split(",")
        try:
            code = int(parts[0])
        except:
            continue

        if code == 4 and len(parts) >= 3:
            # e.g. "4,31, 1" -> month=1
            current_month = int(parts[2].strip()) if parts[2].strip().isdigit() else current_month

        elif code == 13 and current_month is not None and len(parts) >= 2:
            # e.g. "13,432108933168.218,..." -> Electricity:Facility [J]
            try:
                joules = float(parts[1].strip())
                kwh    = joules / 3_600_000
                monthly[current_month] = kwh
            except:
                pass

    if not monthly:
        print("ERROR: No monthly data parsed. Check .mtr file.")
        return None

    rows = []
    for m in sorted(monthly.keys()):
        kwh     = monthly[m]
        cooling = kwh * 0.55   # estimate: ~55% cooling in Singapore buildings
        carbon  = kwh * GRID_FACTOR / 1000  # tCO2e
        rows.append({
            "month":                       m,
            "month_name":                  MONTHS[m-1],
            "electricity_facility_kwh":    round(kwh, 2),
            "cooling_electricity_kwh":     round(cooling, 2),
            "other_electricity_kwh":       round(kwh - cooling, 2),
            "carbon_tco2e":                round(carbon, 4),
        })

    df = pd.DataFrame(rows).set_index("month")

    parsed_dir = Path(outputs_dir) / building / "parsed"
    parsed_dir.mkdir(parents=True, exist_ok=True)
    out = parsed_dir / f"{building}_monthly.csv"
    df.to_csv(out)

    print(f"\n  Monthly energy for {building}:")
    print(f"  {'Month':<6} {'kWh':>12} {'Cooling kWh':>14} {'Carbon tCO2e':>14}")
    print(f"  {'-'*48}")
    for _, row in df.iterrows():
        print(f"  {row['month_name']:<6} {row['electricity_facility_kwh']:>12,.0f} "
              f"{row['cooling_electricity_kwh']:>14,.0f} {row['carbon_tco2e']:>14.2f}")
    annual = df["electricity_facility_kwh"].sum()
    print(f"  {'-'*48}")
    print(f"  {'TOTAL':<6} {annual:>12,.0f} kWh  |  "
          f"{annual*GRID_FACTOR/1000:.1f} tCO2e  |  "
          f"EUI {annual/5000:.1f} kWh/m²")
    print(f"\n  Saved -> {out}")
    return df

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--building", default="CLB6")
    parser.add_argument("--outputs",  default="/Users/ye/nus-energy/outputs")
    args = parser.parse_args()

    b       = args.building
    mtr     = Path(args.outputs) / b / "simulation" / (f"{b}out.mtr" if (Path(args.outputs) / b / "simulation" / f"{b}out.mtr").exists() else "eplusout.mtr")

    if not mtr.exists():
        print(f"ERROR: {mtr} not found. Run simulate.py first.")
        return

    parse_mtr(str(mtr), b, args.outputs)

if __name__ == "__main__":
    main()
