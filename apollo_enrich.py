#!/usr/bin/env python3
"""
Phase 3: Enrich dealers missing verified websites via Apollo.io.

Apollo.io is a B2B company database with verified business domains.
Searches by dealer name + location, domain-gates the result,
then fetches the page to confirm the website belongs to that specific dealer.

Setup:
    1. Go to https://app.apollo.io/#/settings/integrations/api
    2. Create an API key
    3. Add to .env:  APOLLO_API_KEY=your_key_here

Run: python3 apollo_enrich.py
"""

import csv
import json
import os
import re
import time
import random
import urllib.request
import urllib.parse
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

ENRICHED     = "dealers_enriched.csv"
VERIFY       = "verify_results.csv"
CACHE        = "cache/apollo_cache.json"
APOLLO_KEY   = os.getenv("APOLLO_API_KEY", "")
APOLLO_URL   = "https://api.apollo.io/v1/mixed_companies/search"

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
    "shooting.org", "usedguns.com", "nra.org",
    "henryusa.com", "beretta.com", "glock.com", "ruger.com", "sigsauer.com",
    "mossberg.com", "smith-wesson.com",
    "statscrop.com", "usitestat.com", "hypestat.com", "alexa.com",
    "gun-guard.com", "foursquare.com", "tripadvisor.com",
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

_GENERIC = {
    "gun", "guns", "pawn", "pawnshop", "arms", "armory", "armories",
    "firearm", "firearms", "rifle", "rifles", "pistol", "shop", "store",
    "inc", "llc", "ltd", "co", "corp", "company", "sales", "supply",
    "trading", "sporting", "outdoor", "outdoors", "goods", "center",
    "sports", "defense", "tactical", "shooting", "range", "ammo",
    "ammunition", "military", "surplus", "loan", "loans", "jewelry",
    "jewelers", "jewellery", "cash", "city", "ace", "pro", "top",
    "big", "new", "old", "all", "usa", "america", "american",
}

try:
    from bs4 import BeautifulSoup
    BS4 = True
except ImportError:
    BS4 = False

_HTTP_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.5",
}


# ── Shared helpers ────────────────────────────────────────────────────────────

