#!/usr/bin/env python3
"""
Detect and merge duplicate dealer entries in dealers.csv.

Three-signal verification before any merge:
  Signal 1 — Name analysis: classify the suffix between two similar names.
             Slash-variant suffixes (" / EXTRA") = same-store candidate.
             Location/number suffixes = different physical store.
  Signal 2 — Product page address: visit one listing from each dealer,
             compare the physical address. Same address = same store.
  Signal 3 — Flag for manual review if address not found on page.

Only merges when Signal 2 confirms same address.
Flags everything else without auto-merging.

Usage:
    python3 deduplicate.py                    # analysis only (no browser)
    python3 deduplicate.py --verify           # run Signal 2 browser checks
    python3 deduplicate.py --verify --apply   # verify + write deduped CSV
"""

import argparse
import csv
import re
import sys
import time
from pathlib import Path

from rapidfuzz import fuzz

INPUT_CSV = "dealers.csv"
REPORT_CSV = "out/duplicates_report.csv"
DEDUPED_CSV = "dealers_deduped.csv"

# ── Suffix classification ──────────────────────────────────────────────────────

_LEGAL_SUFFIXES = re.compile(
    r"^[\s,]*(LLC|INC|CORP|CO|LTD|LP|INCORPORATED|COMPANY)\s*\.?\s*$",
    re.IGNORECASE,
)

_ROMAN_NUMERALS = {"I", "II", "III", "IV", "V", "VI", "VII", "VIII", "IX", "X",
                   "XI", "XII", "XIV", "XV", "XVI", "XVII", "XVIII", "XIX", "XX"}

_CARDINALS = {"NORTH", "SOUTH", "EAST", "WEST",
              "NORTHEAST", "NORTHWEST", "SOUTHEAST", "SOUTHWEST",
              "DOWNTOWN", "UPTOWN", "MIDTOWN", "CENTRAL"}


def _classify_suffix(suffix: str) -> str:
    """
    Given the suffix that distinguishes a longer name from a shorter one,
    return:
      'name_variant'     → slash-separated extra brand name, or legal-only suffix
                           (same store, candidate for merge)
      'location_branch'  → city name, store number, cardinal direction, dash-separator
                           (different physical store, keep separate)
      'numbered_branch'  → #N or Roman numeral suffix
                           (different physical store, keep separate)
    """
    s = suffix.strip()

    # Slash-separated extra brand: " / HAFER'S" — same store candidate
    if s.startswith("/"):
        return "name_variant"

    # Legal-only suffix: "LLC", "INC", etc. — same store candidate
    if _LEGAL_SUFFIXES.match(s):
        return "name_variant"

    # Dash separator (any form: " - ", "- ", "-") → location branch
    if re.match(r"^[-–]", s) or re.match(r"^\s*[-–]\s*", suffix):
        return "location_branch"

    # Store number: #1, #2, etc.
    if re.match(r"^#\s*\d+", s):
        return "numbered_branch"

    # Roman numeral at end: " IV", " V", " XI"
    words = s.upper().split()
    if words and words[-1] in _ROMAN_NUMERALS:
        return "numbered_branch"
    if len(words) == 1 and words[0] in _ROMAN_NUMERALS:
        return "numbered_branch"

    # Cardinal direction
    if words and words[0] in _CARDINALS:
        return "location_branch"

    # Trailing digit-only word: "PAWN 1 2" → store number variant
    if words and re.match(r"^\d+$", words[-1]):
        return "numbered_branch"

    # Anything else that starts with a word (city name, location)
    # e.g., "POCOTELLO", "POST FALLS", "CDA", "BREMERTON"
    if words and re.match(r"^[A-Z]", words[0]):
        return "location_branch"

    return "location_branch"  # default: assume different location, keep separate


def _normalize(name: str) -> str:
    name = name.upper().strip()
    name = re.sub(r"[''`]", "'", name)
    name = re.sub(r"\s+", " ", name)
    return name


# ── Candidate detection ────────────────────────────────────────────────────────

