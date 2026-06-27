#!/usr/bin/env python3
"""
Use Exa to find websites for dealers that guns.com profile didn't have,
and to fetch content for Cloudflare-protected sites that requests couldn't read.

Browser-login flow:
  1. Opens https://dashboard.exa.ai in Chrome (your SeleniumBase session)
  2. You log in with Account #1 — script auto-extracts the API key
  3. Runs Exa API calls until credits exhausted (HTTP 402 / 429)
  4. Prints "Credits exhausted — log in with next account"
  5. Re-opens dashboard, you log in with Account #2
  6. Continues from exactly where it stopped

Run interactively (VPN on):
    python3 exa_fill_gaps.py

Resume anytime — progress saved to exa_cache.json after each dealer.
"""

import csv
import json
import re
import time
import random
import urllib.request
import urllib.parse
import urllib.error
from pathlib import Path

ENRICHED       = "dealers_enriched.csv"
VERIFY_RESULTS = "verify_results.csv"
EXA_CACHE      = "cache/exa_cache.json"
OUTPUT         = "dealers_enriched.csv"

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_FETCH_URL  = "https://api.exa.ai/contents"
EXA_KEY_FILE   = "cache/exa_key.txt"

# Statuses that still need work after requests-based verification
STILL_NEEDS    = {"fetch_failed", "unverified"}

_GENERIC = {
    "gun", "guns", "pawn", "pawnshop", "arms", "armory", "firearm", "firearms",
    "rifle", "shop", "store", "inc", "llc", "ltd", "co", "corp", "company",
    "sales", "supply", "sporting", "outdoor", "outdoors", "goods", "center",
    "sports", "defense", "tactical", "shooting", "range", "ammo", "ammunition",
    "military", "surplus", "loan", "jewelry", "cash", "city", "and", "the",
}

SKIP_DOMAINS = {
    "guns.com", "gunbroker.com", "armslist.com", "gunsamerica.com",
    "facebook.com", "instagram.com", "twitter.com", "youtube.com", "yelp.com",
    "google.com", "bing.com", "wikipedia.org", "amazon.com", "ebay.com",
    "yellowpages.com", "whitepages.com", "manta.com",
    "masterffl.com", "fflguru.com", "ffls.com", "ffleasy.com", "gunmade.com",
    "pawnshops.net", "pawnshopmap.com", "pawnbat.com",
    "hibid.com", "proxibid.com", "liveauctioneers.com",
    "henryusa.com", "beretta.com", "glock.com", "ruger.com", "sigsauer.com",
    "nra.org", "linkedin.com",
}
SKIP_PATHS = ["/dealer-location/", "/dealers/", "/stores/", "/find-a-dealer/",
              "/pawn-shops/", "/ffl/", "/store-locator/"]


# ── Domain scoring ────────────────────────────────────────────────────────────

def _domain(url: str) -> str:
    m = re.search(r"https?://(?:www\.)?([^/]+)", url)
    return m.group(1).lower() if m else ""


def _unique_tokens(name: str) -> set:
    raw = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    u = {t for t in raw if len(t) >= 4 and t not in _GENERIC}
    return u if u else {t for t in raw if len(t) >= 4}


def _domain_score(url: str, name: str) -> int:
    dom = _domain(url).replace("-", " ").replace(".", " ")
    return sum(1 for t in _unique_tokens(name) if t in dom)


def _is_valid(url: str, name: str) -> bool:
    if not url or not url.startswith("http"):
        return False
    dom = _domain(url)
    if any(s in dom for s in SKIP_DOMAINS):
        return False
    ul = url.lower()
    if any(p in ul for p in SKIP_PATHS):
        return False
    if _domain_score(url, name) < 1:
        return False
    return True


def _root_url(url: str) -> str:
    m = re.match(r"(https?://(?:www\.)?[^/]+)", url)
    return m.group(1) if m else url


# ── API key — paste from your own browser ────────────────────────────────────

def _get_api_key(account_num: int = 1) -> str:
    """
    Ask the user to paste their Exa API key from their own browser.
    Saves to exa_key.txt so resume runs don't re-ask.
    """
    # On first call, check if a saved key exists
    if account_num == 1 and Path(EXA_KEY_FILE).exists():
        key = Path(EXA_KEY_FILE).read_text().strip()
        if key:
            print(f"  Loaded saved Exa key: {key[:8]}...{key[-4:]}")
            return key

    print("\n" + "=" * 60)
    print(f"  EXA API KEY NEEDED (Account #{account_num})")
    print("=" * 60)
    print("  In your normal browser (Chrome/Firefox):")
    print("  1. Go to  https://dashboard.exa.ai/api-keys")
    print("  2. Sign in with your Exa account")
    print("  3. Copy your API key (starts with  ex-...)")
    print("  4. Paste it below and press ENTER")
    print("=" * 60)
    try:
        key = input("  >>> Paste Exa API key: ").strip()
    except EOFError:
        return ""

    if key:
        Path(EXA_KEY_FILE).write_text(key)
        print(f"  Key saved ({key[:8]}...{key[-4:]})")
    return key