def _domain(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else ""


def _all_tokens(name: str) -> set:
    return {t for t in re.sub(r"[^a-z0-9 ]", " ", name.lower()).split() if len(t) > 2}


def _unique_tokens(name: str) -> set:
    return _all_tokens(name) - _GENERIC


def _domain_score(url: str, name: str) -> int:
    dom = _domain(url).replace("-", " ").replace(".", " ")
    ut = _unique_tokens(name)
    usable = {t for t in (ut or _all_tokens(name)) if len(t) >= 4}
    return sum(1 for t in usable if t in dom) if usable else 0


def _is_valid(url: str, name: str) -> bool:
    dom = _domain(url)
    if not dom:
        return False
    if any(s in dom for s in SKIP_DOMAINS):
        return False
    if any(p in url.lower() for p in SKIP_PATHS):
        return False
    if _domain_score(url, name) < 1:
        return False
    return True


def _fetch_page(url: str) -> str:
    try:
        req = urllib.request.Request(url, headers=_HTTP_HEADERS)
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read(65536)
            html = raw.decode(resp.headers.get_content_charset("utf-8"), errors="ignore")
    except Exception as e:
        return f"[FETCH_ERROR: {e}]"

    if not BS4:
        return html.lower()

    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    title    = soup.title.string if soup.title else ""
    meta     = ""
    for m in soup.find_all("meta", attrs={"name": re.compile("description", re.I)}):
        meta = m.get("content", "")
        break
    body = soup.get_text(" ", strip=True)[:4000]
    return f"{title} {meta} {body}".lower()


def _name_tokens(name: str) -> list:
    raw = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    tokens = [t for t in raw if len(t) >= 4 and t not in _GENERIC]
    return tokens if tokens else [t for t in raw if len(t) >= 4]


def verify_website(url: str, name: str, city: str, state: str) -> str:
    """Fetch the URL and return: name_match | location_match | fetch_failed | unverified."""
    tokens = _name_tokens(name)
    content = _fetch_page(url)

    if content.startswith("[FETCH_ERROR"):
        return "fetch_failed"
    if any(t in content for t in tokens):
        return "name_match"
    if city and len(city) > 3 and city.lower().strip() in content:
        return "location_match"
    if state and state.lower() in content:
        return "location_match"
    return "unverified"


def _to_root(url: str) -> str:
    m = re.match(r"(https?://(?:www\.)?[^/]+)", url)
    return m.group(1) if m else url


# ── Apollo search ─────────────────────────────────────────────────────────────

def apollo_search(name: str, city: str, state: str) -> str:
    """Search Apollo.io for company website. Returns root URL or ''."""
    payload = json.dumps({
        "q_organization_name": name,
        "organization_locations": [f"{city}, {state}"] if city and state else [],
        "per_page": 5,
        "page": 1,
    }).encode("utf-8")

    req = urllib.request.Request(
        APOLLO_URL, data=payload,
        headers={
            "Content-Type": "application/json",
            "Cache-Control": "no-cache",
            "x-api-key": APOLLO_KEY,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            data = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            raise RuntimeError(f"Apollo auth error {e.code} — check your API key")
        if e.code == 429:
            print(f"    [Apollo] rate-limited — sleeping 60s")
            time.sleep(60)
        else:
            print(f"    [Apollo] HTTP {e.code}")
        return ""
    except Exception as e:
        print(f"    [Apollo] error: {e}")
        return ""

    candidates = []
    for org in data.get("organizations", []):
        website = org.get("website_url") or ""
        if not website:
            dom = org.get("primary_domain", "")
            website = f"https://{dom}" if dom else ""
        if not website:
            continue
        if not website.startswith("http"):
            website = f"https://{website}"
        if not _is_valid(website, name):
            continue
        score = _domain_score(website, name)
        candidates.append((score, _to_root(website)))

    if not candidates:
        return ""
    candidates.sort(key=lambda x: -x[0])
    return candidates[0][1]


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    if not APOLLO_KEY:
        print("ERROR: APOLLO_API_KEY not set.")
        print("  1. Get key: https://app.apollo.io/#/settings/integrations/api")
        print("  2. Add to .env:  APOLLO_API_KEY=your_key_here")
        return

    rows = list(csv.DictReader(open(ENRICHED)))
    print(f"Loaded {len(rows)} dealers from {ENRICHED}")

    verify_status = {}
    if Path(VERIFY).exists():
        for r in csv.DictReader(open(VERIFY)):
            verify_status[r["dealer_name"]] = r["verify_status"]

    NEEDS_WORK = {"fetch_failed", "unverified"}
    targets = [
        r for r in rows
        if not r.get("website")
        or verify_status.get(r["dealer_name"], "") in NEEDS_WORK
    ]
    print(f"Targets: {len(targets)} dealers")

    cache = json.loads(Path(CACHE).read_text()) if Path(CACHE).exists() else {}
    todo  = [r for r in targets if r["dealer_name"] not in cache]
    print(f"Cache: {len(cache)} done | Remaining: {len(todo)}")

    found = not_found = bad = 0

    for i, row in enumerate(todo, 1):
        name  = row["dealer_name"]
        city  = row.get("location_city", "")
        state = row.get("location_state", "")

        print(f"[{i}/{len(todo)}] {name[:50]}", end="  ", flush=True)

        try:
            website = apollo_search(name, city, state)
        except RuntimeError as e:
            print(f"\nFATAL: {e}")
            break

        if not website:
            print("not in Apollo")
            cache[name] = {"website": "", "verify_status": "not_found", "source": "apollo"}
            not_found += 1
        else:
            status = verify_website(website, name, city, state)
            if status in ("name_match", "location_match", "fetch_failed"):
                symbol = "✓" if status == "name_match" else ("~" if status == "location_match" else "?CF")
                print(f"{symbol} {website}  [{status}]")
                cache[name] = {"website": website, "verify_status": status, "source": "apollo"}
                found += 1
            else:
                print(f"✗ {website}  [unverified — discarding]")
                cache[name] = {"website": "", "verify_status": "unverified", "source": "apollo"}
                bad += 1

        Path(CACHE).write_text(json.dumps(cache, indent=2))
        time.sleep(random.uniform(0.4, 1.0))

    # Apply to CSV
    print(f"\nApplying to {ENRICHED}...")
    row_by_name = {r["dealer_name"]: r for r in rows}
    updated = 0

    for name, result in cache.items():
        if name not in row_by_name:
            continue
        current_vs = verify_status.get(name, "")
        if current_vs in ("name_match", "location_match"):
            continue  # don't overwrite already-verified

        website = result.get("website", "")
        status  = result.get("verify_status", "")
        if website and status in ("name_match", "location_match", "fetch_failed"):
            row_by_name[name]["website"]          = website
            row_by_name[name]["website_source"]   = "apollo"
            row_by_name[name]["website_verified"] = status
            updated += 1

    fieldnames = list(rows[0].keys())
    for col in ("website_source", "website_verified"):
        if col not in fieldnames:
            fieldnames.insert(fieldnames.index("website") + 1, col)
            for r in rows:
                r.setdefault(col, "")

    with open(ENRICHED, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    total = found + not_found + bad
    print(f"\nApollo results ({total} processed):")
    print(f"  Found + accepted : {found}")
    print(f"  Not in Apollo    : {not_found}")
    print(f"  Found but bad    : {bad}")
    print(f"  Written to CSV   : {updated}")
    print(f"\nNext: python3 exa_fill_gaps.py")


if __name__ == "__main__":
    main()