def find_candidates(dealers: list[dict]) -> list[dict]:
    """
    Find pairs where one name is a clean prefix of the other.
    Fuzzy matching is intentionally excluded — it generates too many
    false positives for gun shop names that share common words.
    Prefix match is precise and directly represents the duplicate pattern
    we see on guns.com (same shop, one name has extra words appended).
    """
    normed = [(i, d["dealer_name"], _normalize(d["dealer_name"])) for i, d in enumerate(dealers)]
    candidates = []
    seen = set()

    for i, (idx_a, raw_a, norm_a) in enumerate(normed):
        for idx_b, raw_b, norm_b in normed[i + 1:]:
            key = (min(idx_a, idx_b), max(idx_a, idx_b))
            if key in seen:
                continue

            # One must be a clean prefix of the other
            if len(norm_a) == len(norm_b):
                # Exact match after normalization
                if norm_a == norm_b:
                    seen.add(key)
                    candidates.append(_make_candidate(idx_a, raw_a, norm_a, idx_b, raw_b, norm_b, ""))
                continue

            short, long_ = (norm_a, norm_b) if len(norm_a) < len(norm_b) else (norm_b, norm_a)
            short_idx, long_idx = (idx_a, idx_b) if len(norm_a) < len(norm_b) else (idx_b, idx_a)
            short_raw = raw_a if len(norm_a) < len(norm_b) else raw_b
            long_raw  = raw_b if len(norm_a) < len(norm_b) else raw_a

            if not long_.startswith(short):
                continue

            # What follows the short name in the long name?
            suffix = long_[len(short):]
            # Must start with a separator character, not mid-word
            if suffix and suffix[0] not in " /,-#":
                continue

            seen.add(key)
            candidates.append(_make_candidate(short_idx, short_raw, short, long_idx, long_raw, long_, suffix))

    return candidates


def _make_candidate(idx_a, raw_a, norm_a, idx_b, raw_b, norm_b, suffix):
    return {
        "idx_a": idx_a,   # shorter name index
        "idx_b": idx_b,   # longer name index
        "name_a": raw_a,  # shorter
        "name_b": raw_b,  # longer
        "norm_a": norm_a,
        "norm_b": norm_b,
        "suffix": suffix.strip(),
        "similarity": fuzz.ratio(norm_a, norm_b),
        "detection": "prefix",
        "signal_1": "",
        "signal_2": "",
        "address_a": "",
        "address_b": "",
        "verdict": "",
        "action": "",
    }


# ── Signal 1 ───────────────────────────────────────────────────────────────────

def apply_signal_1(candidates: list[dict]) -> list[dict]:
    for c in candidates:
        suffix_type = _classify_suffix(c["suffix"])
        c["signal_1"] = f"suffix_type={suffix_type} suffix={c['suffix']!r}"

        if suffix_type == "name_variant":
            c["verdict"] = "ambiguous"
            c["action"] = "needs_verification"
        else:
            # location_branch or numbered_branch → definitely different stores
            c["verdict"] = "different_stores"
            c["action"] = "keep_separate"

    return candidates


# ── Signal 2 (browser) ────────────────────────────────────────────────────────

def _extract_address_from_page(sb) -> str:
    selectors = [
        "[class*='seller-info']", "[class*='dealer-info']",
        "[class*='store-info']", "[class*='seller-detail']",
        "[itemprop='streetAddress']", "[class*='address']",
        "[class*='location']",
    ]
    for sel in selectors:
        try:
            el = sb.find_element(sel)
            text = el.text.strip()
            if text and re.search(r'\d', text) and re.search(r'\b[A-Z]{2}\b', text):
                return text
        except Exception:
            continue
    try:
        src = sb.get_page_source()
        matches = re.findall(
            r'\d+\s+[A-Za-z][A-Za-z\s]{3,30},\s*[A-Za-z\s]{3,20},\s*[A-Z]{2}\s*\d{5}',
            src
        )
        if matches:
            return matches[0]
    except Exception:
        pass
    return ""


def _normalize_address(addr: str) -> str:
    addr = addr.upper().strip()
    addr = re.sub(r'[^\w\s]', ' ', addr)
    addr = re.sub(r'\s+', ' ', addr)
    return addr


