#!/usr/bin/env python3
"""
Enrich dealers_final.csv with:
  1. location_city / location_state  — extracted from masterffl.com via DDG search
  2. website                         — found via DDG search using name + location
  3. phone cleared                   — all scraped phones were fake; cleared here

Run: python3 enrich_websites.py
"""

import csv
import json
import re
import time
import random
from pathlib import Path

from ddgs import DDGS

INPUT   = "dealers_final.csv"
OUTPUT  = "dealers_enriched.csv"
CACHE   = "cache/enrich_cache.json"   # avoids re-searching on restart

# Domains that are never a gun shop's own website
SKIP_DOMAINS = {
    # Marketplaces / listing aggregators
    "guns.com", "gunbroker.com", "armslist.com", "gunsamerica.com", "wikiarms.com",
    "grabagun.com", "sportsmansguide.com", "cabelas.com", "basspro.com",
    # Auction platforms
    "hibid.com", "proxibid.com", "invaluable.com", "liveauctioneers.com",
    "auctionzip.com", "bidspotter.com", "equip-bid.com", "gotoauction.com",
    "auctioneer.net", "bidopia.com", "comasnorth.com", "auctionsniper.com",
    # Social / review / professional networks
    "facebook.com", "instagram.com", "twitter.com", "youtube.com", "yelp.com",
    "bbb.org", "nextdoor.com", "reddit.com", "linkedin.com", "tiktok.com",
    "glassdoor.com", "indeed.com",
    # Search engines / maps
    "google.com", "bing.com", "yahoo.com", "maps.google.com", "mapquest.com",
    "yandex.com", "duckduckgo.com",
    # General directories / info sites
    "wikipedia.org", "amazon.com", "ebay.com", "craigslist.org",
    "yellowpages.com", "whitepages.com", "superpages.com", "manta.com",
    "chamberofcommerce.com", "bizapedia.com", "opencorporates.com",
    "pawnbat.com", "pawnamerica.com", "pawngo.com",
    "telephonedirectories.us", "411.com", "angi.com", "thumbtack.com",
    # Website analytics / stat wrappers (embed real domain as subdomain)
    "statscrop.com", "usitestat.com", "similarsites.com", "siteworthtraffic.com",
    "sitetrail.com", "hypestat.com", "websiteoutlook.com", "alexa.com",
    # FFL databases / dealer networks / gun directories
    "masterffl.com", "fflguru.com", "ffldealers.net", "fflshops.com",
    "ffls.com", "ffleasy.com", "findgundealers.com", "bluebookinc.com", "ffldealernetwork.com",
    "gunmade.com", "sportinggoods.com", "nrawomen.com", "scopesandbarrels.com",
    # Pawn / general shop directories
    "pawnshops.net", "pawnboss.com", "pawn-shops.net", "usapawnshop.com",
    "pawnshopmap.com", "pawnpros.com", "usedguns.com",
    # Gun case / accessory manufacturers whose dealer locators appear in results
    "gun-guard.com", "plano.com", "pelican.com", "nanuk.com",
    # Web analysis / place review sites
    "com-place.com", "place.com", "yelp.ca", "foursquare.com",
    "tripadvisor.com", "mapquest.com", "festivals.com", "eventbrite.com",
    "meetup.com", "thumbtack.com",
    # Manufacturer dealer locators (common ones)
    "henryusa.com", "beretta.com", "glock.com", "springfield-armory.com",
    "smith-wesson.com", "colt.com", "ruger.com", "sigsauer.com",
    "mossberg.com", "winchester.com", "remington.com", "browning.com",
    "fn-usa.com", "heckler-koch.com", "kimberamerica.com", "walther-arms.com",
    "usguns.com", "shooting.org", "nra.org", "nssf.org",
    # Pawnshop locator directories
    "pawnshoplocator.us", "pawnshoplocator.com",
    # Chamber of commerce sites (city names match dealer names, not the business)
    "chamberofcommerce.org", "chamber.org",
}

# URL path patterns that indicate a dealer locator / directory page (never the dealer's own site)
SKIP_PATH_PATTERNS = [
    "/dealer-location/", "/dealer-locator/", "/find-a-dealer/", "/dealers/",
    "/pawn-shops/", "/gun-shops/", "/ffl/", "/ffl-dealers/",
    "/store-locator/", "/store-finder/", "/find-dealer/",
    "/stores/",          # manufacturer dealer locator pages (e.g. henryusa.com/stores/...)
    "/retailer/",        # same pattern for some manufacturers
    "/pawn-shops-",      # directory sub-pages like masterffl.com/pawn-shops-texas/
]


def _domain(url: str) -> str:
    m = re.search(r'https?://(?:www\.)?([^/]+)', url)
    if not m:
        return ""
    host = m.group(1).lower()
    # Reject stat wrapper domains (real domain appears as subdomain of tracker)
    # e.g. "believerspawn.com.statscrop.com" → host has >1 registered domain
    parts = host.split(".")
    if len(parts) >= 4:  # x.y.tld.tracker.com pattern
        return host  # will be caught by SKIP_DOMAINS check
    return host


