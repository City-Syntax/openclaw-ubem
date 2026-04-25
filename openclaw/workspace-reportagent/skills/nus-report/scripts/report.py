"""
OpenClaw — report.py
=====================
Called by ReportAgent in agents.py to generate:
  - reports/{building}/{building}_report.pdf       (per-building PDF)
  - reports/{building}/{building}_calibration_log.md  (markdown log)
  - reports/NUS_Campus_Summary_Report.pdf          (campus summary, --campus flag)

The ReportAgent calls this as a subprocess:
    python report.py --building CLB6
    python report.py --campus

This script reads from the outputs/ folder produced by simulate.py,
and writes to the reports/ folder. It is stateless — the PipelineState
is passed in via the outputs/ CSVs and JSON logs on disk.

Requirements:
    pip3 install reportlab matplotlib pandas numpy

Usage (standalone):
    python report.py --building CLB6
    python report.py --campus
    python report.py --building CLB6 --open
"""

import os
import sys
import json
import argparse
import subprocess
from pathlib import Path
from datetime import datetime

import numpy as np
import pandas as pd
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import matplotlib.patches as mpatches

from reportlab.lib.pagesizes import A4
from reportlab.lib import colors
from reportlab.lib.units import mm
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_LEFT, TA_CENTER
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle,
    HRFlowable, PageBreak, Image
)


# ══════════════════════════════════════════════════════════════════════════
# PATHS — must match CONFIG in agents.py
# ══════════════════════════════════════════════════════════════════════════

PROJECT_ROOT = Path(os.getenv("NUS_PROJECT_DIR", os.getcwd())).resolve()
OUTPUTS_DIR = PROJECT_ROOT / "outputs"
REPORTS_DIR = PROJECT_ROOT / "reports"
CHARTS_DIR  = REPORTS_DIR / "_charts"
INTERVENTION_SCRIPT = Path("/Users/ye/.openclaw/workspace-interventionagent/skills/nus-intervention/scripts/carbon_scenarios.py")

REPORTS_DIR.mkdir(parents=True, exist_ok=True)
CHARTS_DIR.mkdir(parents=True, exist_ok=True)


def _clear_building_chart_cache(building: str):
    """Remove cached chart images for a building before regenerating the PDF."""
    for suffix in ["monthly_energy", "mape", "carbon_bca"]:
        p = CHARTS_DIR / f"{building}_{suffix}.png"
        if p.exists():
            p.unlink()

# Singapore grid emission factor (EMA 2023)
GRID_FACTOR_KG_PER_KWH = 0.4168
SGD_PER_KWH            = 0.28   # SP Group commercial tariff ~2024

# ── BCA Green Mark 2021 — typology-specific thresholds (Fig 9 / Table 1B) ────
# Source: BCA Green Mark 2021 Energy Efficiency Section, 2nd Ed. (Jan 2024)
# Each entry: (GoldPLUS, Platinum, SLE) in kWh/m²/yr
#
# IHL (University, Polytechnics and ITE):
#   GoldPLUS ≤ 130  |  Platinum ≤ 120  |  SLE ≤ 90
# Hospitals (Private and General):
#   GoldPLUS ≤ 375  |  Platinum ≤ 340  |  SLE ≤ 300
# Research + residential: no Pathway 1 EUI threshold; IHL used as proxy.

BCA_THRESHOLDS = {
    # IHL — academic / functional buildings
    'Research':             (130, 120, 90),   # proxy — no dedicated BCA category
    'Business':             (130, 120, 90),
    'Art & Social Science': (130, 120, 90),
    'Science':              (130, 120, 90),
    'Engineering':          (130, 120, 90),
    'CLB':                  (130, 120, 90),
    'YIH':                  (130, 120, 90),
    'UCC':                  (130, 120, 90),
    # Hospital — Health (UHC) buildings only
    'Health':               (375, 340, 300),
    # Residential — no applicable Pathway 1 EUI; IHL used as proxy
    'University Hall':      (130, 120, 90),   # proxy
    'Hall':                 (130, 120, 90),   # proxy
    'PGP':                  (130, 120, 90),   # proxy
    'UTown':                (130, 120, 90),   # proxy
    'Kent Vale':            (130, 120, 90),   # proxy
}

# Typologies assessed against IHL proxy (not a native BCA EUI category)
BCA_PROXY_TYPOLOGIES = {'Research', 'University Hall', 'Hall', 'PGP', 'UTown', 'Kent Vale'}

# Four compliance bands — exact Fig 9 naming and order (worst → best)
BCA_BANDS = ['above_goldplus', 'goldplus', 'platinum', 'sle']

BCA_BAND_LABELS = {
    'above_goldplus': '> GoldPLUS',
    'goldplus':       'GoldPLUS  (≤130 / ≤375)',
    'platinum':       'Platinum   (≤120 / ≤340)',
    'sle':            'SLE           (≤90 / ≤300)',
}

# Exact Fig 9 band colours — warm (non-compliant) → cool (compliant)
BCA_BAND_COLORS_HEX = {
    'above_goldplus': '#C0504D',   # dark coral
    'goldplus':       '#F2A9A0',   # salmon
    'platinum':       '#8AB5BE',   # steel blue
    'sle':            '#2E5F8A',   # dark navy
}

# Kept for backward-compat references elsewhere in the file; mirrors IHL thresholds
BCA_BENCHMARKS = {
    "GoldPLUS": 130,
    "Platinum": 120,
    "SLE":       90,
}


def _building_typology(building: str) -> str:
    """Map building code to typology label — consistent with Fig 6–9."""
    for prefix, typ in [
        ('UHC',   'Health'),
        ('UCC',   'UCC'),
        ('UHALL', 'University Hall'),
        ('FASS',  'Art & Social Science'),
        ('FOE',   'Engineering'),
        ('FOS',   'Science'),
        ('BIZ',   'Business'),
        ('CLB',   'CLB'),
        ('YIH',   'YIH'),
        ('HALL',  'Hall'),
        ('PGP',   'PGP'),
        ('KV',    'Kent Vale'),
        ('UT',    'UTown'),
        ('R',     'Research'),
    ]:
        if building.startswith(prefix):
            return typ
    return 'Other'


def get_band(eui_val: float, typology: str) -> str:
    """
    Classify EUI into one of four BCA GM:2021 compliance bands.
    Uses typology-specific thresholds from Fig 9 BCA_THRESHOLDS.
    Returns: 'sle' | 'platinum' | 'goldplus' | 'above_goldplus'
    """
    if eui_val is None or (isinstance(eui_val, float) and np.isnan(eui_val)):
        return 'above_goldplus'
    goldplus, platinum, sle = BCA_THRESHOLDS.get(typology, (130, 120, 90))
    if eui_val <= sle:        return 'sle'
    if eui_val <= platinum:   return 'platinum'
    if eui_val <= goldplus:   return 'goldplus'
    return 'above_goldplus'


def bca_band_label(band: str) -> str:
    """Human-readable label for a BCA compliance band."""
    return BCA_BAND_LABELS.get(band, band)


def bca_tier(eui_val: float, typology: str = 'Engineering') -> str:
    """Return BCA band label string for a given EUI and typology."""
    return bca_band_label(get_band(eui_val, typology))


def bca_color(eui_val: float, typology: str = 'Engineering'):
    """
    Return ReportLab HexColor for a BCA compliance band.
    Exact Fig 9 palette — coral (worst) → navy (best).
    """
    band = get_band(eui_val, typology)
    return colors.HexColor(BCA_BAND_COLORS_HEX.get(band, '#555555'))


MONTHS = ["Jan","Feb","Mar","Apr","May","Jun",
          "Jul","Aug","Sep","Oct","Nov","Dec"]

GROUND_TRUTH_BUILDINGS = {"FOE6", "FOE9", "FOE13", "FOE18", "FOS43", "FOS46"}

# ── Building registry (floor areas) ──────────────────────────────────────────
_REGISTRY_CACHE: dict = {}

def _registry() -> dict:
    global _REGISTRY_CACHE
    if not _REGISTRY_CACHE:
        candidate_paths = [
            PROJECT_ROOT / "building_registry.json",
            PROJECT_ROOT / "openclaw_agents" / "building_registry.json",
        ]
        for reg_path in candidate_paths:
            if reg_path.exists():
                with open(reg_path) as f:
                    _REGISTRY_CACHE = json.load(f)
                break
    return _REGISTRY_CACHE

def floor_area(building: str) -> float:
    """Return floor area m² from building_registry.json, fallback 5000."""
    return _registry().get(building, {}).get("floor_area_m2", 5000) or 5000


# ══════════════════════════════════════════════════════════════════════════
# COLOURS — aligned with Fig 6 / 7 / 8 / 9 visualization palette
# ══════════════════════════════════════════════════════════════════════════

# Primary chart colours (end-use stack, consistent with Fig 6 & 7)
C_COOL      = "#D2676F"   # cooling   — coral red
C_EQUIP     = "#F2A9A0"   # equipment — salmon pink
C_LIGHT_EU  = "#8AB5BE"   # lighting  — steel blue
C_BAR_EDGE  = "black"     # thin black outline on every bar (Fig 6 style)
C_EDGE_LW   = 0.2         # linewidth for bar outlines

# Intervention tier colours (Fig 8)
C_BASE      = "#C0504D"   # baseline      — dark coral
C_T1        = "#E8A09E"   # Tier 1        — light coral
C_T2        = "#8AB5BE"   # Tier 2        — steel blue
C_T3        = "#4A749E"   # Tier 3        — navy blue
C_SLE       = "#2E5F8A"   # SLE compliant — dark navy (Fig 9)

# EUI tier band colours for callouts / compliance (Fig 9)
C_ABOVE_GP  = "#C0504D"   # > GoldPLUS — dark coral
C_GOLDPLUS  = "#F2A9A0"   # GoldPLUS   — salmon
C_PLATINUM  = "#8AB5BE"   # Platinum   — steel blue
C_SLE_BAND  = "#2E5F8A"   # SLE band   — dark navy

# Typology colour strip (Fig 6 TYP_BAR_COLOR, mapped to ReportLab HexColor)
TYP_BAR_COLOR_HEX = {
    'Research':             "#860707",
    'Health':               "#dd35c1",
    'Business':             "#e26b3a",
    'CLB':                  "#f79646",
    'YIH':                  "#f4b183",
    'Art & Social Science': "#ffe699",
    'Science':              "#a9d18e",
    'Engineering':          "#70ad47",
    'UCC':                  "#9dc3e6",
    'University Hall':      "#4bacc6",
    'UTown':                "#2e75b6",
    'Hall':                 "#1131E4",
    'PGP':                  "#5944c4",
    'Kent Vale':            "#c9c9c9",
}

