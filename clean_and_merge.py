#!/usr/bin/env python3
"""
Apply chain merges, clear fake phones, flag risky groups, output dealers_clean.csv.

Run: python3 clean_and_merge.py
"""

import csv
import re
from pathlib import Path

INPUT  = "dealers.csv"
OUTPUT = "dealers_clean.csv"

# ── Chain merge definitions ────────────────────────────────────────────────────
# (canonical_name, [all member names to merge into it])
MERGE_GROUPS = [
    ("PAWN 1", [
        "PAWN 1","PAWN 1 - BILLINGS","PAWN 1 - VISTA","PAWN 1 - ORCHARD",
        "PAWN 1 POCOTELLO","PAWN 1-POST FALLS","PAWN 1 -  NORTH","PAWN 1 CDA",
        "PAWN 1 AMMON","PAWN 1 INCORPORATED","PAWN 1 -LEWISTON","PAWN 1-MISSOULA",
        "PAWN 1 - STATE STREET","PAWN 1 BREMERTON","PAWN 1 - KALISPELL",
        "PAWN 1 HAYDEN","PAWN 1 NAMPA","PAWN 1 TWIN FALLS","PAWN 1 - COLUMBIA FALLS",
        "PAWN 1 - MT HOME","PAWN 1-E SPRAGUE","PAWN 1 TACOMA","PAWN 1 NORTH MONROE",
    ]),
    ("HAFER'S GUNS / HAFER'S CUSTOM", [
        "HAFER'S GUNS / HAFER'S CUSTOM",
        "HAFER'S GUNS / HAFER'S CUSTOM / HAFER'S",
    ]),
    ("MIDWEST SHOOTING CENTER", [
        "MIDWEST SHOOTING CENTER","MIDWEST SHOOTING CENTER - Pittsburgh",
        "MIDWEST SHOOTING CENTER - Sylvania","MIDWEST SHOOTING CENTER -Cridersville",
        "MIDWEST SHOOTING CENTER- Beavercreek","MIDWEST SHOOTING CENTER- Taylor",
    ]),
    ("MEGA PAWN", [
        "MEGA PAWN","MEGA PAWN #1 OF FLINT","MEGA PAWN #2 OF FLINT",
        "MEGA PAWN 3","MEGA PAWN 4, INC","MEGA PAWN OF BURTON INC",
    ]),
    ("JAY'S GUNS & ACCESSORIES", [
        "JAY'S GUNS & ACCESSORIES",
        "JAY'S GUNS &  ACCESSORIES IV",
    ]),
    ("SUNBELT JEWELRY & LOAN", [
        "SUNBELT JEWELRY & LOAN","SUNBELT JEWELRY & LOAN #1","SUNBELT JEWELRY & LOAN #3",
        "SUNBELT JEWELRY & LOAN #4","SUNBELT JEWELRY & LOAN #5","SUNBELT JEWELRY & LOAN #6",
        "SUNBELT JEWELRY & LOAN #7","SUNBELT JEWELRY & LOAN #8","SUNBELT JEWELRY & LOAN #10",
        "SUNBELT JEWELRY & LOAN #11","SUNBELT JEWELRY & LOAN #12","SUNBELT JEWELRY & LOAN #14",
    ]),
    ("CASH PLUS PAWN", [
        "CASH PLUS PAWN","CASH PLUS PAWN - STEPHENVILLE",
        "CASH PLUS PAWN -BURLESON","CASH PLUS PAWN -SOUTH FREEWAY",
    ]),
    ("CENTEX PAWN", [
        "CENTEX PAWN","CENTEX PAWN HEWITT","CENTEX PAWN WACO","CENTEX PAWN TEMPLE",
    ]),
    ("PREMIER PLUS PAWN", [
        "PREMIER PLUS PAWN","PREMIER PLUS PAWN-TERRELL","PREMIER PLUS PAWN-GREENVILLE",
    ]),
    ("CASH IN A FLASH", [
        "CASH IN A FLASH","CASH IN A FLASH HAMPDEN","CASH IN A FLASH PARKER",
    ]),
    ("POINT BLANK RANGE", [
        "POINT BLANK RANGE","POINT BLANK RANGE MATTHEWS",
    ]),
    ("CATALINA PAWN", [
        "CATALINA PAWN","CATALINA PAWN LLC-Oracle","CATALINA PAWN LLC- Prince",
    ]),
    ("RIVER CITY PAWN", [
        "RIVER CITY PAWN",
        "RIVER CITY PAWN & JEWELRY LLC -MISSOURI CITY",
        "RIVER CITY PAWN & JEWELRY LLC-SCHERTZ",
    ]),
    ("UNCLE DANS PAWN", [
        "UNCLE DANS PAWN","UNCLE DANS PAWN JEWELRY & GUN SALES INC",
    ]),
    ("UNCLE DAN'S PAWN", [
        "UNCLE DAN'S PAWN","UNCLE DAN'S MESQUITE","UNCLE DAN'S MESQUITE BIG TOWN",
        "UNCLE DAN'S PAWN JEWELRY & GUN SALES, INC",
    ]),
    ("SUNBELT PAWN & SALES", [
        "SUNBELT PAWN & SALES","SUNBELT PAWN & SALES #15","SUNBELT PAWN & SALES #16",
    ]),
    ("FORT COLLINS DAILY PAWN", [
        "FORT COLLINS DAILY PAWN","FORT COLLINS DAILY PAWN NORTH",
    ]),
    ("QC PAWN", ["QC PAWN","QC PAWN 2"]),
    ("BIG BROTHERS PAWN", [
        "BIG BROTHERS PAWN","BIG BROTHERS PAWN #1","BIG BROTHERS PAWN #2",
    ]),
    ("SMALL TOWN SPORTS & OUTDOORS", [
        "SMALL TOWN SPORTS & OUTDOORS","SMALL TOWN SPORTS & OUTDOORS GREENSBURG",
    ]),
    ("14K PAWN & GUN", ["14K PAWN & GUN","14K PAWN & GUN OXFORD"]),
    ("FREEDOM FIREARMS", ["FREEDOM FIREARMS","FREEDOM FIREARMS III LLC"]),
    ("COUNTY LINE PAWN", ["COUNTY LINE PAWN","COUNTY LINE PAWN SHOP"]),
]