# ── Exa API calls ─────────────────────────────────────────────────────────────

class ExaQuotaExhausted(Exception):
    pass


def _exa_request(endpoint: str, payload: dict, api_key: str) -> dict:
    """Make a single Exa API call. Raises ExaQuotaExhausted on 402/429."""
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        endpoint,
        data=data,
        headers={
            "Content-Type": "application/json",
            "x-api-key": api_key,
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (402, 429):
            raise ExaQuotaExhausted(f"HTTP {e.code}")
        body = e.read().decode("utf-8", errors="ignore")[:200]
        raise RuntimeError(f"Exa HTTP {e.code}: {body}")


def exa_search(name: str, city: str, state: str, api_key: str) -> str:
    """Search Exa for the dealer's official website. Returns URL or ''."""
    loc = f"{city} {state}".strip() if city else ""
    query = f'"{name}" {loc} official website guns firearms pawn'.strip()

    try:
        resp = _exa_request(
            EXA_SEARCH_URL,
            {
                "query": query,
                "numResults": 8,
                "type": "neural",
                "useAutoprompt": True,
            },
            api_key,
        )
        results = resp.get("results", [])
        candidates = []
        for r in results:
            url = r.get("url", "")
            if _is_valid(url, name):
                score = _domain_score(url, name)
                candidates.append((score, _root_url(url)))
        if candidates:
            candidates.sort(key=lambda x: -x[0])
            return candidates[0][1]
    except ExaQuotaExhausted:
        raise
    except Exception as e:
        print(f"    [exa search error] {e}")
    return ""


def exa_fetch(url: str, name: str, api_key: str) -> str:
    """
    Fetch a URL via Exa (bypasses Cloudflare).
    Returns page text (title + body) or '' on error.
    """
    try:
        resp = _exa_request(
            EXA_FETCH_URL,
            {"ids": [url], "text": True},
            api_key,
        )
        results = resp.get("results", [])
        if results:
            r = results[0]
            title = r.get("title", "")
            text  = r.get("text", "")[:3000]
            return f"{title} {text}".lower()
    except ExaQuotaExhausted:
        raise
    except Exception as e:
        print(f"    [exa fetch error] {e}")
    return ""


# ── Verification helper ────────────────────────────────────────────────────────

def verify_content(content: str, name: str, city: str) -> str:
    """Return 'name_match', 'location_match', or 'unverified'."""
    tokens = [t for t in _unique_tokens(name) if len(t) >= 4]
    if any(t in content for t in tokens):
        return "name_match"
    if city and len(city) > 3 and city.lower() in content:
        return "location_match"
    return "unverified"


# ── Main ─────────────────────────────────────────────────────────────────────

def _load_json(path: str) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def _save_json(path: str, data: dict) -> None:
    Path(path).write_text(json.dumps(data, indent=2))


def main():
    if not Path(ENRICHED).exists():
        print(f"ERROR: {ENRICHED} not found")
        return

    rows = list(csv.DictReader(open(ENRICHED)))
    print(f"Loaded {len(rows)} dealers")

    # Load verify results
    verify_status = {}
    if Path(VERIFY_RESULTS).exists():
        for r in csv.DictReader(open(VERIFY_RESULTS)):
            verify_status[r["dealer_name"]] = r["verify_status"]

    # Determine targets
    # Mode A: fetch_failed — has a website URL but couldn't verify (Cloudflare)
    # Mode B: no website or unverified — need a new URL from Exa search
    fetch_fail_targets = []
    search_targets     = []

    for row in rows:
        name    = row["dealer_name"]
        website = row.get("website", "")
        source  = row.get("website_source", "")
        status  = verify_status.get(name, "")
        already_verified = status in ("name_match", "location_match") or source == "guns_com_registered"

        if already_verified:
            continue

        if website and status == "fetch_failed":
            fetch_fail_targets.append(row)
        elif not website or status == "unverified":
            search_targets.append(row)

    print(f"\nTargets:")
    print(f"  Fetch+verify (Cloudflare-blocked sites) : {len(fetch_fail_targets)}")
    print(f"  Search+find (no website or wrong one)   : {len(search_targets)}")
    total_targets = len(fetch_fail_targets) + len(search_targets)
    if total_targets == 0:
        print("Nothing to do — all dealers verified.")
        return

    # Load cache
    cache = _load_json(EXA_CACHE)
    done  = set(cache.keys())

    fetch_todo  = [r for r in fetch_fail_targets if r["dealer_name"] not in done]
    search_todo = [r for r in search_targets     if r["dealer_name"] not in done]
    todo_all    = fetch_todo + search_todo

    print(f"  Already in cache    : {len(done)}")
    print(f"  Remaining to process: {len(todo_all)}")

    if not todo_all:
        print("All targets in cache — applying results.")
    else:
        key = _get_api_key(account_num=1)
        if not key:
            print("No API key provided — exiting.")
            return

        print(f"\nStarting Exa enrichment — {len(todo_all)} dealers to process\n")

        total_done = 0
        try:
            for i, row in enumerate(todo_all, 1):
                name    = row["dealer_name"]
                website = row.get("website", "")
                city    = row.get("location_city", "")
                state   = row.get("location_state", "")
                is_fetch_mode = bool(website) and verify_status.get(name) == "fetch_failed"

                print(f"[{i}/{len(todo_all)}] {name[:50]}")

                while True:
                    try:
                        if is_fetch_mode:
                            # Mode A: fetch the CF-blocked page via Exa
                            content = exa_fetch(website, name, key)
                            if content:
                                v = verify_content(content, name, city)
                                cache[name] = {
                                    "website": website,
                                    "verify_status": v,
                                    "source": "exa_fetch",
                                }
                                print(f"  fetch → {v}")
                            else:
                                cache[name] = {"website": website,
                                               "verify_status": "fetch_failed_exa",
                                               "source": "exa_fetch"}
                                print(f"  fetch → could not load")
                        else:
                            # Mode B: search for website
                            found_url = exa_search(name, city, state, key)
                            if found_url:
                                content = exa_fetch(found_url, name, key)
                                v = verify_content(content, name, city) if content else "found_unverified"
                                cache[name] = {
                                    "website": found_url,
                                    "verify_status": v,
                                    "source": "exa_search",
                                }
                                print(f"  found: {found_url}  [{v}]")
                            else:
                                cache[name] = {"website": "", "verify_status": "not_found",
                                               "source": "exa_search"}
                                print(f"  not found")
                        break  # success — move to next dealer

                    except ExaQuotaExhausted:
                        print(f"\n{'='*60}")
                        print("  EXA CREDITS EXHAUSTED")
                        print(f"  Processed {total_done} dealers so far.")
                        print("  Log in with your next Exa account to continue.")
                        print(f"{'='*60}")
                        _save_json(EXA_CACHE, cache)
                        Path(EXA_KEY_FILE).unlink(missing_ok=True)  # clear saved key

                        key = _get_api_key(account_num=2)
                        if not key:
                            print("No key provided — saving progress and exiting.")
                            raise KeyboardInterrupt

                _save_json(EXA_CACHE, cache)
                total_done += 1
                time.sleep(random.uniform(0.5, 1.5))

        except KeyboardInterrupt:
            print(f"\nInterrupted — {total_done} processed, cache saved.")

    # Apply results to dealers_enriched.csv
    print("\nApplying Exa results to dealers_enriched.csv...")

    row_by_name = {r["dealer_name"]: r for r in rows}
    updated = 0
    for name, cached in cache.items():
        if name not in row_by_name:
            continue
        row    = row_by_name[name]
        status = cached.get("verify_status", "")
        url    = cached.get("website", "")
        source = cached.get("source", "exa")

        if url and status in ("name_match", "location_match", "found_unverified"):
            # Only update if not already better
            current_source = row.get("website_source", "")
            if current_source not in ("guns_com_registered",):
                row["website"]          = url
                row["website_source"]   = source
                row["website_verified"] = status
                updated += 1

    # Ensure new columns exist
    fieldnames = list(rows[0].keys())
    for col in ("website_source", "website_verified"):
        if col not in fieldnames:
            fieldnames.append(col)
            for r in rows:
                r.setdefault(col, "")

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)

    print(f"Updated {updated} dealers with Exa data.")

    # Final stats
    has_web = sum(1 for r in rows if r.get("website"))
    verified = sum(1 for r in rows
                   if r.get("website_verified") in ("name_match", "location_match",
                                                     "guns_com_registered"))
    print(f"\nFinal totals in {OUTPUT}:")
    print(f"  With website      : {has_web}/{len(rows)} ({has_web/len(rows)*100:.0f}%)")
    print(f"  Verified (strong) : {verified}/{len(rows)} ({verified/len(rows)*100:.0f}%)")
    print(f"\nNext step: python3 verify_websites.py --only-unverified")


if __name__ == "__main__":
    main()