def _typ_color_rl(typology_key: str):
    """Return ReportLab HexColor for a typology, falling back to light grey."""
    return colors.HexColor(TYP_BAR_COLOR_HEX.get(typology_key, "#cccccc"))

# EUI tier thresholds — match Fig 6 / Fig 8 / Fig 9
EUI_HIGH_THRESH = 350   # High / Mid boundary
EUI_LOW_THRESH  = 200   # Mid / Low boundary

def eui_tier_label(eui_val: float) -> str:
    """Return EUI tier label matching Fig 6 tier system."""
    if eui_val > EUI_HIGH_THRESH:
        return f"High EUI  >{EUI_HIGH_THRESH} kWh/m²/yr"
    if eui_val > EUI_LOW_THRESH:
        return f"Mid EUI  {EUI_LOW_THRESH}–{EUI_HIGH_THRESH} kWh/m²/yr"
    return f"Low EUI  <{EUI_LOW_THRESH} kWh/m²/yr"

def eui_tier_bg(eui_val: float):
    """Return ReportLab background color for EUI tier callout."""
    if eui_val > EUI_HIGH_THRESH:
        return colors.HexColor("#FDECEA")   # warm red tint
    if eui_val > EUI_LOW_THRESH:
        return colors.HexColor("#FFF4E5")   # amber tint
    return colors.HexColor("#EAF4F4")       # cool teal tint

# ReportLab semantic colours (legacy aliases retained for backward compat)
TEAL        = colors.HexColor("#2e75b6")    # UTown blue — anchors header/table chrome
TEAL_LIGHT  = colors.HexColor("#D6EAF8")
GREEN       = colors.HexColor("#70ad47")    # Engineering green
GREEN_LITE  = colors.HexColor("#EAF4E8")
AMBER       = colors.HexColor("#f79646")    # CLB amber
AMBER_LITE  = colors.HexColor("#FFF4E5")
RED         = colors.HexColor("#C0504D")    # baseline / high-EUI coral
RED_LITE    = colors.HexColor("#FDECEA")
DARK        = colors.HexColor("#2C3E50")
GRAY        = colors.HexColor("#555555")
LIGHT_GRAY  = colors.HexColor("#F2F3F4")


# ══════════════════════════════════════════════════════════════════════════
# DATA LOADER
# ══════════════════════════════════════════════════════════════════════════

def _refresh_intervention_data(building: str):
    """Refresh Compass output before report generation when the script is available."""
    if not INTERVENTION_SCRIPT.exists():
        return
    cmd = [
        "python3", str(INTERVENTION_SCRIPT),
        "--building", building,
        "--outputs", str(OUTPUTS_DIR),
        "--idfs", str(PROJECT_ROOT / "idfs"),
    ]
    env = {**os.environ, "NUS_PROJECT_DIR": str(PROJECT_ROOT)}
    try:
        subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env, cwd=str(PROJECT_ROOT), check=False)
    except Exception:
        pass


def load_building_data(building: str) -> dict:
    """
    Load all pipeline outputs for a building from outputs/.
    Returns dict — missing files are silently skipped (agents handle errors).
    """
    base = OUTPUTS_DIR / building / "parsed"
    data = {"building": building}

    monthly_path  = base / f"{building}_monthly.csv"
    mape_path     = base / f"{building}_mape_comparison.csv"
    carbon_path   = OUTPUTS_DIR / building / "carbon" / f"{building}_carbon_scenarios.json"
    log_json_path = OUTPUTS_DIR / building / f"{building}_calibration_log.json"
    state_paths   = sorted((OUTPUTS_DIR / building).glob(f"{building}_pipeline_state_*.json"))

    if monthly_path.exists():
        data["monthly"] = pd.read_csv(monthly_path, index_col=0)

    if mape_path.exists():
        data["mape"] = pd.read_csv(mape_path, index_col=0)

    if carbon_path.exists():
        with open(carbon_path) as f:
            data["carbon"] = json.load(f)

    if log_json_path.exists():
        with open(log_json_path) as f:
            data["cal_log"] = json.load(f)

    # Load most recent pipeline state if available
    if state_paths:
        with open(state_paths[-1]) as f:
            data["pipeline_state"] = json.load(f)

    return data


def load_all_buildings() -> list:
    if not OUTPUTS_DIR.exists():
        return []
    buildings = [
        d.name for d in OUTPUTS_DIR.iterdir()
        if d.is_dir() and (d / "parsed").exists()
    ]
    return [load_building_data(b) for b in sorted(buildings)]


def _elec_col(df):
    for c in df.columns:
        if "electricity_facility" in c.lower():
            return c
    return None

def _cool_col(df):
    for c in df.columns:
        if "cooling_electricity" in c.lower():
            return c
    return None


def _adjusted_eui_metrics(df, floor_area_m2: float):
    """Return adjusted annual kWh and EUI including cooling elec equivalent when available."""
    if df is None or not floor_area_m2:
        return None, None, None

    working = df.copy()
    if "month_name" in working.columns:
        working = working[working["month_name"].astype(str).str.upper() != "ANNUAL"].copy()

    annual_elec_kwh = None
    annual_cooling_adj_kwh = 0.0

    if "electricity_facility_kwh" in working.columns:
        annual_elec_kwh = float(working["electricity_facility_kwh"].sum())
    else:
        ec = _elec_col(working)
        if ec:
            annual_elec_kwh = float(working[ec].sum())

    if annual_elec_kwh is None:
        return None, None, None

    if "cooling_elec_adj_kwh" in working.columns:
        annual_cooling_adj_kwh = float(working["cooling_elec_adj_kwh"].sum())
    elif "district_cooling_kwh" in working.columns:
        annual_cooling_adj_kwh = float(working["district_cooling_kwh"].sum()) / 4.5
    elif "cooling_thermal_kwh" in working.columns:
        annual_cooling_adj_kwh = float(working["cooling_thermal_kwh"].sum()) / 4.5
    elif "cooling_electricity_kwh" in working.columns:
        annual_cooling_adj_kwh = float(working["cooling_electricity_kwh"].sum())

    adjusted_annual_kwh = annual_elec_kwh + annual_cooling_adj_kwh
    base_eui = annual_elec_kwh / floor_area_m2 if floor_area_m2 else None
    adjusted_eui = adjusted_annual_kwh / floor_area_m2 if floor_area_m2 else None
    return adjusted_annual_kwh, base_eui, adjusted_eui


# ══════════════════════════════════════════════════════════════════════════
# CHARTS
# ══════════════════════════════════════════════════════════════════════════