def verify_by_address(candidates: list[dict], dealers: list[dict]) -> list[dict]:
    ambiguous = [c for c in candidates if c["verdict"] == "ambiguous"]
    if not ambiguous:
        print("  No ambiguous candidates — browser verification not needed.")
        return candidates

    print(f"\n  Signal 2: Verifying {len(ambiguous)} ambiguous pair(s) via product page address...\n")

    from scraper.browser import get_browser, close_browser, safe_get
    from scraper.listing_count import get_sample_listing_url

    sb = get_browser(headless=False)

    for c in ambiguous:
        name_a = c["name_a"]
        name_b = c["name_b"]
        print(f"  Checking: \"{name_a}\"  vs  \"{name_b}\"")

        url_a = get_sample_listing_url(sb, name_a)
        url_b = get_sample_listing_url(sb, name_b)

        if not url_a or not url_b:
            c["signal_2"] = "no_listing_found"
            c["verdict"] = "unresolved"
            c["action"] = "flag_manual_review"
            c["address_a"] = ""
            c["address_b"] = ""
            print(f"         No listing found — flagged for manual review")
            continue

        safe_get(sb, url_a)
        time.sleep(2)
        addr_a = _extract_address_from_page(sb)

        safe_get(sb, url_b)
        time.sleep(2)
        addr_b = _extract_address_from_page(sb)

        c["address_a"] = addr_a
        c["address_b"] = addr_b

        if not addr_a or not addr_b:
            c["signal_2"] = "address_not_on_page"
            c["verdict"] = "unresolved"
            c["action"] = "flag_manual_review"
            print(f"         Address not visible on listing page — flagged for manual review")
            continue

        sim = fuzz.ratio(_normalize_address(addr_a), _normalize_address(addr_b))
        c["signal_2"] = f"address_similarity={sim}"

        if sim >= 80:
            c["verdict"] = "same_store"
            c["action"] = "merge"
            print(f"         SAME STORE ({sim}% address match) → will merge")
            print(f"           A: {addr_a}")
            print(f"           B: {addr_b}")
        else:
            c["verdict"] = "different_stores"
            c["action"] = "keep_separate"
            print(f"         DIFFERENT STORES ({sim}% address match) → keeping separate")
            print(f"           A: {addr_a}")
            print(f"           B: {addr_b}")

    close_browser()
    return candidates


# ── Merge + output ─────────────────────────────────────────────────────────────

def _merge_rows(row_a: dict, row_b: dict) -> dict:
    merged = dict(row_a)
    # Keep longer/more descriptive name
    merged["dealer_name"] = (
        row_a["dealer_name"] if len(row_a["dealer_name"]) >= len(row_b["dealer_name"])
        else row_b["dealer_name"]
    )
    merged["total_listings"] = int(row_a["total_listings"]) + int(row_b["total_listings"])
    merged["new_listings"]   = int(row_a["new_listings"])   + int(row_b["new_listings"])
    merged["used_listings"]  = int(row_a["used_listings"])  + int(row_b["used_listings"])
    for field in ["location_city", "location_state", "location_address",
                  "phone", "email", "website", "dealer_rating", "review_count"]:
        merged[field] = row_a.get(field) or row_b.get(field) or ""
    merged["merged_from"] = f"{row_a['dealer_name']} | {row_b['dealer_name']}"
    merged["possible_duplicate"] = ""
    return merged


def apply_merges(dealers: list[dict], candidates: list[dict]) -> list[dict]:
    to_merge: dict[int, int] = {}
    flagged_pairs: dict[int, str] = {}

    for c in candidates:
        if c["action"] == "merge":
            # idx_a is shorter (base), idx_b is longer — merge longer into shorter
            to_merge[c["idx_b"]] = c["idx_a"]
        elif c["action"] == "flag_manual_review":
            flagged_pairs[c["idx_a"]] = c["name_b"]
            flagged_pairs[c["idx_b"]] = c["name_a"]

    merged_into: dict[int, dict] = {}
    result = []

    for i, dealer in enumerate(dealers):
        if i in to_merge:
            target = to_merge[i]
            if target not in merged_into:
                merged_into[target] = dict(dealers[target])
                merged_into[target].setdefault("merged_from", "")
                merged_into[target].setdefault("possible_duplicate", "")
            merged_into[target] = _merge_rows(merged_into[target], dealer)
            continue

        row = dict(dealer)
        row.setdefault("merged_from", "")
        row.setdefault("possible_duplicate", "")

        if i in merged_into:
            row = merged_into[i]
        if i in flagged_pairs:
            row["possible_duplicate"] = f"review: similar to '{flagged_pairs[i]}'"

        result.append(row)

    return result


# ── Report ─────────────────────────────────────────────────────────────────────