# Generic industry words that appear in many gun shop domains — not useful for matching
_GENERIC_TOKENS = {
    "gun", "guns", "pawn", "pawnshop", "arms", "armory", "armories",
    "firearm", "firearms", "rifle", "rifles", "pistol", "shop", "store",
    "inc", "llc", "ltd", "co", "corp", "company", "sales", "supply",
    "trading", "sporting", "outdoor", "outdoors", "goods", "center",
    "sports", "defense", "tactical", "shooting", "range", "ammo",
    "ammunition", "military", "surplus", "loan", "loans", "jewelry",
    "jewelers", "jewellery",
    # High-frequency words in pawn/loan shop names — too common to distinguish
    "cash", "city", "ace", "pro", "top", "big", "new", "old", "all",
    "usa", "america", "american",
}


def _name_to_slug_tokens(name: str) -> set:
    """Lowercase, strip punctuation, split into tokens."""
    name = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    return set(t for t in name.split() if len(t) > 2)


def _name_to_unique_tokens(name: str) -> set:
    """Return only non-generic tokens — used for domain matching gate."""
    return _name_to_slug_tokens(name) - _GENERIC_TOKENS


def _url_slug_matches(url: str, name: str, threshold: float = 0.45) -> bool:
    """Return True if ≥threshold of name tokens appear in the URL slug."""
    m = re.search(r'/ffl-dealers/([^/]+)', url)
    if not m:
        return False
    slug = m.group(1).lower()
    tokens = _name_to_slug_tokens(name)
    if not tokens:
        return False
    hits = sum(1 for t in tokens if t in slug)
    return (hits / len(tokens)) >= threshold


def search_masterffl(ddg: DDGS, dealer_name: str) -> tuple:
    """Return (city, state) or ('', '') from masterffl.com DDG search."""
    query = f'"{dealer_name}" site:masterffl.com'
    try:
        results = list(ddg.text(query, max_results=4))
        for r in results:
            url = r.get("href", "")
            if "masterffl.com/ffl-dealers/" not in url:
                continue
            if not _url_slug_matches(url, dealer_name):
                continue
            m = re.search(r'/ffl-dealers/[^/]+-([a-z][a-z\-]*[a-z])-([a-z]{2})-\d+', url)
            if m:
                city  = m.group(1).replace("-", " ").title()
                state = m.group(2).upper()
                if len(city) >= 3 and len(state) == 2:
                    return city, state
    except Exception as e:
        print(f"    [masterffl search error] {e}")
    return "", ""


def _is_valid_website(url: str, dealer_name: str) -> bool:
    """Return True if the URL looks like the dealer's own website."""
    dom = _domain(url)
    if not dom:
        return False
    # Skip known bad domains
    if any(skip in dom for skip in SKIP_DOMAINS):
        return False
    # Skip directory/locator path patterns
    url_lower = url.lower()
    if any(pat in url_lower for pat in SKIP_PATH_PATTERNS):
        return False
    return True


def _score_website(url: str, dealer_name: str) -> tuple:
    """
    Return (domain_score, total_score) for a candidate URL.
    domain_score: count of UNIQUE (non-generic) dealer name tokens in the domain.
                  Hard gate: must be ≥ 1 unless dealer name is all-generic.
    total_score:  domain_score + snippet bonus (used for ranking)
    """
    dom = _domain(url)
    dom_text = dom.replace("-", " ").replace(".", " ")
    unique_tokens = _name_to_unique_tokens(dealer_name)
    all_tokens    = _name_to_slug_tokens(dealer_name)

    if unique_tokens:
        # Require unique token ≥ 4 chars (avoid single-syllable abbreviation collisions)
        usable = {t for t in unique_tokens if len(t) >= 4}
        domain_score = sum(1 for t in usable if t in dom_text) if usable else 0
    else:
        # Dealer name is all-generic — only match if ALL generic tokens hit the domain
        # (e.g. "GC PAWN" → "pawn" in "gcpawn.com" is still plausible)
        all_usable = {t for t in all_tokens if len(t) >= 4}
        domain_score = sum(1 for t in all_usable if t in dom_text) if all_usable else 0

    return domain_score, domain_score


GUN_KEYWORDS = {
    "gun", "firearm", "pawn", "rifle", "pistol", "handgun", "ammo",
    "ammunition", "shooting", "hunt", "outdoor", "sport", "tactical",
    "weapon", "armer", "arms", "armory", "range", "jewelry",
}


def _snippet_is_relevant(snippet: str) -> bool:
    """Return True if the DDG result snippet mentions gun/pawn/firearm keywords."""
    lower = snippet.lower()
    return any(k in lower for k in GUN_KEYWORDS)


def _to_root_url(url: str) -> str:
    """Strip deep paths — return just https://domain.tld."""
    m = re.match(r'(https?://(?:www\.)?[^/]+)', url)
    return m.group(1) if m else url