def chart_monthly_energy(data: dict) -> object:
    """
    Stacked bar: monthly cooling / equipment / lighting loads — Fig 6 palette.
    For district-cooled buildings, cooling uses electrical-equivalent cooling
    (`cooling_elec_adj_kwh`) so the visual matches adjusted EUI reporting.
    """
    b  = data["building"]
    df = data.get("monthly")
    if df is None:
        return None

    working = df.copy()
    if "month_name" in working.columns:
        working = working[working["month_name"].astype(str).str.upper() != "ANNUAL"].copy()
    if working.empty:
        return None

    if "cooling_elec_adj_kwh" in working.columns:
        cooling = working["cooling_elec_adj_kwh"].values
    elif "cooling_thermal_kwh" in working.columns:
        cooling = (working["cooling_thermal_kwh"].values / 4.5)
    else:
        cc = _cool_col(working)
        cooling = working[cc].values if cc else np.zeros(len(working))

    if "lighting_kwh" in working.columns or "exterior_lights_kwh" in working.columns:
        lighting = working.get("lighting_kwh", 0).values if "lighting_kwh" in working.columns else np.zeros(len(working))
        if "exterior_lights_kwh" in working.columns:
            lighting = lighting + working["exterior_lights_kwh"].values
    else:
        lighting = np.zeros(len(working))

    if any(c in working.columns for c in ["equipment_kwh", "fans_kwh", "pumps_kwh", "heat_rejection_kwh"]):
        equipment = np.zeros(len(working))
        for col in ["equipment_kwh", "fans_kwh", "pumps_kwh", "heat_rejection_kwh"]:
            if col in working.columns:
                equipment = equipment + working[col].values
    else:
        equipment = np.zeros(len(working))

    total = cooling + equipment + lighting

    # Trim to 12 months
    n = min(len(total), len(MONTHS))
    x = np.arange(n)
    cooling = cooling[:n]
    equipment = equipment[:n]
    lighting = lighting[:n]

    fig, ax = plt.subplots(figsize=(10, 4.2), dpi=100)
    fig.patch.set_facecolor("white")

    # Three-layer stacked bars with black outlines (Fig 6 style)
    ax.bar(x, cooling,   width=0.65, color=C_COOL,     linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2, label="Cooling")
    ax.bar(x, equipment, width=0.65, color=C_EQUIP,    linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2, label="Equipment", bottom=cooling)
    ax.bar(x, lighting,  width=0.65, color=C_LIGHT_EU, linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2, label="Lighting",  bottom=cooling + equipment)

    # Ground truth overlay
    mape_df = data.get("mape")
    if mape_df is not None and "measured_kwh" in mape_df.columns:
        measured = mape_df["measured_kwh"].values[:n]
        ax.plot(x[:len(measured)], measured, "o--",
                color="#333333", linewidth=1.2, markersize=4.5,
                label="Measured (ground truth)", zorder=5)

    ax.set_xticks(x)
    ax.set_xticklabels(MONTHS[:n], fontsize=9.5)
    ax.set_ylabel("Energy (kWh)", fontsize=10.5)
    dark_color = DARK.hexval() if hasattr(DARK, 'hexval') else "#2C3E50"
    if isinstance(dark_color, str) and dark_color.startswith("0x"):
        dark_color = "#" + dark_color[2:]
    ax.set_title(f"{b} — Monthly Energy Consumption", fontsize=12,
                 fontweight="bold", pad=8, color=dark_color)
    ax.legend(fontsize=8.5, loc="upper right", framealpha=0.9,
              edgecolor="#cccccc", handlelength=1.4)
    ax.grid(axis="y", color="#e8e8e8", linewidth=0.5, zorder=0)
    ax.set_axisbelow(True)
    ax.axhline(0, color="black", linewidth=0.8, zorder=6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)

    plt.tight_layout()
    path = str(CHARTS_DIR / f"{b}_monthly_energy.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path


def chart_mape(data: dict) -> object:
    """
    Bar chart: monthly APE % with ASHRAE 10% threshold.
    Green = calibrated (<10%), coral = flagged (≥10%) — Fig 6/9 palette.
    """
    b       = data["building"]
    mape_df = data.get("mape")
    if mape_df is None or "ape" not in mape_df.columns:
        return None

    ape  = mape_df["ape"].values
    n    = min(len(ape), len(MONTHS))
    x    = np.arange(n)
    ape  = ape[:n]
    mean = np.mean(ape) if len(ape) else 0

    # Bar colours: calibrated = Fig 9 SLE navy, flagged = Fig 9 above-GoldPLUS coral
    bar_colors = [C_T3 if v < 10 else C_ABOVE_GP for v in ape]

    fig, ax = plt.subplots(figsize=(10, 3.8), dpi=100)
    fig.patch.set_facecolor("white")
    ax.bar(x, ape, color=bar_colors, width=0.65,
           linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, alpha=0.9, zorder=2)
    ax.axhline(10,   color=C_EQUIP, linewidth=1.5, linestyle="--",
               label="ASHRAE threshold (10%)", zorder=4)
    gray_color = GRAY.hexval() if hasattr(GRAY, 'hexval') else "#555555"
    if isinstance(gray_color, str) and gray_color.startswith("0x"):
        gray_color = "#" + gray_color[2:]
    ax.axhline(mean, color=gray_color,
               linewidth=1.0, linestyle=":", zorder=4,
               label=f"Mean MAPE = {mean:.1f}%")

    ax.set_xticks(x)
    ax.set_xticklabels(MONTHS[:n], fontsize=9.5)
    ax.set_ylabel("APE (%)", fontsize=10.5)
    ax.set_title(f"{b} — Monthly Absolute Percentage Error", fontsize=12,
                 fontweight="bold", pad=8)
    ax.legend(fontsize=8.5, framealpha=0.9, edgecolor="#cccccc", handlelength=1.4)
    ax.grid(axis="y", color="#e8e8e8", linewidth=0.5, zorder=0)
    ax.set_axisbelow(True)
    ax.axhline(0, color="black", linewidth=0.8, zorder=6)
    ax.spines["top"].set_visible(False)
    ax.spines["right"].set_visible(False)
    ax.tick_params(axis="y", labelsize=9)

    plt.tight_layout()
    path = str(CHARTS_DIR / f"{b}_mape.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path


def chart_carbon_bca(data: dict, floor_area_m2=None) -> object:
    """
    Left panel : EUI vs BCA Green Mark tiers — Fig 9 compliance colours.
    Right panel: monthly carbon stacked bar (cooling/equipment/lighting) — Fig 6 palette.
    """
    b  = data["building"]
    if floor_area_m2 is None:
        floor_area_m2 = floor_area(b)
    df = data.get("monthly")
    if df is None:
        return None

    ec = _elec_col(df)
    if not ec:
        return None

    annual_kwh = df[ec].sum()
    adjusted_annual_kwh, base_eui, adjusted_eui = _adjusted_eui_metrics(df, floor_area_m2)
    eui = adjusted_eui or (annual_kwh / floor_area_m2)

    # Monthly carbon — stacked by end-use when possible
    cc        = _cool_col(df)
    equip_col = next((c for c in df.columns if "equipment" in c.lower()), None)
    light_col = next((c for c in df.columns if "lighting"  in c.lower()), None)
    n = min(len(df), len(MONTHS))

    cool_kwh = np.zeros(n)
    if "cooling_elec_adj_kwh" in df.columns:
        cool_kwh = df["cooling_elec_adj_kwh"].values[:n]
    elif "district_cooling_kwh" in df.columns:
        cool_kwh = df["district_cooling_kwh"].values[:n] / 4.5
    elif "cooling_thermal_kwh" in df.columns:
        cool_kwh = df["cooling_thermal_kwh"].values[:n] / 4.5
    elif cc:
        cool_kwh = df[cc].values[:n]
    equip_kwh = df[equip_col].values[:n] if equip_col else np.zeros(n)
    light_kwh = df[light_col].values[:n] if light_col else np.zeros(n)

    cool_co2  = cool_kwh  * GRID_FACTOR_KG_PER_KWH / 1000
    equip_co2 = equip_kwh * GRID_FACTOR_KG_PER_KWH / 1000
    light_co2 = light_kwh * GRID_FACTOR_KG_PER_KWH / 1000

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(13, 4.2), dpi=100)
    fig.patch.set_facecolor("white")
    x = np.arange(n)

    # ── Left: EUI vs BCA tiers (Fig 9 four-band colour + typology thresholds) ──
    b_typ    = _building_typology(b)
    gp, pl, sl = BCA_THRESHOLDS.get(b_typ, (130, 120, 90))
    is_proxy = b_typ in BCA_PROXY_TYPOLOGIES

    # Horizontal band bars — worst (bottom) → best (top), Fig 9 colours
    band_defs = [
        ('above_goldplus', 0,    gp,    BCA_BAND_COLORS_HEX['above_goldplus'], f'> GoldPLUS (>{gp})'),
        ('goldplus',       sl,   gp,    BCA_BAND_COLORS_HEX['goldplus'],       f'GoldPLUS (≤{gp})'),
        ('platinum',       sl,   pl,    BCA_BAND_COLORS_HEX['platinum'],       f'Platinum (≤{pl})'),
        ('sle',            0,    sl,    BCA_BAND_COLORS_HEX['sle'],            f'SLE (≤{sl})'),
    ]
    for band_key, y0, y1, hex_c, lbl in band_defs:
        ax1.axhspan(y0, y1, alpha=0.18, color=hex_c, zorder=0)
        ax1.text(max(eui, gp) * 1.01, (y0 + y1) / 2, lbl,
                 fontsize=7, color=hex_c, va='center', clip_on=True)

    ax1.axvline(eui, color=BCA_BAND_COLORS_HEX[get_band(eui, b_typ)],
                linewidth=2.0, label=f"Adjusted EUI = {eui:.1f} kWh/m²/yr", zorder=4)
    ax1.axvspan(0, eui, alpha=0.07,
                color=BCA_BAND_COLORS_HEX[get_band(eui, b_typ)], zorder=1)

    achieved_band  = get_band(eui, b_typ)
    achieved_label = bca_band_label(achieved_band)
    proxy_note     = "  * IHL proxy" if is_proxy else ""

    ax1.set_xlabel("Adjusted EUI (kWh/m²/yr)", fontsize=9.5)
    ax1.set_title(f"BCA GM:2021 Compliance — {b_typ}{proxy_note}",
                  fontsize=10.5, fontweight="bold", pad=6)
    ax1.legend(fontsize=8.5, framealpha=0.9, edgecolor="#cccccc")
    ax1.text(0.98, 0.05, achieved_label,
             transform=ax1.transAxes, ha="right", va="bottom", fontsize=9.5,
             color=BCA_BAND_COLORS_HEX[achieved_band], fontweight="bold")
    ax1.spines["top"].set_visible(False)
    ax1.spines["right"].set_visible(False)
    ax1.grid(axis="x", color="#e8e8e8", linewidth=0.5, zorder=0)
    ax1.tick_params(labelsize=8.5)

    # ── Right: monthly carbon stacked bar (Fig 6 colour scheme) ──────────
    ax2.bar(x, cool_co2,  width=0.65, color=C_COOL,     linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2, label="Cooling")
    ax2.bar(x, equip_co2, width=0.65, color=C_EQUIP,    linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2, label="Equipment", bottom=cool_co2)
    ax2.bar(x, light_co2, width=0.65, color=C_LIGHT_EU, linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2, label="Lighting",  bottom=cool_co2 + equip_co2)
    ax2.set_xticks(x)
    ax2.set_xticklabels(MONTHS[:n], fontsize=8.5)
    ax2.set_ylabel("Carbon (tCO₂e/month)", fontsize=9.5)
    ax2.set_title("Monthly Carbon Emissions", fontsize=11, fontweight="bold", pad=6)
    ax2.legend(fontsize=8, framealpha=0.9, edgecolor="#cccccc",
               handlelength=1.4, loc="upper right")
    ax2.grid(axis="y", color="#e8e8e8", linewidth=0.5, zorder=0)
    ax2.set_axisbelow(True)
    ax2.axhline(0, color="black", linewidth=0.8, zorder=6)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_visible(False)
    ax2.tick_params(labelsize=8.5)
    annual_co2 = (cool_co2 + equip_co2 + light_co2).sum()
    ax2.text(0.98, 0.95, f"Annual: {annual_co2:.1f} tCO₂e",
             transform=ax2.transAxes, ha="right", va="top",
             fontsize=9, color=C_BASE, fontweight="bold")

    plt.suptitle(f"{b} — Carbon Intensity & BCA Green Mark",
                 fontsize=12.5, fontweight="bold", y=1.02)
    plt.tight_layout()
    path = str(CHARTS_DIR / f"{b}_carbon_bca.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path


def chart_campus_summary(all_data) -> object:
    """
    Campus-wide MAPE per building (top) + EUI comparison vs BCA tiers (bottom).
    Colour scheme matches Fig 6 typology strips and Fig 8/9 compliance bands.
    """
    buildings, mapes, euis, typologies, statuses = [], [], [], [], []

    for d in all_data:
        b       = d["building"]
        mape_df = d.get("mape")
        monthly = d.get("monthly")
        mape_val = mape_df["ape"].mean() if (mape_df is not None and "ape" in mape_df.columns) else None
        ec       = _elec_col(monthly) if monthly is not None else None
        fa_b     = floor_area(d["building"])
        adj_tot, _, adj_eui = _adjusted_eui_metrics(monthly, fa_b) if monthly is not None else (None, None, None)
        eui_val  = adj_eui or (float(monthly[ec].sum()) / fa_b if (ec and monthly is not None) else None)

        buildings.append(b)
        mapes.append(mape_val)
        euis.append(eui_val)
        typologies.append(_building_typology(b))
        statuses.append("ok" if (mape_val and mape_val < 10) else "flag")

    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(14, 9), dpi=100)
    fig.patch.set_facecolor("white")
    x = np.arange(len(buildings))

    # ── Top: MAPE with Fig 6 end-use colours ─────────────────────────────
    bar_colors_mape = [C_T3 if s == "ok" else C_ABOVE_GP for s in statuses]
    ax1.bar(x, [m or 0 for m in mapes], color=bar_colors_mape, width=0.65,
            linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2)
    ax1.axhline(10, color=C_EQUIP, linewidth=1.5, linestyle="--",
                label="ASHRAE 10%", zorder=4)
    ax1.set_xticks(x)
    ax1.set_xticklabels(buildings, rotation=45, ha="right", fontsize=8.5)
    ax1.set_ylabel("MAPE (%)", fontsize=10.5)
    ax1.set_title("Campus Calibration Status — MAPE per Building",
                  fontsize=12, fontweight="bold", pad=8)
    import matplotlib.patches as _mp
    ok_patch  = _mp.Patch(facecolor=C_T3,      edgecolor=C_BAR_EDGE, linewidth=0.4, label="Calibrated (< 10%)")
    bad_patch = _mp.Patch(facecolor=C_ABOVE_GP, edgecolor=C_BAR_EDGE, linewidth=0.4, label="Needs recalibration")
    ax1.legend(handles=[ok_patch, bad_patch], fontsize=8.5,
               framealpha=0.9, edgecolor="#cccccc")
    ax1.grid(axis="y", color="#e8e8e8", linewidth=0.5, zorder=0)
    ax1.set_axisbelow(True)
    ax1.axhline(0, color="black", linewidth=0.8, zorder=6)
    ax1.spines["top"].set_visible(False)
    ax1.spines["right"].set_visible(False)
    ax1.tick_params(axis="y", labelsize=9)

    # ── Bottom: EUI with typology colours from Fig 6 TYP_BAR_COLOR ───────
    typ_bar_colors = [TYP_BAR_COLOR_HEX.get(t, "#cccccc") for t in typologies]
    ax2.bar(x, [e or 0 for e in euis], color=typ_bar_colors, width=0.65,
            linewidth=C_EDGE_LW, edgecolor=C_BAR_EDGE, zorder=2)

    # BCA tier lines — Fig 9 four-band palette, IHL thresholds as reference
    bca_ref = [
        (BCA_BENCHMARKS["GoldPLUS"], BCA_BAND_COLORS_HEX['goldplus'],  f"GoldPLUS (≤130)"),
        (BCA_BENCHMARKS["Platinum"], BCA_BAND_COLORS_HEX['platinum'],  f"Platinum (≤120)"),
        (BCA_BENCHMARKS["SLE"],      BCA_BAND_COLORS_HEX['sle'],       f"SLE (≤90)"),
    ]
    for val, col, lbl in bca_ref:
        ax2.axhline(val, linestyle="--", linewidth=1.0,
                    color=col, label=lbl, zorder=3)

    # EUI tier bands (Fig 6)
    ax2.axhline(EUI_HIGH_THRESH, color="#aaaaaa", linewidth=0.8, linestyle=":", zorder=3,
                label=f"High EUI >{EUI_HIGH_THRESH}")
    ax2.axhline(EUI_LOW_THRESH,  color="#aaaaaa", linewidth=0.8, linestyle=":", zorder=3,
                label=f"Mid/Low EUI >{EUI_LOW_THRESH}")

    ax2.set_xticks(x)
    ax2.set_xticklabels(buildings, rotation=45, ha="right", fontsize=8.5)
    ax2.set_ylabel("Adjusted EUI (kWh/m²/yr)", fontsize=10.5)
    ax2.set_title("Adjusted EUI vs. BCA Green Mark Benchmarks",
                  fontsize=12, fontweight="bold", pad=8)
    ax2.legend(fontsize=7.5, ncol=3, framealpha=0.9,
               edgecolor="#cccccc", handlelength=1.4)
    ax2.grid(axis="y", color="#e8e8e8", linewidth=0.5, zorder=0)
    ax2.set_axisbelow(True)
    ax2.axhline(0, color="black", linewidth=0.8, zorder=6)
    ax2.spines["top"].set_visible(False)
    ax2.spines["right"].set_visible(False)
    ax2.tick_params(axis="y", labelsize=9)

    plt.tight_layout(pad=2.0)
    path = str(CHARTS_DIR / "campus_summary.png")
    plt.savefig(path, dpi=150, bbox_inches="tight")
    plt.close()
    return path


# ══════════════════════════════════════════════════════════════════════════
# PDF STYLES
# ══════════════════════════════════════════════════════════════════════════

def styles():
    base = getSampleStyleSheet()
    return {
        "title":    ParagraphStyle("title",    parent=base["Title"],
                        fontSize=22, textColor=TEAL, fontName="Helvetica-Bold",
                        spaceAfter=4, alignment=TA_LEFT),
        "subtitle": ParagraphStyle("subtitle", parent=base["Normal"],
                        fontSize=9.5, textColor=colors.HexColor("#555555"),
                        fontName="Helvetica", spaceAfter=10),
        "h1":       ParagraphStyle("h1",       parent=base["Heading1"],
                        fontSize=12, textColor=TEAL, fontName="Helvetica-Bold",
                        spaceBefore=14, spaceAfter=5),
        "h2":       ParagraphStyle("h2",       parent=base["Heading2"],
                        fontSize=10.5, textColor=DARK, fontName="Helvetica-Bold",
                        spaceBefore=8, spaceAfter=4),
        "body":     ParagraphStyle("body",     parent=base["Normal"],
                        fontSize=9.5, textColor=DARK, fontName="Helvetica",
                        leading=13.5, spaceAfter=5),
        "small":    ParagraphStyle("small",    parent=base["Normal"],
                        fontSize=7.5, textColor=colors.HexColor("#555555"),
                        fontName="Helvetica", leading=11, spaceAfter=3),
        "caption":  ParagraphStyle("caption",  parent=base["Normal"],
                        fontSize=7.5, textColor=colors.HexColor("#555555"),
                        fontName="Helvetica-Oblique",
                        alignment=TA_CENTER, spaceAfter=6),
        "kpi_val":  ParagraphStyle("kpi_val",  parent=base["Normal"],
                        fontSize=19, textColor=TEAL, fontName="Helvetica-Bold",
                        alignment=TA_CENTER, leading=23),
        "kpi_lbl":  ParagraphStyle("kpi_lbl",  parent=base["Normal"],
                        fontSize=7.5, textColor=colors.HexColor("#555555"),
                        fontName="Helvetica", alignment=TA_CENTER),
    }


def _page_footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(GRAY)
    canvas.drawRightString(190*mm, 10*mm,
        f"OpenClaw | NUS Campus | Page {doc.page}")
    canvas.restoreState()


def _hr():
    return HRFlowable(width="100%", thickness=0.5,
                      color=colors.HexColor("#9dc3e6"), spaceAfter=6)  # UCC steel blue


def _make_table(rows, col_widths, last_bold=False):
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND",    (0,0),  (-1,0),  TEAL),
        ("TEXTCOLOR",     (0,0),  (-1,0),  colors.white),
        ("FONTNAME",      (0,0),  (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),  (-1,-1), 8.5),
        ("ROWBACKGROUNDS",(0,1),  (-1,-1), [colors.white, LIGHT_GRAY]),
        ("GRID",          (0,0),  (-1,-1), 0.4, colors.HexColor("#CCCCCC")),
        ("ALIGN",         (0,0),  (-1,-1), "CENTER"),
        ("VALIGN",        (0,0),  (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),  (-1,-1), 5),
        ("BOTTOMPADDING", (0,0),  (-1,-1), 5),
    ]
    if last_bold:
        style += [
            ("BACKGROUND", (0,-1), (-1,-1), TEAL_LIGHT),
            ("FONTNAME",   (0,-1), (-1,-1), "Helvetica-Bold"),
        ]
    t.setStyle(TableStyle(style))
    return t


