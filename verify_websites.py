#!/usr/bin/env python3
"""
Verify that each found website actually belongs to the dealer.

Three checks:
  1. Dealer name tokens appear in the page title or meta description  → "name_match"
  2. City or state appears in the page                                → "location_match"
  3. Neither found                                                    → "unverified"

Outputs verify_results.csv with a 'verify_status' column.

Run: python3 verify_websites.py [--limit N] [--input dealers_enriched.csv]
"""

import argparse
import csv
import re
import time
import random
import urllib.request
from pathlib import Path

try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False

INPUT  = "dealers_enriched.csv"
OUTPUT = "verify_results.csv"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}

_GENERIC = {
    "gun", "guns", "pawn", "pawnshop", "arms", "armory", "firearm", "firearms",
    "rifle", "rifles", "pistol", "shop", "store", "inc", "llc", "ltd", "co",
    "corp", "company", "sales", "supply", "sporting", "outdoor", "outdoors",
    "goods", "center", "sports", "defense", "tactical", "shooting", "range",
    "ammo", "ammunition", "military", "surplus", "loan", "loans", "jewelry",
    "jewelers", "cash", "city", "and", "the",
}


def _name_tokens(name: str) -> list:
    """Return meaningful (non-generic, ≥4 chars) tokens from dealer name."""
    raw = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    unique = [t for t in raw if len(t) >= 4 and t not in _GENERIC]
    if not unique:
        # fallback: all tokens ≥ 4 chars
        unique = [t for t in raw if len(t) >= 4]
    return unique


def _fetch_page(url: str, timeout: int = 8) -> str:
    """Fetch URL, return lowercased text content (title + meta + body)."""
    try:
        req = urllib.request.Request(url, headers=HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read(65536)  # max 64KB
            encoding = resp.headers.get_content_charset("utf-8")
            html = raw.decode(encoding, errors="ignore")
    except Exception as e:
        return f"[FETCH_ERROR: {e}]"

    if not BS4:
        return html.lower()

    soup = BeautifulSoup(html, "html.parser")

    # Remove script / style noise
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()

    title = soup.title.string if soup.title else ""
    meta_desc = ""
    for m in soup.find_all("meta", attrs={"name": re.compile("description", re.I)}):
        meta_desc = m.get("content", "")
        break

    body = soup.get_text(" ", strip=True)[:4000]  # first 4KB of visible text
    return f"{title} {meta_desc} {body}".lower()


def verify(dealer_name: str, website: str, city: str, state: str,
           website_source: str = "") -> dict:
    """Fetch website and return verification result."""
    tokens = _name_tokens(dealer_name)
    result = {
        "dealer_name":    dealer_name,
        "website":        website,
        "website_source": website_source,
        "location":       f"{city},{state}" if city else "",
        "name_tokens":    " | ".join(tokens[:4]),
        "verify_status":  "unverified",
        "match_detail":   "",
        "fetch_status":   "ok",
    }

    content = _fetch_page(website)

    if content.startswith("[FETCH_ERROR"):
        result["fetch_status"]  = content[14:-1][:80]
        result["verify_status"] = "fetch_failed"
        return result

    # Check 1: name token match
    matched_tokens = [t for t in tokens if t in content]
    if matched_tokens:
        result["verify_status"] = "name_match"
        result["match_detail"]  = "tokens: " + ", ".join(matched_tokens[:3])
        return result

    # Check 2: location match (only if we have city)
    if city and len(city) > 3:
        city_clean = city.lower().strip()
        if city_clean in content:
            result["verify_status"] = "location_match"
            result["match_detail"]  = f"city: {city}"
            return result
    if state and state.lower() in content:
        result["verify_status"] = "location_match"
        result["match_detail"]  = f"state: {state}"
        return result

    return result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit",  type=int, default=0,      help="Max dealers to verify (0=all)")
    parser.add_argument("--input",  default=INPUT,             help="Input CSV")
    parser.add_argument("--output", default=OUTPUT,            help="Output CSV")
    parser.add_argument("--only-unverified", action="store_true",
                        help="Skip rows already in output file")
    args = parser.parse_args()

    if not Path(args.input).exists():
        print(f"ERROR: {args.input} not found")
        return

    rows = list(csv.DictReader(open(args.input)))
    with_website = [r for r in rows if r.get("website")]
    print(f"Loaded {len(rows)} dealers — {len(with_website)} have a website to verify")

    if args.limit:
        with_website = with_website[:args.limit]
        print(f"Verifying first {args.limit}")

    # Load already-verified if resuming
    done = set()
    if args.only_unverified and Path(args.output).exists():
        for r in csv.DictReader(open(args.output)):
            done.add(r["dealer_name"])
        with_website = [r for r in with_website if r["dealer_name"] not in done]
        print(f"Skipping {len(done)} already verified — {len(with_website)} remaining")

    results = []
    ok = fail = loc_only = unver = 0

    for i, row in enumerate(with_website, 1):
        name    = row["dealer_name"]
        website = row["website"]
        city    = row.get("location_city", "")
        state   = row.get("location_state", "")

        print(f"[{i}/{len(with_website)}] {name[:45]}")

        website_source = row.get("website_source", "ddg")
        r = verify(name, website, city, state, website_source)
        results.append(r)

        status = r["verify_status"]
        if status == "name_match":
            ok += 1
            print(f"  ✓  {r['match_detail']}")
        elif status == "location_match":
            loc_only += 1
            print(f"  ~  {r['match_detail']}")
        elif status == "fetch_failed":
            fail += 1
            print(f"  ✗  fetch failed: {r['fetch_status'][:60]}")
        else:
            unver += 1
            print(f"  ?  unverified (tokens: {r['name_tokens'][:40]})")

        time.sleep(random.uniform(0.5, 1.5))

    # Write output
    fieldnames = ["dealer_name", "website", "website_source", "location", "name_tokens",
                  "verify_status", "match_detail", "fetch_status"]
    with open(args.output, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        for r in results:
            w.writerow(r)

    total = len(results)
    print(f"\nVerification Results ({total} websites checked):")
    print(f"  Name match      : {ok}  ({ok/total*100:.0f}%)  ← high confidence correct")
    print(f"  Location match  : {loc_only}  ({loc_only/total*100:.0f}%)  ← plausible but verify")
    print(f"  Fetch failed    : {fail}  ({fail/total*100:.0f}%)  ← Cloudflare or offline")
    print(f"  Unverified      : {unver}  ({unver/total*100:.0f}%)  ← review manually")
    print(f"\nWritten → {args.output}")

    # Show unverified for manual review
    unverified = [r for r in results if r["verify_status"] == "unverified"]
    if unverified:
        print(f"\nUnverified dealers (check these manually):")
        for r in unverified[:20]:
            print(f"  {r['dealer_name'][:40]:40} | {r['website'][:50]}")


if __name__ == "__main__":
    main()
