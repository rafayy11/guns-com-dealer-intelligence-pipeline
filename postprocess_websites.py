#!/usr/bin/env python3
"""
Post-process dealers_enriched.csv:
  1. Final cleanup of bad websites (remaining directories/locators)
  2. Quality stats
  3. Summary of what's missing

Run after enrich_websites.py completes.
"""

import csv
import re
from pathlib import Path

INPUT  = "dealers_enriched.csv"
OUTPUT = "dealers_enriched.csv"  # in-place update

# Comprehensive bad domain list (same as enrich_websites.py + any new ones found)
SKIP_DOMAINS = {
    "guns.com", "gunbroker.com", "armslist.com", "gunsamerica.com", "wikiarms.com",
    "hibid.com", "proxibid.com", "gotoauction.com", "bidspotter.com",
    "facebook.com", "instagram.com", "twitter.com", "x.com", "youtube.com",
    "yelp.com", "bbb.org", "google.com", "bing.com", "yahoo.com",
    "wikipedia.org", "amazon.com", "ebay.com", "craigslist.org",
    "yellowpages.com", "whitepages.com", "superpages.com", "manta.com",
    "chamberofcommerce.com", "bizapedia.com", "opencorporates.com",
    "pawnbat.com", "pawnshops.net", "pawnshopmap.com", "pawnpros.com",
    "telephonedirectories.us", "411.com", "angi.com", "linkedin.com",
    "masterffl.com", "fflguru.com", "ffls.com", "ffleasy.com",
    "findgundealers.com", "ffldealernetwork.com", "gunmade.com",
    "shooting.org", "usedguns.com",
    "henryusa.com", "beretta.com", "glock.com", "ruger.com", "sigsauer.com",
    "mossberg.com", "nra.org", "smith-wesson.com", "fortscottmunitions.com",
    "lipseys.com", "dnb.com", "edan.io", "wheree.com",
    "statscrop.com", "usitestat.com", "hypestat.com", "alexa.com",
    "gun-guard.com", "com-place.com", "foursquare.com",
    "festivals.com", "eventbrite.com", "tripadvisor.com",
    "tenereteam.com", "procashadvances.com",
    "glassdoor.com", "indeed.com",
    "pawnshoplocator.us", "pawnshoplocator.com",
    "chamberofcommerce.org", "chamber.org",
}
SKIP_PATHS = [
    "/dealer-location/", "/dealer-locator/", "/find-a-dealer/", "/dealers/",
    "/pawn-shops/", "/gun-shops/", "/ffl/", "/ffl-dealers/",
    "/store-locator/", "/stores/", "/retailer/", "/find-dealer/",
    "/pawn-shops-",
]

_GENERIC_TOKENS = {
    "gun", "guns", "pawn", "pawnshop", "arms", "armory", "armories",
    "firearm", "firearms", "rifle", "rifles", "pistol", "shop", "store",
    "inc", "llc", "ltd", "co", "corp", "company", "sales", "supply",
    "trading", "sporting", "outdoor", "outdoors", "goods", "center",
    "sports", "defense", "tactical", "shooting", "range", "ammo",
    "ammunition", "military", "surplus", "loan", "loans", "jewelry",
    "jewelers", "jewellery",
    "cash", "city", "ace", "pro", "top", "big", "new", "old", "all",
    "usa", "america", "american",
}


def _domain(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else ""


def _all_tokens(name: str) -> set:
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return {t for t in name.split() if len(t) > 2}


def _unique_tokens(name: str) -> set:
    return _all_tokens(name) - _GENERIC_TOKENS


def _domain_score(url: str, name: str) -> int:
    dom = _domain(url).replace("-", " ").replace(".", " ")
    ut = _unique_tokens(name)
    all_t = _all_tokens(name)
    # Require tokens ≥ 4 chars to avoid abbreviation collisions
    usable_ut  = {t for t in ut    if len(t) >= 4}
    usable_all = {t for t in all_t if len(t) >= 4}
    toks = usable_ut if usable_ut else usable_all
    return sum(1 for t in toks if t in dom) if toks else 0


def is_bad_website(url: str, name: str) -> bool:
    if not url:
        return False
    dom = _domain(url)
    if any(s in dom for s in SKIP_DOMAINS):
        return True
    ul = url.lower()
    if any(p in ul for p in SKIP_PATHS):
        return True
    if _domain_score(url, name) < 1:
        return True
    return False


def main():
    if not Path(INPUT).exists():
        print(f"ERROR: {INPUT} not found — run enrich_websites.py first")
        return

    rows = list(csv.DictReader(open(INPUT)))
    print(f"Loaded {len(rows)} rows from {INPUT}")

    cleared = 0
    for row in rows:
        website = row.get("website", "")
        if is_bad_website(website, row["dealer_name"]):
            row["website"]           = ""
            row["website_source"]    = ""
            row["website_verified"]  = ""
            row["website_confidence"] = "none"
            cleared += 1

    # Stats
    has_city    = sum(1 for r in rows if r.get("location_city"))
    has_state   = sum(1 for r in rows if r.get("location_state"))
    has_website = sum(1 for r in rows if r.get("website"))
    high_conf   = sum(1 for r in rows if r.get("website_confidence") == "high")
    med_conf    = sum(1 for r in rows if r.get("website_confidence") == "medium")
    chains      = sum(1 for r in rows if int(r.get("location_count", 1)) > 1)

    print(f"\nAfter post-processing:")
    print(f"  Cleared bad websites  : {cleared}")
    print(f"  With website (clean)  : {has_website} / {len(rows)}  ({has_website/len(rows)*100:.0f}%)")
    print(f"    High confidence     : {high_conf}")
    print(f"    Medium confidence   : {med_conf}")
    print(f"  With city/state       : {has_city} / {len(rows)}  ({has_city/len(rows)*100:.0f}%)")
    print(f"  Chains (>1 location)  : {chains}")

    # Dealers with no website — show top 20 by listings
    no_site = [r for r in rows if not r.get("website")]
    no_site_top = sorted(no_site, key=lambda r: -int(r.get("total_listings", 0)))[:20]
    print(f"\nTop dealers still missing website ({len(no_site)} total):")
    for r in no_site_top:
        loc = f"{r.get('location_city','')},{r.get('location_state','')}" if r.get("location_city") else "?"
        print(f"  {r['dealer_name'][:40]:40} | {loc:12} | {r['total_listings']} listings")

    # Write back
    fieldnames = list(rows[0].keys())
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in rows:
            w.writerow(row)

    print(f"\nWritten → {OUTPUT}")


if __name__ == "__main__":
    main()