def _callout(text, bg=None):
    bg = bg or TEAL_LIGHT
    S  = styles()
    t  = Table([[Paragraph(text, S["body"])]], colWidths=[160*mm])
    t.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (-1,-1), bg),
        ("BOX",           (0,0), (-1,-1), 0.5, TEAL),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
        ("LEFTPADDING",   (0,0), (-1,-1), 10),
    ]))
    return t


# ══════════════════════════════════════════════════════════════════════════
# KPI SCORECARD
# ══════════════════════════════════════════════════════════════════════════

def _kpi_scorecard(data: dict) -> list:
    """Returns a list of flowables: two-row KPI scorecard + EUI tier callout."""
    S       = styles()
    b       = data["building"]
    monthly = data.get("monthly")
    mape_df = data.get("mape")
    state   = data.get("pipeline_state", {})
    story   = []

    mape_val   = mape_df["ape"].mean() if (mape_df is not None and "ape" in mape_df.columns) else None
    annual_kwh = None
    cooling_kwh = None
    adjusted_annual_kwh = None
    base_eui = None
    eui_val = None
    if monthly is not None:
        ec = _elec_col(monthly)
        cc = _cool_col(monthly)
        if ec:
            annual_kwh  = float(monthly[ec].sum())
        if cc:
            cooling_kwh = float(monthly[cc].sum())

    # EUI — reported EUI must include cooling elec equivalent when available
    fa = floor_area(b)
    if monthly is not None:
        adjusted_annual_kwh, base_eui, eui_val = _adjusted_eui_metrics(monthly, fa)
    if eui_val is None and annual_kwh:
        eui_val = annual_kwh / fa
    tier     = bca_tier(eui_val, _building_typology(b)) if eui_val else "—"
    tier_col = bca_color(eui_val, _building_typology(b)) if eui_val else GRAY

    # Cooling fraction
    cool_frac = (cooling_kwh / annual_kwh * 100) if (cooling_kwh and annual_kwh) else None

    # Carbon, based on adjusted annual kWh when available
    annual_carbon = (adjusted_annual_kwh or annual_kwh) * GRID_FACTOR_KG_PER_KWH / 1000 if (adjusted_annual_kwh or annual_kwh) else None

    # Calibration status
    has_gt      = b in GROUND_TRUTH_BUILDINGS
    calibrated  = mape_val is not None and mape_val < 10
    if not has_gt:
        status_txt = "NO GT DATA"
        status_bg  = colors.HexColor("#D6EAF8")
        status_col = colors.HexColor("#1B6CA8")
    elif calibrated:
        status_txt = "CALIBRATED"
        status_bg  = GREEN_LITE
        status_col = GREEN
    else:
        status_txt = "NEEDS RECAL."
        status_bg  = RED_LITE
        status_col = RED

    iterations = state.get("recal_iteration", 0)

    # ── Row 1: EUI + Annual kWh + Cooling % + Carbon ──────────────────────
    row1 = [
        [
            Paragraph(f"{eui_val:.1f}" if eui_val else "—", S["kpi_val"]),
            Paragraph(f"{(adjusted_annual_kwh or annual_kwh)/1000:,.1f}k" if (adjusted_annual_kwh or annual_kwh) else "—", S["kpi_val"]),
            Paragraph(f"{cool_frac:.0f}%" if cool_frac else "—", S["kpi_val"]),
            Paragraph(f"{annual_carbon:.0f}" if annual_carbon else "—", S["kpi_val"]),
        ],
        [
            Paragraph("Adjusted EUI (kWh/m²/yr)", S["kpi_lbl"]),
            Paragraph("Annual kWh incl. cooling equiv", S["kpi_lbl"]),
            Paragraph("Cooling Fraction", S["kpi_lbl"]),
            Paragraph("Carbon (tCO₂e/yr)", S["kpi_lbl"]),
        ],
    ]
    t1 = Table(row1, colWidths=[40*mm]*4)
    t1.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (0,-1), colors.HexColor("#EAF4FB")),
        ("BACKGROUND",    (1,0), (1,-1), LIGHT_GRAY),
        ("BACKGROUND",    (2,0), (2,-1), colors.HexColor("#FEF5E7")),
        ("BACKGROUND",    (3,0), (3,-1), colors.HexColor("#EAFAF1")),
        ("BOX",           (0,0), (-1,-1), 1, colors.white),
        ("INNERGRID",     (0,0), (-1,-1), 1, colors.white),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(t1)
    story.append(Spacer(1, 2*mm))

    # ── Row 2: MAPE + Floor area + Iterations + Status ────────────────────
    row2 = [
        [
            Paragraph(f"{mape_val:.1f}%" if mape_val else ("N/A" if not has_gt else "—"), S["kpi_val"]),
            Paragraph(f"{fa:,.0f} m²", S["kpi_val"]),
            Paragraph(f"{iterations}", S["kpi_val"]),
            Paragraph(status_txt,
                      ParagraphStyle("sv", parent=S["kpi_val"],
                                     textColor=status_col, fontSize=13)),
        ],
        [
            Paragraph("Mean MAPE", S["kpi_lbl"]),
            Paragraph("Floor Area", S["kpi_lbl"]),
            Paragraph("Recal. Iterations", S["kpi_lbl"]),
            Paragraph("Calibration Status", S["kpi_lbl"]),
        ],
    ]
    t2 = Table(row2, colWidths=[40*mm]*4)
    t2.setStyle(TableStyle([
        ("BACKGROUND",    (0,0), (0,-1), TEAL_LIGHT),
        ("BACKGROUND",    (1,0), (1,-1), LIGHT_GRAY),
        ("BACKGROUND",    (2,0), (2,-1), AMBER_LITE),
        ("BACKGROUND",    (3,0), (3,-1), status_bg),
        ("BOX",           (0,0), (-1,-1), 1, colors.white),
        ("INNERGRID",     (0,0), (-1,-1), 1, colors.white),
        ("VALIGN",        (0,0), (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0), (-1,-1), 8),
        ("BOTTOMPADDING", (0,0), (-1,-1), 8),
    ]))
    story.append(t2)

    # ── BCA tier + EUI tier (Fig 6) + peak month callout ──────────────────
    if eui_val:
        # Peak month
        peak_month = "—"
        if monthly is not None:
            ec = _elec_col(monthly)
            if ec:
                peak_idx   = monthly[ec].values.argmax()
                peak_month = MONTHS[peak_idx % 12]
                peak_kwh   = monthly[ec].values[peak_idx]

        eui_text = f"Adjusted EUI = {eui_val:.1f} kWh/m²/yr"
        if base_eui is not None and eui_val is not None and abs(eui_val - base_eui) > 0.05:
            eui_text += f" (base elec {base_eui:.1f})"

        tier_lbl = eui_tier_label(eui_val)   # Fig 6 High / Mid / Low tier
        callout_txt = (
            f"🏢  BCA Green Mark tier: <b>{tier}</b>  |  "
            f"Portfolio EUI band: <b>{tier_lbl}</b>  |  "
            f"{eui_text}  |  "
            f"Floor area = {fa:,.0f} m²  |  "
            f"Peak month: <b>{peak_month}</b>"
            + (f" ({peak_kwh:,.0f} kWh elec)" if monthly is not None and ec else "")
        )
        story.append(Spacer(1, 2*mm))
        story.append(_callout(callout_txt, bg=eui_tier_bg(eui_val)))

    return story


