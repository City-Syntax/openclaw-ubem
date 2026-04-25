#!/usr/bin/env python3
"""
enrich_registry_shp.py — Enrich building_registry.json with shapefile geometry data.

Reads the NUS campus shapefile (QGISFIle/MasterFile_241127.shp/.dbf) and merges
the following fields into each building entry in building_registry.json:

    shp_name_2        : human-readable building name from shapefile
    shp_archetype     : building archetype (Faculty, Research, Lecture Theatre, etc.)
    shp_floors_ag     : number of above-ground floors
    shp_floors_bg     : number of below-ground floors
    shp_floor_height  : average floor-to-floor height (m)
    shp_ag_height     : above-ground building height (m)
    shp_wwr_pct       : Window-to-Wall Ratio (%)

The shapefile is matched to registry entries by building ID (e.g. FOE13, FOS46).
Buildings not found in the shapefile are left unchanged — no data is removed.

SAFE: reads building_registry.json, writes it back atomically (via .tmp file).
      Existing fields are never removed. shp_* fields are added or updated only.

Usage:
    NUS_PROJECT_DIR=/Users/ye/nus-energy \\
        python3 {SKILL_DIR}/scripts/enrich_registry_shp.py

    # Preview without writing
    NUS_PROJECT_DIR=/Users/ye/nus-energy \\
        python3 {SKILL_DIR}/scripts/enrich_registry_shp.py --dry-run

    # Custom paths
    python3 enrich_registry_shp.py \\
        --shp /path/to/MasterFile.dbf \\
        --registry /path/to/building_registry.json \\
        --dry-run
"""

import argparse
import json
import os
import shutil
import struct
import sys
from pathlib import Path


# ---------------------------------------------------------------------------
# DBF reader (no external deps — pure stdlib)
# ---------------------------------------------------------------------------

def read_dbf(dbf_path: Path) -> list[dict]:
    """Read a dBASE III DBF file and return list of row dicts."""
    with open(dbf_path, "rb") as f:
        # Header
        f.read(4)  # version + last-update date
        num_records = struct.unpack("<I", f.read(4))[0]
        header_size = struct.unpack("<H", f.read(2))[0]
        record_size = struct.unpack("<H", f.read(2))[0]
        f.seek(32)

        # Field descriptors
        fields = []
        while True:
            desc = f.read(32)
            if not desc or desc[0] == 0x0D:
                break
            name = desc[:11].replace(b"\x00", b"").decode("ascii", errors="ignore").strip()
            ftype = chr(desc[11])
            length = desc[16]
            fields.append((name, ftype, length))

        # Records
        f.seek(header_size)
        rows = []
        for _ in range(num_records):
            raw = f.read(record_size)
            if not raw or raw[0] == 0x1A:  # EOF marker
                break
            if raw[0] == 0x2A:  # deleted record
                continue
            row = {}
            offset = 1  # skip deletion flag byte
            for name, ftype, length in fields:
                val = raw[offset : offset + length].decode("ascii", errors="ignore").strip()
                # Cast numeric fields
                if ftype == "N" and val:
                    try:
                        val = float(val) if "." in val else int(val)
                    except ValueError:
                        pass
                row[name] = val
                offset += length
            rows.append(row)
    return rows


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Enrich building_registry.json from NUS shapefile")
    parser.add_argument(
        "--shp",
        help="Path to the .dbf file (default: $NUS_PROJECT_DIR/QGISFIle/MasterFile_241127.dbf)",
    )
    parser.add_argument(
        "--registry",
        help="Path to building_registry.json (default: $NUS_PROJECT_DIR/building_registry.json)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would change without writing anything",
    )
    args = parser.parse_args()

    project_dir = Path(os.environ.get("NUS_PROJECT_DIR", "/Users/ye/nus-energy"))

    dbf_path = Path(args.shp) if args.shp else project_dir / "QGISFIle" / "MasterFile_241127.dbf"
    registry_path = Path(args.registry) if args.registry else project_dir / "building_registry.json"

    # Validate inputs
    if not dbf_path.exists():
        print(f"ERROR: DBF file not found: {dbf_path}", file=sys.stderr)
        sys.exit(1)
    if not registry_path.exists():
        print(f"ERROR: Registry not found: {registry_path}", file=sys.stderr)
        sys.exit(1)

    # Load
    print(f"Reading shapefile: {dbf_path}")
    rows = read_dbf(dbf_path)
    print(f"  → {len(rows)} buildings in shapefile")

    print(f"Reading registry:  {registry_path}")
    with open(registry_path) as f:
        registry = json.load(f)

    # Build lookup: building ID → shapefile row
    shp_lookup: dict[str, dict] = {}
    for row in rows:
        bid = row.get("ID", "").strip()
        if bid:
            shp_lookup[bid] = row

    print(f"  → {len(shp_lookup)} unique building IDs in shapefile")

    # Enrich
    matched = 0
    unmatched = []
    changes: list[str] = []

    for key, entry in registry.items():
        if key.startswith("_"):
            continue  # skip metadata/notes keys

        bid = entry.get("building", key)
        row = shp_lookup.get(bid)

        if row is None:
            unmatched.append(bid)
            continue

        matched += 1
        new_fields = {
            "shp_name_2":       row.get("Name_2", ""),
            "shp_archetype":    row.get("Archetype", ""),
            "shp_floors_ag":    row.get("floors_ag", None),
            "shp_floors_bg":    row.get("floors_bg", None),
            "shp_floor_height": row.get("floor_hei", None),
            "shp_ag_height":    row.get("ag_height", None),
            "shp_wwr_pct":      row.get("WWR (%)", None),
        }

        for field, value in new_fields.items():
            old = entry.get(field)
            if old != value:
                changes.append(f"  {bid}.{field}: {old!r} → {value!r}")
            entry[field] = value

    # Summary
    print(f"\nResults:")
    print(f"  Matched:   {matched} buildings")
    print(f"  Unmatched: {len(unmatched)} buildings (no shapefile entry)")
    if unmatched:
        print(f"  Unmatched IDs: {', '.join(sorted(unmatched))}")
    print(f"  Field changes: {len(changes)}")

    if changes:
        print("\nChanges preview (first 20):")
        for c in changes[:20]:
            print(c)
        if len(changes) > 20:
            print(f"  ... and {len(changes) - 20} more")

    if args.dry_run:
        print("\n[dry-run] No files written.")
        return

    # Atomic write: write to .tmp then rename
    tmp_path = registry_path.with_suffix(".json.tmp")
    with open(tmp_path, "w") as f:
        json.dump(registry, f, indent=2)
    shutil.move(str(tmp_path), str(registry_path))

    print(f"\n✅ Registry updated: {registry_path}")
    print(f"   Run generate_registry.py to re-extract IDF params if needed.")


if __name__ == "__main__":
    main()
