import pandas as pd
import numpy as np

def calc_cv_rmse(sim, gt):
    rmse = np.sqrt(np.mean((sim - gt) ** 2))
    mean_gt = np.mean(gt)
    return (rmse / mean_gt) * 100 if mean_gt else np.nan

def calc_nmbe(sim, gt):
    n = len(sim)
    mean_gt = np.mean(gt)
    return (np.sum(sim - gt) / ((n - 1) * mean_gt)) * 100 if mean_gt and n > 1 else np.nan

def error_metrics(sim_df, gt_df, year="2024"):
    # Filter for year/months present in both
    sim_df = sim_df[sim_df['month'].str.startswith(year)]
    gt_df = gt_df[gt_df['month'].str.startswith(year)]
    merged = pd.merge(sim_df, gt_df, on='month', suffixes=('_sim', '_gt'))

    results = {}
    # Find all subcategories (columns ending with _sim except 'month')
    subcats = [c.replace('_sim', '') for c in merged.columns if c.endswith('_sim') and c != 'month_sim']

    for cat in subcats:
        sim = merged[f"{cat}_sim"].values
        gt = merged[f"{cat}_gt"].values
        if len(sim) and len(gt):
            results[cat] = {
                "CV(RMSE)": calc_cv_rmse(sim, gt),
                "NMBE": calc_nmbe(sim, gt)
            }

    # EUI as sum of all subcategories
    sim_eui = merged[[f"{cat}_sim" for cat in subcats]].sum(axis=1)
    gt_eui = merged[[f"{cat}_gt" for cat in subcats]].sum(axis=1)
    results["Total EUI"] = {
        "CV(RMSE)": calc_cv_rmse(sim_eui, gt_eui),
        "NMBE": calc_nmbe(sim_eui, gt_eui)
    }

    return results

# Example usage:
# sim_df = pd.read_csv("monthly_sim.csv")  # must have: month,electrical_kwh,cooling_kwh,...
# gt_df = pd.read_csv("monthly_gt.csv")    # same columns as sim_df
# print(error_metrics(sim_df, gt_df, year="2024"))