# ══════════════════════════════════════════════════════════════════════════
# AGENT SUMMARY SECTION
# ══════════════════════════════════════════════════════════════════════════

def _agent_summary_section(data: dict) -> list:
    """
    Renders the executive summary and paper-ready paragraph written by
    the ReportAgent LLM — appended to calibration_log.md and shown in PDF.
    """
    S     = styles()
    state = data.get("pipeline_state", {})
    story = []

    exec_summary = state.get("diagnosis", {}).get("executive_summary", "")
    paper_para   = state.get("diagnosis", {}).get("paper_result_paragraph", "")

    # These are written by ReportAgent._append_to_log into the MD file.
    # Try to read the last entry from calibration_log.md instead.
    md_path = (REPORTS_DIR / data["building"]
               / f"{data['building']}_calibration_log.md")
    if md_path.exists():
        text = md_path.read_text(encoding="utf-8")
        if "**Executive Summary:**" in text:
            parts = text.split("**Executive Summary:**")
            last  = parts[-1].strip()
            exec_summary = last.split("**Paper-Ready Result:**")[0].strip()
        if "**Paper-Ready Result:**" in text:
            parts      = text.split("**Paper-Ready Result:**")
            paper_para = parts[-1].split("_Generated by")[0].strip()

    if exec_summary:
        story.append(Paragraph("Executive Summary (Facilities Team)", S["h2"]))
        story.append(_callout(exec_summary, bg=TEAL_LIGHT))
        story.append(Spacer(1, 4*mm))

    if paper_para:
        story.append(Paragraph("Paper-Ready Result (Applied Energy)", S["h2"]))
        story.append(_callout(paper_para, bg=GREEN_LITE))
        story.append(Spacer(1, 4*mm))

    return story


# ══════════════════════════════════════════════════════════════════════════
# FAULT DETECTION TABLE
# ══════════════════════════════════════════════════════════════════════════

def _fault_table(data: dict) -> list:
    S       = styles()
    mape_df = data.get("mape")
    state   = data.get("pipeline_state", {})

    if mape_df is None or "ape" not in mape_df.columns:
        return [_callout("No ground truth data yet. Add NUS OED meter data to enable MAPE.", AMBER_LITE)]

    flagged = mape_df[mape_df["ape"] > 10]
    if flagged.empty:
        return [_callout("No months exceeded MAPE 10%. Building is fully calibrated.", GREEN_LITE)]

    # Pull diagnosis from pipeline state if available
    diagnosis  = state.get("diagnosis", {})
    hypotheses = diagnosis.get("hypotheses", [])

    rows = [["Month", "APE (%)", "Error (kWh)", "Agent Diagnosis", "Recommended Action"]]
    for i, (idx, row) in enumerate(flagged.iterrows()):
        midx   = int(row.name) if str(row.name).isdigit() else i
        cause  = hypotheses[i]["plain_english"] if i < len(hypotheses) else "Refer to Diagnosis Agent"
        action = hypotheses[i].get("direction","—") if i < len(hypotheses) else "Run agents.py"
        rows.append([
            MONTHS[midx % 12],
            f"{row['ape']:.1f}%",
            f"{row.get('error_kwh', 0):+,.0f}",
            cause,
            action,
        ])

    col_w = [18*mm, 18*mm, 26*mm, 56*mm, 42*mm]
    return [
        Paragraph(f"⚠  {len(flagged)} month(s) flagged by Anomaly Agent:", S["h2"]),
        _make_table(rows, col_w),
    ]


# ══════════════════════════════════════════════════════════════════════════
# CALIBRATION LOG TABLE
# ══════════════════════════════════════════════════════════════════════════

def _cal_log_table(data: dict) -> list:
    S       = styles()
    cal_log = data.get("cal_log", {})
    iters   = cal_log.get("iterations", [])

    if not iters:
        return [_callout(
            "No recalibration iterations yet. Pipeline will populate this table "
            "as the Recalibration Agent proposes and humans approve IDF changes.",
            LIGHT_GRAY
        )]

    rows = [["Iter.", "Date", "Parameter", "Old Value", "New Value",
             "MAPE Before", "Approved By"]]
    for it in iters:
        changes = it.get("changes", [{}])
        for ch in changes:
            rows.append([
                str(it.get("iteration", "")),
                str(it.get("date", "")),
                ch.get("idf_field", ""),
                str(ch.get("current_value", "")),
                str(ch.get("proposed_value", "")),
                f"{it.get('mape_before', '—'):.1f}%" if isinstance(it.get("mape_before"), float) else "—",
                str(it.get("approved_by", "Human (Slack)")),
            ])

    col_w = [14*mm, 22*mm, 40*mm, 22*mm, 22*mm, 22*mm, 28*mm]
    return [
        Paragraph("IDF Parameter Changes (Human-Approved)", S["h2"]),
        _make_table(rows, col_w),
    ]


