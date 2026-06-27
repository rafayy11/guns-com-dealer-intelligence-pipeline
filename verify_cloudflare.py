#!/usr/bin/env python3
"""
Phase 6: Verify Cloudflare-protected dealer websites using SeleniumBase UC.

Targets dealers whose website is marked fetch_failed — meaning requests/Exa
couldn't read the content. SeleniumBase UC (undetected Chrome) renders the
page like a real browser and bypasses most Cloudflare bot protections.

If a Cloudflare CAPTCHA appears, the script pauses and asks you to solve it.

Run: python3 verify_cloudflare.py
"""

import csv
import json
import re
import time
import random
from pathlib import Path

ENRICHED = "dealers_enriched.csv"
VERIFY   = "verify_results.csv"
CACHE    = "cache/cf_verify_cache.json"

_GENERIC = {
    "gun", "guns", "pawn", "pawnshop", "arms", "armory", "firearm", "firearms",
    "rifle", "rifles", "pistol", "shop", "store", "inc", "llc", "ltd", "co",
    "corp", "company", "sales", "supply", "trading", "sporting", "outdoor",
    "outdoors", "goods", "center", "sports", "defense", "tactical", "shooting",
    "range", "ammo", "ammunition", "military", "surplus", "loan", "loans",
    "jewelry", "jewelers", "jewellery", "cash", "city", "ace", "pro", "top",
    "big", "new", "old", "all", "usa", "america", "american",
}

CLOUDFLARE_SIGNALS = [
    "just a moment", "checking your browser", "cloudflare",
    "ddos protection", "ray id", "please enable cookies",
    "verifying you are human", "enable javascript",
]


def _unique_tokens(name: str) -> list:
    raw = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    tokens = [t for t in raw if len(t) >= 4 and t not in _GENERIC]
    return tokens if tokens else [t for t in raw if len(t) >= 4]


def _is_cloudflare(text: str) -> bool:
    lower = text.lower()
    return any(s in lower for s in CLOUDFLARE_SIGNALS)


def _verify_content(text: str, name: str, city: str, state: str) -> str:
    lower = text.lower()
    tokens = _unique_tokens(name)
    if any(t in lower for t in tokens):
        return "name_match"
    if city and len(city) > 3 and city.lower() in lower:
        return "location_match"
    if state and state.lower() in lower:
        return "location_match"
    return "unverified"


def _extract_text(sb) -> str:
    try:
        title = sb.get_title() or ""
    except Exception:
        title = ""
    try:
        body = sb.get_page_source() or ""
        # Strip tags
        body = re.sub(r"<[^>]+>", " ", body)
        body = re.sub(r"\s+", " ", body)[:5000]
    except Exception:
        body = ""
    return f"{title} {body}"


def _wait_past_cloudflare(sb, url: str) -> bool:
    """Return True if page is past Cloudflare after optional user solve."""
    for attempt in range(3):
        text = _extract_text(sb)
        if not _is_cloudflare(text):
            return True
        if attempt == 0:
            print(f"\n  ⚠️  Cloudflare detected — solve it in the Chrome window, then press ENTER...")
            input("  >>> Press ENTER when done: ")
        else:
            print(f"  Still blocked (attempt {attempt+1}) — press ENTER to retry or Ctrl+C to skip...")
            input("  >>> Press ENTER: ")
    return False


TRUSTED_SOURCES = {"exa_fetch", "exa_search", "exa", "apollo", "guns_com"}
ALREADY_GOOD    = {"name_match", "location_match", "guns_com_registered"}


def _load_targets() -> list:
    rows = list(csv.DictReader(open(ENRICHED)))
    verify_status = {}
    if Path(VERIFY).exists():
        for r in csv.DictReader(open(VERIFY)):
            verify_status[r["dealer_name"]] = r["verify_status"]

    targets = []
    for r in rows:
        name    = r["dealer_name"]
        website = r.get("website", "")
        wv      = r.get("website_verified", "")
        source  = r.get("website_source", "")
        vs      = verify_status.get(name, "")

        if not website:
            continue
        # Skip already content-verified
        if wv in ALREADY_GOOD or vs in ALREADY_GOOD:
            continue
        # Only target fetch_failed — the one status UC Chrome can help with
        if wv == "fetch_failed" or vs == "fetch_failed":
            targets.append(r)

    return targets