def write_report(candidates: list[dict], dealers: list[dict]) -> None:
    Path("out").mkdir(exist_ok=True)
    fieldnames = [
        "verdict", "action",
        "name_a", "listings_a",
        "name_b", "listings_b",
        "combined",
        "suffix", "signal_1", "signal_2",
        "address_a", "address_b",
    ]
    counts = {d["dealer_name"]: d["total_listings"] for d in dealers}

    with open(REPORT_CSV, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for c in sorted(candidates, key=lambda x: (x["verdict"], x["name_a"])):
            ca = counts.get(c["name_a"], 0)
            cb = counts.get(c["name_b"], 0)
            try:
                combined = int(ca) + int(cb)
            except Exception:
                combined = "?"
            w.writerow({
                "verdict": c["verdict"],
                "action": c["action"],
                "name_a": c["name_a"],
                "listings_a": ca,
                "name_b": c["name_b"],
                "listings_b": cb,
                "combined": combined,
                "suffix": c["suffix"],
                "signal_1": c.get("signal_1", ""),
                "signal_2": c.get("signal_2", ""),
                "address_a": c.get("address_a", ""),
                "address_b": c.get("address_b", ""),
            })


# ── CLI ────────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--verify", action="store_true",
                        help="Run Signal 2 browser verification for ambiguous pairs.")
    parser.add_argument("--apply", action="store_true",
                        help="Write dealers_deduped.csv (requires --verify).")
    parser.add_argument("--input", default=INPUT_CSV)
    args = parser.parse_args()

    print(f"\n  Loading {args.input}...")
    with open(args.input) as f:
        dealers = list(csv.DictReader(f))
    print(f"  {len(dealers)} dealers loaded.\n")

    print("  Finding candidate pairs (prefix match)...")
    candidates = find_candidates(dealers)
    print(f"  {len(candidates)} candidate pairs found.\n")

    print("  Applying Signal 1 (suffix classification)...")
    candidates = apply_signal_1(candidates)

    by_verdict: dict[str, list] = {}
    for c in candidates:
        by_verdict.setdefault(c["verdict"], []).append(c)

    diff   = by_verdict.get("different_stores", [])
    ambig  = by_verdict.get("ambiguous", [])

    print(f"  → {len(diff)} pairs: different stores (branch/location/number suffix) — keeping separate")
    print(f"  → {len(ambig)} pairs: ambiguous (name-variant suffix) — need Signal 2 verification")

    if ambig:
        print("\n  Ambiguous pairs (candidates for merge):")
        for c in ambig:
            ca = next(d["total_listings"] for d in dealers if d["dealer_name"] == c["name_a"])
            cb = next(d["total_listings"] for d in dealers if d["dealer_name"] == c["name_b"])
            print(f"    [{ca}] \"{c['name_a']}\"")
            print(f"    [{cb}] \"{c['name_b']}\"")
            print(f"           suffix: {c['suffix']!r}")
            print()

    if args.verify:
        candidates = verify_by_address(candidates, dealers)
    else:
        if ambig:
            print(f"  Run with --verify to resolve the {len(ambig)} ambiguous pair(s) via browser.\n")

    write_report(candidates, dealers)
    print(f"  Report written → {REPORT_CSV}")

    # Summary
    print("\n  ── Summary ──────────────────────────────────")
    verdicts = ["same_store", "different_stores", "ambiguous", "unresolved"]
    for v in verdicts:
        n = len(by_verdict.get(v, []))
        if n:
            actions = {c["action"] for c in by_verdict.get(v, [])}
            print(f"  {v:<22}: {n}  ({', '.join(actions)})")

    merges = sum(1 for c in candidates if c["action"] == "merge")
    print(f"\n  Merges to apply : {merges}")
    print(f"  Rows after merge: {len(dealers) - merges}")

    if args.apply:
        if not args.verify:
            print("\n  ERROR: --apply requires --verify to be run first.")
            sys.exit(1)
        deduped = apply_merges(dealers, candidates)
        fieldnames = list(dealers[0].keys()) + ["merged_from", "possible_duplicate"]
        with open(DEDUPED_CSV, "w", newline="") as f:
            w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
            w.writeheader()
            for row in deduped:
                w.writerow(row)
        print(f"\n  Written → {DEDUPED_CSV}  ({len(deduped)} rows)")


if __name__ == "__main__":
    main()