def _intervention_section(data: dict) -> list:
    S = styles()
    carbon = data.get("carbon")
    if not carbon:
        return [_callout(
            "No intervention analysis found for this building. Run Compass to generate carbon scenarios first.",
            LIGHT_GRAY
        )]

    story = []
    baseline = carbon.get("baseline", {})
    fingerprint = carbon.get("idf_fingerprint", {})
    scenarios = carbon.get("scenarios", []) or []
    flags = carbon.get("flags", []) or []

    baseline_kwh = baseline.get("annual_kwh")
    baseline_tco2e = baseline.get("annual_tco2e")
    baseline_eui = baseline.get("eui_kwh_m2")
    baseline_base_eui = baseline.get("base_eui_kwh_m2")
    baseline_bca = baseline.get("bca_rating")

    monthly = data.get("monthly")
    fa = floor_area(data.get("building", ""))
    adjusted_annual_kwh, computed_base_eui, computed_adjusted_eui = _adjusted_eui_metrics(monthly, fa) if monthly is not None else (None, None, None)
    if computed_adjusted_eui is not None:
        baseline_eui = computed_adjusted_eui
    if computed_base_eui is not None:
        baseline_base_eui = computed_base_eui
    if adjusted_annual_kwh is not None:
        baseline_kwh = adjusted_annual_kwh
    if adjusted_annual_kwh is not None:
        baseline_tco2e = adjusted_annual_kwh * GRID_FACTOR_KG_PER_KWH / 1000
    if baseline_eui is not None:
        baseline_bca = bca_tier(baseline_eui, _building_typology(data.get("building", "")))

    summary_bits = []
    if baseline_kwh is not None:
        summary_bits.append(f"Annual baseline incl. cooling: <b>{baseline_kwh:,.0f} kWh</b>")
    if baseline_tco2e is not None:
        summary_bits.append(f"Carbon incl. cooling: <b>{baseline_tco2e:,.1f} tCO2e/yr</b>")
    if baseline_eui is not None:
        summary_bits.append(f"Adjusted EUI: <b>{baseline_eui:.1f} kWh/m²/yr</b>")
    if baseline_base_eui is not None:
        summary_bits.append(f"Base electrical EUI: <b>{baseline_base_eui:.1f} kWh/m²/yr</b>")
    if baseline_bca:
        summary_bits.append(f"BCA (adjusted): <b>{baseline_bca}</b>")
    if summary_bits:
        story.append(_callout("  |  ".join(summary_bits), bg=GREEN_LITE))
        story.append(Spacer(1, 3*mm))

    fp_lines = []
    if "cooling_setpoint_c" in fingerprint:
        fp_lines.append(f"Cooling SP {fingerprint['cooling_setpoint_c']}°C")
    if "lighting_lpd_wm2" in fingerprint:
        fp_lines.append(f"LPD {fingerprint['lighting_lpd_wm2']} W/m²")
    if "equipment_epd_wm2" in fingerprint:
        fp_lines.append(f"Equipment {fingerprint['equipment_epd_wm2']} W/m²")
    if "infiltration_ach" in fingerprint:
        fp_lines.append(f"Infiltration {fingerprint['infiltration_ach']} ACH")
    if fingerprint.get("hvac_type"):
        fp_lines.append(f"HVAC {fingerprint['hvac_type']}")
    if fp_lines:
        story.append(Paragraph("Intervention baseline fingerprint", S["h2"]))
        story.append(_callout("  |  ".join(fp_lines), bg=TEAL_LIGHT))
        story.append(Spacer(1, 3*mm))

    if scenarios:
        rows = [["Scenario", "Reduction", "CO2 saved", "Capex", "SGD/m²", "Source"]]
        for s in scenarios:
            capex = s.get("capex_summary", {}) or {}
            rows.append([
                s.get("label", s.get("id", "—")),
                f"{s.get('reduction_pct', 0):.0f}%" if isinstance(s.get("reduction_pct"), (int, float)) else "—",
                f"{s.get('co2_saved_tco2e', 0):,.1f}" if isinstance(s.get("co2_saved_tco2e"), (int, float)) else "—",
                capex.get("label", "—"),
                f"{capex.get('cost_per_m2_gfa', 0):,.0f}" if isinstance(capex.get("cost_per_m2_gfa"), (int, float)) else "—",
                ", ".join(s.get("sources", [])) or "—",
            ])
        story.append(Paragraph("Scenario summary", S["h2"]))
        story.append(_make_table(rows, [28*mm, 20*mm, 24*mm, 38*mm, 18*mm, 32*mm]))
        story.append(Spacer(1, 3*mm))

        story.append(Paragraph("Scenario strategy used", S["h2"]))
        for s in scenarios:
            title = s.get('label', s.get('id', 'Scenario'))
            expl = s.get("explanation") or "No strategy explanation provided."
            tradeoff = s.get("tradeoff")
            source_line = ", ".join(s.get("sources", [])) or "—"
            block = f"<b>{title}</b><br/>{expl}<br/><br/><b>Source:</b> {source_line}"
            if tradeoff:
                block += f"<br/><b>Trade-off:</b> {tradeoff}"
            story.append(_callout(block, bg=TEAL_LIGHT if title.lower().startswith('zero') else AMBER_LITE if title.lower().startswith('medium') else RED_LITE))
            story.append(Spacer(1, 3*mm))

        best = max(
            scenarios,
            key=lambda s: s.get("co2_saved_tco2e", 0) if isinstance(s.get("co2_saved_tco2e"), (int, float)) else -1
        )
        expl = best.get("explanation") or best.get("tradeoff")
        if expl:
            story.append(Paragraph(f"Recommended pathway: {best.get('label', best.get('id', 'Scenario'))}", S["h2"]))
            story.append(_callout(expl, bg=AMBER_LITE))
            story.append(Spacer(1, 3*mm))

    if flags:
        story.append(Paragraph("Intervention flags", S["h2"]))
        story.append(_callout("<br/>".join(f"• {f}" for f in flags), bg=RED_LITE))

    return story


# ══════════════════════════════════════════════════════════════════════════
# BUILD BUILDING PDF
# ══════════════════════════════════════════════════════════════════════════

def build_building_pdf(data: dict, out_path: str):
    building = data["building"]
    _clear_building_chart_cache(building)
    S        = styles()
    story    = []

    # ── Header ────────────────────────────────────────────────────────────
    story.append(Paragraph("OPENCLAW — NUS Campus Energy Management", S["subtitle"]))
    story.append(Paragraph(f"Building Report — {building}", S["title"]))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}  |  "
        f"EnergyPlus 23.x  |  Singapore EPW (site-calibrated by Nimbus where available)  |  ASHRAE Guideline 14",
        S["subtitle"]
    ))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL, spaceAfter=10))

    # ── Explicit reported EUI headline ────────────────────────────────────
    monthly = data.get("monthly")
    fa = floor_area(building)
    if monthly is not None:
        _, _, headline_eui = _adjusted_eui_metrics(monthly, fa)
        if headline_eui is not None:
            story.append(_callout(
                f"<b>REPORTED ADJUSTED EUI: {headline_eui:.1f} kWh/m²/yr"
                f"  |  {eui_tier_label(headline_eui)}</b>",
                bg=eui_tier_bg(headline_eui)
            ))
            story.append(Spacer(1, 4*mm))

    # ── KPI scorecard ─────────────────────────────────────────────────────
    story += _kpi_scorecard(data)
    story.append(Spacer(1, 6*mm))

    # ── Agent summaries (written by ReportAgent LLM) ──────────────────────
    agent_section = _agent_summary_section(data)
    if agent_section:
        story.append(Paragraph("0. OpenClaw Agent Summary", S["h1"]))
        story.append(_hr())
        story += agent_section

    # ── Section 1: Monthly energy ─────────────────────────────────────────
    story.append(Paragraph("1. Monthly Energy Consumption", S["h1"]))
    story.append(_hr())
    story.append(Paragraph(
        "Monthly simulated electricity broken into cooling and other loads. "
        "Red markers show measured ground truth where available.",
        S["body"]
    ))
    c1 = chart_monthly_energy(data)
    if c1:
        story.append(Image(c1, width=160*mm, height=68*mm))
        story.append(Paragraph("Figure 1. Monthly electricity — simulated vs. measured.", S["caption"]))
    story.append(Spacer(1, 4*mm))

    # ── Section 2: Fault detection ────────────────────────────────────────
    story.append(Paragraph("2. Fault Detection & Anomaly Agent Results", S["h1"]))
    story.append(_hr())
    story.append(Paragraph(
        "Monthly APE between simulated and measured energy. "
        "Months exceeding ASHRAE Guideline 14 threshold (MAPE > 10%) "
        "are flagged red and queued for the Diagnosis Agent.",
        S["body"]
    ))
    c2 = chart_mape(data)
    if c2:
        story.append(Image(c2, width=160*mm, height=62*mm))
        story.append(Paragraph("Figure 2. Monthly APE (%) with ASHRAE 10% threshold.", S["caption"]))
    story += _fault_table(data)
    story.append(Spacer(1, 4*mm))

    # ── Section 3: Carbon & BCA ───────────────────────────────────────────
    story.append(Paragraph("3. Carbon Intensity & BCA Green Mark", S["h1"]))
    story.append(_hr())
    story.append(Paragraph(
        "Reported adjusted EUI here means total EUI including cooling electrical equivalent, used for BCA comparison. "
        "Carbon is also calculated on the adjusted total using Singapore grid factor 0.4168 kgCO2e/kWh (EMA 2023). "
        "Floor area comes from building_registry.json.",
        S["body"]
    ))
    c3 = chart_carbon_bca(data)
    if c3:
        story.append(Image(c3, width=170*mm, height=65*mm))
        story.append(Paragraph("Figure 3. Adjusted EUI vs. BCA tiers, and monthly carbon including cooling equivalent.", S["caption"]))
    story.append(Spacer(1, 4*mm))

    # ── Section 4: Recalibration log ──────────────────────────────────────
    story.append(Paragraph("4. Recalibration Log (Human-Approved Changes)", S["h1"]))
    story.append(_hr())
    story += _cal_log_table(data)
    story.append(Spacer(1, 4*mm))

    # ── Section 5: Intervention analysis ──────────────────────────────────
    story.append(Paragraph("5. Intervention Analysis & Carbon Scenarios", S["h1"]))
    story.append(_hr())
    story.append(Paragraph(
        "Operational and retrofit scenarios produced by Compass. "
        "Scenario outputs should be treated as indicative when the baseline is uncalibrated.",
        S["body"]
    ))
    story += _intervention_section(data)

    # ── Footer ────────────────────────────────────────────────────────────
    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=TEAL_LIGHT, spaceAfter=4))
    story.append(Paragraph(
        "Generated by OpenClaw — multi-agent LLM framework for NUS campus energy management. "
        "Calibration standard: ASHRAE Guideline 14.",
        S["small"]
    ))

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title=f"OpenClaw — {building}",
        author="OpenClaw / NUS",
    )
    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    print(f"  [PDF] {out_path}")


# ══════════════════════════════════════════════════════════════════════════
# BUILD CALIBRATION LOG MD
# ══════════════════════════════════════════════════════════════════════════