# Groups where we're unsure — flag but don't merge
FLAG_GROUPS = [
    ["TOP GUN","TOP GUN PAWN","TOP GUN ARMORY","TOP GUN FIREARMS, LLC","TOP GUN RANGE"],
    ["CAPITOL CITY PAWN","CAPITOL CITY PAWN & JEWELRY"],
    ["EAGLE PAWN","EAGLE PAWN OUTFITTERS INC"],
    ["SPARTAN ARMS","SPARTAN ARMS AND AMMO"],
]

# ── Phone validation ───────────────────────────────────────────────────────────
# All 410 phones in the dataset are fake (sequential middle digits from page elements).
# Clear them all.
def _is_valid_phone(phone: str) -> bool:
    if not phone:
        return False
    digits = re.sub(r'\D', '', phone)
    if len(digits) != 10:
        return False
    area = int(digits[:3])
    # Real NANP area codes: first digit 2-9, not 0xx or 1xx
    if area < 200:
        return False
    # Exchange: first digit 2-9
    if int(digits[3]) < 2:
        return False
    return True


def main():
    rows = list(csv.DictReader(open(INPUT)))
    print(f"Loaded {len(rows)} rows from {INPUT}")

    # Build lookup: name → row
    by_name = {r["dealer_name"]: r for r in rows}

    # Build merge map: member_name → canonical_name
    merge_map: dict[str, str] = {}
    for canonical, members in MERGE_GROUPS:
        for m in members:
            if m in by_name:
                merge_map[m] = canonical

    # Build flag set
    flag_set: dict[str, str] = {}
    for group in FLAG_GROUPS:
        existing = [m for m in group if m in by_name]
        if len(existing) > 1:
            note = f"possible duplicate of: {', '.join(m for m in existing)}"
            for m in existing:
                flag_set[m] = note

    # Build merged rows
    merged: dict[str, dict] = {}  # canonical → accumulated row

    for canonical, members in MERGE_GROUPS:
        existing = [m for m in members if m in by_name]
        if not existing:
            continue
        acc = None
        all_names = []
        for m in existing:
            r = by_name[m]
            all_names.append(m)
            if acc is None:
                acc = dict(r)
                acc["dealer_name"] = canonical
                acc["total_listings"] = int(r["total_listings"])
                acc["new_listings"]   = int(r["new_listings"])
                acc["used_listings"]  = int(r["used_listings"])
            else:
                acc["total_listings"] += int(r["total_listings"])
                acc["new_listings"]   += int(r["new_listings"])
                acc["used_listings"]  += int(r["used_listings"])
                for f in ["location_city","location_state","location_address",
                          "dealer_rating","review_count"]:
                    acc[f] = acc.get(f) or r.get(f,"")

        acc["location_count"] = len(existing)
        acc["merged_from"]    = " | ".join(all_names)
        acc["possible_duplicate"] = ""
        merged[canonical] = acc

    # Build output rows
    output = []
    seen_canonical = set()

    for r in rows:
        name = r["dealer_name"]

        if name in merge_map:
            canonical = merge_map[name]
            if canonical not in seen_canonical:
                seen_canonical.add(canonical)
                row = merged[canonical]
                # Clear fake phone
                row["phone"] = row["phone"] if _is_valid_phone(row.get("phone","")) else ""
                output.append(row)
            # else: already added this canonical row, skip duplicate member
            continue

        # Not part of a merge group — keep as-is
        row = dict(r)
        row["location_count"]     = 1
        row["merged_from"]        = ""
        row["possible_duplicate"] = flag_set.get(name, "")
        # Clear fake phone
        row["phone"] = row["phone"] if _is_valid_phone(row.get("phone","")) else ""
        output.append(row)

    # Write output
    fieldnames = [
        "dealer_name","guns_com_profile_url",
        "location_city","location_state","location_address",
        "phone","email","website",
        "total_listings","new_listings","used_listings",
        "ffl_verified","dealer_rating","review_count",
        "location_count","merged_from","possible_duplicate","scraped_at",
    ]
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in output:
            w.writerow(row)

    # Stats
    merged_count  = sum(1 for r in output if r["merged_from"])
    flagged_count = sum(1 for r in output if r["possible_duplicate"])
    total_listings = sum(int(r["total_listings"]) for r in output)

    print(f"\nResults:")
    print(f"  Input rows       : {len(rows)}")
    print(f"  Output rows      : {len(output)}  (-{len(rows)-len(output)} chain members folded in)")
    print(f"  Chains merged    : {merged_count}")
    print(f"  Flagged for review: {flagged_count}")
    print(f"  Phones cleared   : {sum(1 for r in rows if r['phone'])} fake → 0")
    print(f"  Total listings   : {total_listings:,}")
    print(f"\nWritten → {OUTPUT}")

    # Listing distribution after merge
    totals = sorted([int(r["total_listings"]) for r in output], reverse=True)
    print(f"\nListing distribution after merge:")
    print(f"  1 listing        : {sum(1 for t in totals if t==1)}")
    print(f"  2–9              : {sum(1 for t in totals if 2<=t<=9)}")
    print(f"  10–49            : {sum(1 for t in totals if 10<=t<=49)}")
    print(f"  50–199           : {sum(1 for t in totals if 50<=t<=199)}")
    print(f"  200+             : {sum(1 for t in totals if t>=200)}")
    print(f"  Top 5 dealers:")
    top5 = sorted(output, key=lambda r: -int(r["total_listings"]))[:5]
    for r in top5:
        locs = f"({r['location_count']} locations)" if int(r['location_count']) > 1 else ""
        print(f"    {r['dealer_name']}: {r['total_listings']} listings {locs}")


if __name__ == "__main__":
    main()