def search_website(ddg: DDGS, dealer_name: str, city: str, state: str) -> str:
    """Return best website URL for this dealer, or '' if not found."""
    loc = f"{city} {state}".strip() if city or state else ""

    queries = [
        f'"{dealer_name}" {loc} official website'.strip(),
        f'{dealer_name} {loc} guns pawn shop'.strip(),
    ]

    candidates = []
    for query in queries:
        try:
            results = list(ddg.text(query, max_results=10))
            for r in results:
                url     = r.get("href", "")
                snippet = r.get("body", "") or r.get("snippet", "")
                if not _is_valid_website(url, dealer_name):
                    continue
                dom_score, total_score = _score_website(url, dealer_name)
                # HARD GATE: domain must contain at least 1 dealer name token
                if dom_score < 1:
                    continue
                # Bonus for snippet mentioning gun/pawn keywords
                if _snippet_is_relevant(snippet):
                    total_score += 2
                candidates.append((total_score, _to_root_url(url)))
            if candidates:
                break  # found at least one valid result in this query
            time.sleep(0.5)
        except Exception as e:
            print(f"    [website search error] {e}")

    if candidates:
        candidates.sort(key=lambda x: -x[0])
        return candidates[0][1]
    return ""


def load_cache() -> dict:
    p = Path(CACHE)
    if p.exists():
        return json.loads(p.read_text())
    return {}


def save_cache(cache: dict) -> None:
    Path(CACHE).write_text(json.dumps(cache, indent=2))


def main():
    rows = list(csv.DictReader(open(INPUT)))
    print(f"Loaded {len(rows)} dealers from {INPUT}")

    cache = load_cache()
    enriched_count = sum(1 for v in cache.values() if v.get("website") or v.get("city"))
    print(f"Cache: {len(cache)} entries ({enriched_count} with data)")

    fieldnames = [
        "dealer_name", "guns_com_profile_url",
        "location_city", "location_state", "location_address",
        "phone", "email", "website", "website_source",
        "total_listings", "new_listings", "used_listings",
        "ffl_verified", "dealer_rating", "review_count",
        "location_count", "merged_from", "possible_duplicate", "scraped_at",
        "website_confidence",
    ]

    output = []

    with DDGS() as ddg:
        for i, row in enumerate(rows, 1):
            name = row["dealer_name"]

            if name in cache:
                cached = cache[name]
                # If location was found but website wasn't, retry website search
                if cached.get("city") and not cached.get("website"):
                    city  = cached.get("city", "")
                    state = cached.get("state", "")
                    print(f"  [{i}/{len(rows)}] {name}  [retry website]")
                    website = search_website(ddg, name, city, state)
                    if website:
                        cached["website"] = website
                        cached["confidence"] = "high"
                        save_cache(cache)
                        print(f"    website: {website}  [high]")
                    else:
                        cached["confidence"] = "none"
                        save_cache(cache)
                    row["location_city"]      = city
                    row["location_state"]     = state
                    row["website"]            = website
                    row["website_source"]     = "ddg" if website else ""
                    row["website_confidence"] = cached.get("confidence", "none")
                    row["phone"]              = ""
                    output.append(row)
                    time.sleep(random.uniform(1.5, 2.5))
                    continue

                row["location_city"]    = cached.get("city", "")
                row["location_state"]   = cached.get("state", "")
                row["website"]          = cached.get("website", "")
                row["website_source"]   = "ddg" if cached.get("website") else ""
                row["website_confidence"] = cached.get("confidence", "")
                row["phone"]            = ""  # clear all — all scraped phones were fake
                output.append(row)
                if i % 50 == 0:
                    print(f"  [{i}/{len(rows)}] (from cache) {name}")
                continue

            is_chain = int(row.get("location_count", 1)) > 1
            print(f"  [{i}/{len(rows)}] {name}{'  [chain]' if is_chain else ''}")

            city, state = "", ""
            if not is_chain:
                city, state = search_masterffl(ddg, name)
                if city:
                    print(f"    location: {city}, {state}")
                time.sleep(random.uniform(1.0, 2.0))

            website = search_website(ddg, name, city, state)
            confidence = "high" if (city and website) else ("medium" if website else "none")
            if website:
                print(f"    website: {website}  [{confidence}]")

            cache[name] = {"city": city, "state": state,
                           "website": website, "confidence": confidence}
            save_cache(cache)

            row["location_city"]      = city
            row["location_state"]     = state
            row["website"]            = website
            row["website_source"]     = "ddg" if website else ""
            row["website_confidence"] = confidence
            row["phone"]              = ""   # clear all fake phones
            output.append(row)

            time.sleep(random.uniform(1.5, 3.0))

    # Write output
    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for row in output:
            w.writerow(row)

    # Stats
    has_city    = sum(1 for r in output if r.get("location_city"))
    has_website = sum(1 for r in output if r.get("website"))
    high_conf   = sum(1 for r in output if r.get("website_confidence") == "high")
    med_conf    = sum(1 for r in output if r.get("website_confidence") == "medium")

    print(f"\nResults:")
    print(f"  Total rows      : {len(output)}")
    print(f"  With city/state : {has_city}")
    print(f"  With website    : {has_website}")
    print(f"    High confidence : {high_conf}")
    print(f"    Medium         : {med_conf}")
    print(f"\nWritten → {OUTPUT}")


if __name__ == "__main__":
    main()