def build_calibration_log_md(data: dict, out_path: str):
    """
    Write calibration_log.md for this building.
    ReportAgent._append_to_log() will later append the LLM-generated
    executive summary and paper paragraph to this file.
    """
    building = data["building"]
    monthly  = data.get("monthly")
    mape_df  = data.get("mape")
    cal_log  = data.get("cal_log", {})
    state    = data.get("pipeline_state", {})
    ts       = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    mape_val = mape_df["ape"].mean() if (mape_df is not None and "ape" in mape_df.columns) else None
    calibrated = mape_val is not None and mape_val < 10

    lines = [
        f"# Calibration Log — {building}",
        f"",
        f"| Field | Value |",
        f"|-------|-------|",
        f"| Building | {building} |",
        f"| Generated | {ts} |",
        f"| Simulation | EnergyPlus 23.x |",
        f"| Weather | Site-calibrated EPW (Nimbus 🌦️) if available; base TMY Changi 486980 IWEC fallback |",
        f"| Calibration standard | ASHRAE Guideline 14 |",
        f"| Research target | Applied Energy (Q1, Elsevier) |",
        f"",
        f"---",
        f"",
        f"## Calibration Status",
        f"",
    ]

    if mape_val is not None:
        status = "CALIBRATED" if calibrated else "NEEDS RECALIBRATION"
        lines += [
            f"| Metric | Value | Threshold | Status |",
            f"|--------|-------|-----------|--------|",
            f"| MAPE   | {mape_val:.2f}% | < 10% | {status} |",
        ]
        if mape_df is not None and "error_kwh" in mape_df.columns and "measured_kwh" in mape_df.columns:
            rmse   = np.sqrt((mape_df["error_kwh"]**2).mean())
            mean_m = mape_df["measured_kwh"].mean()
            cvrmse = rmse / mean_m * 100
            cv_st  = "CALIBRATED" if cvrmse < 30 else "NEEDS RECALIBRATION"
            lines.append(f"| CVRMSE | {cvrmse:.2f}% | < 30% | {cv_st} |")
    else:
        lines.append("_Ground truth not yet available._")

    lines += ["", "---", "", "## Monthly Energy Summary", ""]

    if monthly is not None:
        ec = _elec_col(monthly)
        monthly_rows = monthly.copy()
        if "month_name" in monthly_rows.columns:
            monthly_rows = monthly_rows[monthly_rows["month_name"].astype(str).str.upper() != "ANNUAL"].copy()

        lines += [
            "| Month | Base elec (kWh) | Cooling equiv (kWh) | Total incl. cooling (kWh) | Carbon (tCO2e) |",
            "|-------|------------------|---------------------|----------------------------|----------------|",
        ]
        for i, (_, row) in enumerate(monthly_rows.iterrows()):
            base_total = row.get("electricity_facility_kwh", row.get(ec, 0) if ec else 0)
            cooling_adj = row.get("cooling_elec_adj_kwh", 0)
            total = base_total + cooling_adj
            carbon  = total * GRID_FACTOR_KG_PER_KWH / 1000
            lines.append(
                f"| {MONTHS[i] if i<12 else i} | {base_total:,.0f} | "
                f"{cooling_adj:,.0f} | {total:,.0f} | {carbon:.2f} |"
            )
        if ec:
            adjusted_annual_kwh, base_eui, adjusted_eui = _adjusted_eui_metrics(monthly_rows, floor_area(building))
            annual = adjusted_annual_kwh or monthly_rows[ec].sum()
            lines += [
                "",
                f"**Annual total incl. cooling:** {annual:,.0f} kWh  |  "
                f"**Carbon:** {annual*GRID_FACTOR_KG_PER_KWH/1000:.1f} tCO2e  |  "
                f"**Reported EUI:** {adjusted_eui:.1f} kWh/m²/yr ({floor_area(building):,.0f} m²)"
                if adjusted_eui is not None else
                f"**Annual total:** {annual:,.0f} kWh  |  **Carbon:** {annual*GRID_FACTOR_KG_PER_KWH/1000:.1f} tCO2e"
            ]
            if base_eui is not None and adjusted_eui is not None:
                lines.append(f"**Base electrical EUI:** {base_eui:.1f} kWh/m²/yr  |  **Cooling-adjusted add-on:** {adjusted_eui - base_eui:.1f} kWh/m²/yr")

    lines += ["", "---", "", "## Flagged Months (Anomaly Agent)", ""]

    if mape_df is not None and "ape" in mape_df.columns:
        flagged = mape_df[mape_df["ape"] > 10]
        if flagged.empty:
            lines.append("_No months flagged. Building calibrated._")
        else:
            lines += [
                "| Month | APE (%) | Error (kWh) |",
                "|-------|---------|-------------|",
            ]
            for i, (_, row) in enumerate(flagged.iterrows()):
                midx = int(row.name) if str(row.name).isdigit() else i
                lines.append(
                    f"| {MONTHS[midx%12]} | {row['ape']:.1f}% | "
                    f"{row.get('error_kwh',0):+,.0f} |"
                )

    lines += ["", "---", "", "## IDF Parameter Changes", ""]

    iters = cal_log.get("iterations", [])
    if not iters:
        lines += [
            "| Iter. | Date | Parameter | Old | New | MAPE Before | Approved By |",
            "|-------|------|-----------|-----|-----|-------------|-------------|",
            "| 0 (baseline) | — | — | — | — | — | — |",
        ]
    else:
        lines += [
            "| Iter. | Date | Parameter | Old | New | MAPE Before | Approved By |",
            "|-------|------|-----------|-----|-----|-------------|-------------|",
        ]
        for it in iters:
            for ch in it.get("changes", [{}]):
                mb = it.get("mape_before")
                lines.append(
                    f"| {it.get('iteration','')} | {it.get('date','')} | "
                    f"{ch.get('idf_field','')} | {ch.get('current_value','')} | "
                    f"{ch.get('proposed_value','')} | "
                    f"{f'{mb:.1f}%' if isinstance(mb, float) else '—'} | "
                    f"{it.get('approved_by','Human (Slack)')} |"
                )

    carbon = data.get("carbon")
    lines += ["", "---", "", "## Intervention Analysis", ""]
    if not carbon:
        lines.append("_No intervention analysis found for this building._")
    else:
        baseline = carbon.get("baseline", {})
        run_date = carbon.get("run_date", "—")
        monthly = data.get("monthly")
        fa = floor_area(data.get("building", ""))
        adjusted_annual_kwh, computed_base_eui, computed_adjusted_eui = _adjusted_eui_metrics(monthly, fa) if monthly is not None else (None, None, None)
        baseline_annual_kwh = adjusted_annual_kwh if adjusted_annual_kwh is not None else baseline.get('annual_kwh', '—')
        baseline_annual_carbon = (adjusted_annual_kwh * GRID_FACTOR_KG_PER_KWH / 1000) if adjusted_annual_kwh is not None else baseline.get('annual_tco2e', '—')
        baseline_reported_eui = computed_adjusted_eui if computed_adjusted_eui is not None else baseline.get('eui_kwh_m2', '—')
        baseline_base_eui = computed_base_eui if computed_base_eui is not None else baseline.get('base_eui_kwh_m2', None)
        lines.append(f"- Scenario data refreshed from carbon JSON dated: {run_date}")
        lines.append(f"- Baseline annual kWh incl. cooling: {baseline_annual_kwh}")
        lines.append(f"- Baseline annual carbon incl. cooling: {baseline_annual_carbon} tCO2e/yr")
        lines.append(f"- Reported adjusted EUI: {baseline_reported_eui} kWh/m²/yr")
        if baseline_base_eui is not None:
            lines.append(f"- Base electrical EUI: {baseline_base_eui} kWh/m²/yr")
        lines.append("")
        lines.append("| Scenario | Reduction % | CO2 saved (tCO2e/yr) | Capex | SGD/m² GFA |")
        lines.append("|----------|-------------|----------------------|-------|------------|")
        for s in carbon.get("scenarios", []) or []:
            capex = s.get("capex_summary", {}) or {}
            lines.append(
                f"| {s.get('label', s.get('id', '—'))} | "
                f"{s.get('reduction_pct', '—')} | "
                f"{s.get('co2_saved_tco2e', '—')} | "
                f"{capex.get('label', '—')} | "
                f"{capex.get('cost_per_m2_gfa', '—')} |"
            )
        flags = carbon.get("flags", []) or []
        if flags:
            lines += ["", "**Intervention flags:**"]
            lines += [f"- {flag}" for flag in flags]

    # ── ReportAgent will append its LLM summaries below this line ─────────
    lines += [
        "",
        "---",
        "",
        f"_Base log generated by report.py at {ts}._",
        f"_OpenClaw ReportAgent will append executive summary and paper paragraph below._",
    ]

    with open(out_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    print(f"  [MD ] {out_path}")


# ══════════════════════════════════════════════════════════════════════════
# BUILD CAMPUS SUMMARY PDF
# ══════════════════════════════════════════════════════════════════════════

def build_campus_summary_pdf(all_data: list, out_path: str):
    S     = styles()
    story = []

    story.append(Paragraph("OPENCLAW — NUS Campus Energy Management", S["subtitle"]))
    story.append(Paragraph("Campus-Wide Summary Report", S["title"]))
    story.append(Paragraph(
        f"Generated: {datetime.now().strftime('%d %B %Y, %H:%M')}  |  "
        f"{len(all_data)} buildings  |  EnergyPlus 23.x",
        S["subtitle"]
    ))
    story.append(HRFlowable(width="100%", thickness=2, color=TEAL, spaceAfter=10))

    # ── Campus KPIs ───────────────────────────────────────────────────────
    total_kwh = 0
    n_cal     = 0
    all_mapes = []

    for d in all_data:
        monthly = d.get("monthly")
        mape_df = d.get("mape")
        if monthly is not None:
            fa_b = floor_area(d["building"])
            adjusted_total, _, _ = _adjusted_eui_metrics(monthly, fa_b)
            ec = _elec_col(monthly)
            if adjusted_total is not None:
                total_kwh += adjusted_total
            elif ec:
                total_kwh += monthly[ec].sum()
        if mape_df is not None and "ape" in mape_df.columns:
            m = mape_df["ape"].mean()
            all_mapes.append(m)
            if m < 10:
                n_cal += 1

    campus_mape  = np.mean(all_mapes) if all_mapes else None
    total_carbon = total_kwh * GRID_FACTOR_KG_PER_KWH / 1000
    total_fa     = sum(floor_area(d["building"]) for d in all_data)
    campus_eui   = total_kwh / total_fa if total_fa else None

    # Row 1: EUI + Total kWh + Carbon + Calibrated count
    kpi1 = [
        [
            Paragraph(f"{campus_eui:.1f}" if campus_eui else "—", S["kpi_val"]),
            Paragraph(f"{total_kwh/1e6:.2f}M", S["kpi_val"]),
            Paragraph(f"{total_carbon:.0f}", S["kpi_val"]),
            Paragraph(f"{n_cal} / {len(all_data)}", S["kpi_val"]),
        ],
        [
            Paragraph("Campus adjusted EUI (kWh/m²/yr)", S["kpi_lbl"]),
            Paragraph("Annual kWh incl. cooling equiv", S["kpi_lbl"]),
            Paragraph("Annual Carbon (tCO₂e)", S["kpi_lbl"]),
            Paragraph("Buildings Calibrated", S["kpi_lbl"]),
        ],
    ]
    t1 = Table(kpi1, colWidths=[40*mm]*4)
    t1.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(0,-1), colors.HexColor("#EAF4FB")),
        ("BACKGROUND",    (1,0),(1,-1), LIGHT_GRAY),
        ("BACKGROUND",    (2,0),(2,-1), AMBER_LITE),
        ("BACKGROUND",    (3,0),(3,-1), GREEN_LITE if n_cal == len(all_data) else RED_LITE),
        ("BOX",           (0,0),(-1,-1), 1, colors.white),
        ("INNERGRID",     (0,0),(-1,-1), 1, colors.white),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),(-1,-1), 10),
        ("BOTTOMPADDING", (0,0),(-1,-1), 10),
    ]))
    story.append(t1)
    story.append(Spacer(1, 2*mm))

    # Row 2: MAPE + Total floor area + BCA campus tier + savings potential
    campus_tier = bca_tier(campus_eui, 'Engineering') if campus_eui else "—"
    best_target = BCA_BENCHMARKS["GoldPLUS"] * total_fa  # GoldPLUS IHL target
    savings_kwh = max(0, total_kwh - best_target)
    kpi2 = [
        [
            Paragraph(f"{campus_mape:.1f}%" if campus_mape else "—", S["kpi_val"]),
            Paragraph(f"{total_fa/1000:.1f}k m²", S["kpi_val"]),
            Paragraph(campus_tier, S["kpi_val"]),
            Paragraph(f"S${savings_kwh*SGD_PER_KWH/1e6:.1f}M/yr" if savings_kwh else "At target ✓",
                      S["kpi_val"]),
        ],
        [
            Paragraph("Campus Mean MAPE", S["kpi_lbl"]),
            Paragraph("Total Floor Area", S["kpi_lbl"]),
            Paragraph("BCA Campus Tier", S["kpi_lbl"]),
            Paragraph("Savings to BCA Gold", S["kpi_lbl"]),
        ],
    ]
    t2 = Table(kpi2, colWidths=[40*mm]*4)
    t2.setStyle(TableStyle([
        ("BACKGROUND",    (0,0),(0,-1), TEAL_LIGHT),
        ("BACKGROUND",    (1,0),(1,-1), LIGHT_GRAY),
        ("BACKGROUND",    (2,0),(2,-1), GREEN_LITE),
        ("BACKGROUND",    (3,0),(3,-1), AMBER_LITE),
        ("BOX",           (0,0),(-1,-1), 1, colors.white),
        ("INNERGRID",     (0,0),(-1,-1), 1, colors.white),
        ("VALIGN",        (0,0),(-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),(-1,-1), 10),
        ("BOTTOMPADDING", (0,0),(-1,-1), 10),
    ]))
    story.append(t2)
    story.append(Spacer(1, 4*mm))

    # BCA tier callout
    if campus_eui:
        story.append(_callout(
            f"🏛️  NUS Campus adjusted EUI = {campus_eui:.1f} kWh/m²/yr  |  "
            f"Portfolio band: <b>{eui_tier_label(campus_eui)}</b>  |  "
            f"BCA tier: <b>{campus_tier}</b>  |  "
            f"Total floor area: {total_fa:,.0f} m²  |  "
            f"Potential savings to BCA Gold: {savings_kwh/1e6:.2f}M kWh/yr = "
            f"S${savings_kwh*SGD_PER_KWH/1e6:.1f}M/yr = "
            f"{savings_kwh*GRID_FACTOR_KG_PER_KWH/1000:.0f} tCO₂e/yr",
            bg=eui_tier_bg(campus_eui)
        ))
    story.append(Spacer(1, 4*mm))

    # ── Campus chart ──────────────────────────────────────────────────────
    story.append(Paragraph("1. Campus Overview", S["h1"]))
    story.append(_hr())
    c = chart_campus_summary(all_data)
    if c:
        story.append(Image(c, width=170*mm, height=130*mm))
        story.append(Paragraph(
            "Figure 1. MAPE per building (top) and adjusted EUI vs. BCA Green Mark (bottom).",
            S["caption"]
        ))

    story.append(PageBreak())

    # ── Per-building table ─────────────────────────────────────────────────
    story.append(Paragraph("2. Per-Building Summary", S["h1"]))
    story.append(_hr())

    rows = [["Building", "Floor Area (m²)", "Annual kWh incl. cooling", "Adjusted EUI (kWh/m²/yr)",
              "BCA Tier", "Cooling %", "Carbon (tCO2e)", "MAPE", "Status"]]
    flagged_buildings = []
    eui_colors = []  # track for row colouring

    for d in all_data:
        b       = d["building"]
        monthly = d.get("monthly")
        mape_df = d.get("mape")
        fa      = floor_area(b)

        kwh = mape_str = eui_str = carbon_str = cool_str = "—"
        tier_str = "—"
        status = "No GT data"
        eui_val = None

        if monthly is not None:
            ec = _elec_col(monthly)
            cc = _cool_col(monthly)
            if ec:
                total      = float(monthly[ec].sum())
                adjusted_total, _, adjusted_eui = _adjusted_eui_metrics(monthly, fa)
                total_for_reporting = adjusted_total or total
                eui_val    = adjusted_eui or (total / fa)
                kwh        = f"{total_for_reporting/1000:,.1f}k"
                eui_str    = f"{eui_val:.1f}"
                carbon_str = f"{total_for_reporting*GRID_FACTOR_KG_PER_KWH/1000:.0f}"
                tier_str   = bca_tier(eui_val, _building_typology(b))
            if cc and ec:
                cool_pct  = float(monthly[cc].sum()) / float(monthly[ec].sum()) * 100
                cool_str  = f"{cool_pct:.0f}%"

        has_gt = b in GROUND_TRUTH_BUILDINGS
        if mape_df is not None and "ape" in mape_df.columns:
            m        = mape_df["ape"].mean()
            mape_str = f"{m:.1f}%"
            if has_gt:
                status = "Calibrated ✓" if m < 10 else "Needs recal."
                if m >= 10:
                    flagged_buildings.append(b)
            else:
                status = "No GT data"

        eui_colors.append(eui_val)
        rows.append([b, f"{fa:,.0f}", kwh, eui_str, tier_str, cool_str,
                     carbon_str, mape_str, status])

    col_w = [22*mm, 18*mm, 22*mm, 22*mm, 24*mm, 18*mm, 20*mm, 16*mm, 22*mm]

    # Build per-row style commands — typology background on col 0, EUI colour on cols 3-4
    extra_cmds = []
    for i, d in enumerate(all_data):
        row_idx  = i + 1  # +1 for header
        typ      = _building_typology(d["building"])
        typ_col  = _typ_color_rl(typ)
        eui_v    = eui_colors[i]

        # Typology colour swatch on Building column (col 0)
        extra_cmds.append(("BACKGROUND", (0, row_idx), (0, row_idx), typ_col))
        extra_cmds.append(("TEXTCOLOR",  (0, row_idx), (0, row_idx), colors.white))
        extra_cmds.append(("FONTNAME",   (0, row_idx), (0, row_idx), "Helvetica-Bold"))

        # EUI tier text colour on EUI + BCA tier columns — typology-aware (Fig 9)
        if eui_v:
            c = bca_color(eui_v, typ)
            extra_cmds.append(("TEXTCOLOR", (3, row_idx), (4, row_idx), c))
            extra_cmds.append(("FONTNAME",  (3, row_idx), (4, row_idx), "Helvetica-Bold"))

    # Build table manually so we can inject extra style commands
    tbl = Table(rows, colWidths=col_w, repeatRows=1)
    base_style = [
        ("BACKGROUND",    (0,0),  (-1,0),  TEAL),
        ("TEXTCOLOR",     (0,0),  (-1,0),  colors.white),
        ("FONTNAME",      (0,0),  (-1,0),  "Helvetica-Bold"),
        ("FONTSIZE",      (0,0),  (-1,-1), 7.5),
        ("ROWBACKGROUNDS",(0,1),  (-1,-1), [colors.white, LIGHT_GRAY]),
        ("GRID",          (0,0),  (-1,-1), 0.3, colors.HexColor("#e0e0e0")),
        ("ALIGN",         (0,0),  (-1,-1), "CENTER"),
        ("VALIGN",        (0,0),  (-1,-1), "MIDDLE"),
        ("TOPPADDING",    (0,0),  (-1,-1), 3.5),
        ("BOTTOMPADDING", (0,0),  (-1,-1), 3.5),
    ] + extra_cmds
    tbl.setStyle(TableStyle(base_style))

    story.append(tbl)

    if flagged_buildings:
        story.append(Spacer(1, 6*mm))
        story.append(_callout(
            f"The following {len(flagged_buildings)} building(s) are queued for "
            f"OpenClaw Stage 2 (Diagnosis + Recalibration):\n\n"
            + "  |  ".join(flagged_buildings),
            RED_LITE
        ))

    story.append(Spacer(1, 6*mm))
    story.append(HRFlowable(width="100%", thickness=0.5, color=TEAL_LIGHT, spaceAfter=4))
    story.append(Paragraph(
        "OpenClaw | NUS Campus | Applied Energy (Q1) | "
        "ASHRAE Guideline 14 | BCA Green Mark 2021 | EMA 2023 grid factor",
        S["small"]
    ))

    doc = SimpleDocTemplate(
        out_path, pagesize=A4,
        leftMargin=20*mm, rightMargin=20*mm,
        topMargin=18*mm, bottomMargin=18*mm,
        title="OpenClaw — NUS Campus Summary",
        author="OpenClaw / NUS",
    )
    doc.build(story, onFirstPage=_page_footer, onLaterPages=_page_footer)
    print(f"  [PDF] {out_path}")