def main():
    targets = _load_targets()
    print(f"Cloudflare-blocked sites to verify: {len(targets)}")

    cache = json.loads(Path(CACHE).read_text()) if Path(CACHE).exists() else {}
    todo  = [r for r in targets if r["dealer_name"] not in cache]
    print(f"Cache: {len(cache)} done | Remaining: {len(todo)}")

    if not todo:
        print("All CF-blocked sites already processed — applying results.")
    else:
        try:
            from seleniumbase import SB
        except ImportError:
            print("ERROR: seleniumbase not installed. Run: pip install seleniumbase")
            return

        print("\nOpening Chrome (UC headless mode)...\n")

        found = skipped = 0

        with SB(uc=True, headless=True) as sb:
            for i, row in enumerate(todo, 1):
                name    = row["dealer_name"]
                website = row.get("website", "")
                city    = row.get("location_city", "")
                state   = row.get("location_state", "")

                print(f"[{i}/{len(todo)}] {name[:50]}")

                try:
                    sb.uc_open_with_reconnect(website, reconnect_time=4)
                    time.sleep(random.uniform(2.0, 3.5))

                    text = _extract_text(sb)
                    if _is_cloudflare(text):
                        print(f"  → cf_blocked")
                        cache[name] = {"website": website, "verify_status": "cf_blocked", "source": "cf_verify"}
                        skipped += 1
                    else:
                        status = _verify_content(text, name, city, state)
                        symbol = "✓" if status == "name_match" else ("~" if status == "location_match" else "✗")
                        print(f"  → {symbol} {status}  {website}")
                        cache[name] = {"website": website, "verify_status": status, "source": "cf_verify"}
                        if status in ("name_match", "location_match"):
                            found += 1

                except KeyboardInterrupt:
                    print(f"\nInterrupted — saving cache ({len(cache)} entries)")
                    Path(CACHE).write_text(json.dumps(cache, indent=2))
                    raise
                except Exception as e:
                    print(f"  → error: {e}")
                    cache[name] = {"website": website, "verify_status": "cf_blocked", "source": "cf_verify"}
                    skipped += 1

                Path(CACHE).write_text(json.dumps(cache, indent=2))
                time.sleep(random.uniform(1.0, 2.0))

        print(f"\nDone: {found} verified, {skipped} still blocked")

    # Apply results to CSV
    print(f"\nApplying to {ENRICHED}...")
    rows       = list(csv.DictReader(open(ENRICHED)))
    row_by_name = {r["dealer_name"]: r for r in rows}
    updated = 0

    for name, result in cache.items():
        if name not in row_by_name:
            continue
        status  = result.get("verify_status", "")
        website = result.get("website", "")

        if status in ("name_match", "location_match"):
            row_by_name[name]["website"]          = website
            row_by_name[name]["website_verified"] = status
            updated += 1
        elif status in ("unverified", "cf_blocked"):
            # Downgrade — mark as unverified but keep URL (domain match is still some signal)
            row_by_name[name]["website_verified"] = "unverified"

    fieldnames = list(rows[0].keys())
    with open(ENRICHED, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Updated {updated} dealers to name_match/location_match")

    # Summary
    rows = list(csv.DictReader(open(ENRICHED)))
    has_web  = sum(1 for r in rows if r.get("website"))
    verified = sum(1 for r in rows if r.get("website_verified") in ("name_match", "location_match", "guns_com_registered"))
    cf_left  = sum(1 for r in rows if r.get("website_verified") == "fetch_failed")
    print(f"\nDataset totals:")
    print(f"  With website    : {has_web}/{len(rows)}")
    print(f"  Content-verified: {verified}/{len(rows)}")
    print(f"  Still CF-blocked: {cf_left}")
    print(f"\nRun: python3 verify_websites.py --only-unverified  (final pass for any remaining)")


if __name__ == "__main__":
    main()