# ══════════════════════════════════════════════════════════════════════════
# ENTRYPOINT — called by ReportAgent as subprocess
# ══════════════════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="OpenClaw report.py — called by ReportAgent in agents.py"
    )
    parser.add_argument("--building", help="Generate report for one building (e.g. CLB6)")
    parser.add_argument("--campus",   action="store_true", help="Generate campus summary")
    parser.add_argument("--open",     action="store_true", help="Open PDF after generation")
    args = parser.parse_args()

    if args.building:
        b = args.building
        _refresh_intervention_data(b)
        data = load_building_data(b)
        bdir = REPORTS_DIR / b
        bdir.mkdir(parents=True, exist_ok=True)

        pdf_path = str(bdir / f"{b}_report.pdf")
        md_path  = str(bdir / f"{b}_calibration_log.md")

        print(f"\n── {b} ──────────────────────────")
        build_building_pdf(data, pdf_path)
        build_calibration_log_md(data, md_path)

        if args.open:
            os.system(f"open '{pdf_path}'")

    elif args.campus:
        all_data = load_all_buildings()
        if not all_data:
            print("No building data found in outputs/. Run simulate.py first.")
            sys.exit(1)

        # Also regenerate all individual building reports
        refreshed = []
        for d in all_data:
            b = d["building"]
            _refresh_intervention_data(b)
            refreshed.append(load_building_data(b))

        for data in refreshed:
            b    = data["building"]
            bdir = REPORTS_DIR / b
            bdir.mkdir(parents=True, exist_ok=True)
            print(f"\n── {b} ──────────────────────────")
            build_building_pdf(data, str(bdir / f"{b}_report.pdf"))
            build_calibration_log_md(data, str(bdir / f"{b}_calibration_log.md"))

        summary_path = str(REPORTS_DIR / "NUS_Campus_Summary_Report.pdf")
        print("\n── Campus Summary ──────────────────────────")
        build_campus_summary_pdf(refreshed, summary_path)

        if args.open:
            os.system(f"open '{summary_path}'")

    else:
        parser.print_help()
        print("\nTip: ReportAgent calls this as: python report.py --building CLB6")


if __name__ == "__main__":
    main()
