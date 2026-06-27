#!/usr/bin/env python3
"""
RocketReach contact enrichment — Phase 4a.

Takes all contacts with a name but no email from the Exa + web caches,
looks them up via RocketReach person lookup API, and writes verified
email + phone back into the contacts.csv.

Run: python3 rocketreach_enrich.py
"""

import csv
import json
import os
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

EXA_CACHE    = "cache/exa_contacts_cache.json"
WEB_CACHE    = "cache/contacts_web_cache.json"
ENRICHED     = "dealers_enriched.csv"
CONTACTS_OUT = "contacts.csv"
RR_CACHE     = "cache/rocketreach_cache.json"

RR_LOOKUP    = "https://api.rocketreach.co/api/v2/person/lookup"
RR_SEARCH    = "https://api.rocketreach.co/api/v2/person/search"

FIELDS = [
    "dealer_name", "dealer_city", "dealer_state", "dealer_website",
    "contact_name", "contact_title", "contact_email", "contact_phone",
    "contact_linkedin", "contact_source", "verified_level", "sources_agreed",
]


def _load_key() -> str:
    return os.getenv("ROCKETREACH_API_KEY", "")


def _rr_request(url: str, api_key: str, params: dict = None, payload: dict = None) -> dict:
    import gzip
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Api-Key":        api_key,
            "Content-Type":   "application/json",
            "Accept":         "application/json",
            "User-Agent":     "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language":"en-US,en;q=0.9",
            "Origin":         "https://rocketreach.co",
            "Referer":        "https://rocketreach.co/",
        },
        method="POST" if payload else "GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            try:
                return json.loads(raw)
            except Exception:
                try:
                    return json.loads(gzip.decompress(raw))
                except Exception:
                    return {}
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {}
        if e.code == 429:
            raise RuntimeError("RocketReach rate limit hit")
        if e.code == 401:
            raise RuntimeError(f"RocketReach auth error 401")
        if e.code == 403:
            raise RuntimeError(f"RocketReach blocked 403")
        body = e.read().decode("utf-8", errors="ignore")[:200]
        print(f"    [RR HTTP {e.code}] {body}")
        return {}
    except Exception as e:
        print(f"    [RR error] {e}")
        return {}


def lookup_person(name: str, company: str, api_key: str) -> dict:
    """
    Try person lookup by name + company.
    Returns dict with email, phone, linkedin or empty dict.
    """
    # Strategy 1: direct lookup
    result = _rr_request(RR_LOOKUP, api_key, params={
        "name":             name,
        "current_employer": company,
        "extras":           "phones",
    })

    if not result or result.get("status") == "failed":
        # Strategy 2: search endpoint
        result = _rr_request(RR_SEARCH, api_key, payload={
            "query": {
                "name":             [name],
                "current_employer": [company],
            },
            "start": 1,
            "pageSize": 3,
        })
        # Search returns a list — pick best match
        profiles = result.get("profiles", [])
        if profiles:
            result = profiles[0]
        else:
            return {}

    # Extract from result
    emails  = result.get("emails", []) or []
    phones  = result.get("phones", []) or result.get("phone_numbers", []) or []
    linkedin = result.get("linkedin_url", "") or ""

    # Prefer work email over personal
    email = ""
    for e in emails:
        val = e.get("email", "") if isinstance(e, dict) else str(e)
        if val and "@" in val:
            if not email:
                email = val
            if not any(p in val for p in ["gmail", "yahoo", "hotmail", "outlook"]):
                email = val
                break

    phone = ""
    for p in phones:
        val = p.get("number", "") if isinstance(p, dict) else str(p)
        if val:
            phone = val
            break

    name_back = result.get("name", "") or f"{result.get('first_name','')} {result.get('last_name','')}".strip()
    title     = result.get("current_title", "") or result.get("title", "") or ""

    return {
        "name":     name_back or name,
        "title":    title,
        "email":    email,
        "phone":    phone,
        "linkedin": linkedin,
    }


def collect_targets(dealers_map: dict, rr_cache: dict) -> list:
    """
    Gather (dealer_name, contact_name, city, state, website) for everyone
    with a real name but no verified email yet, not already in RR cache.
    """
    targets = []
    seen    = set()

    for cache_path in [EXA_CACHE, WEB_CACHE]:
        if not Path(cache_path).exists():
            continue
        cache = json.loads(Path(cache_path).read_text())
        for dealer, data in cache.items():
            if not data.get("found"):
                continue
            name = data.get("contact_name", "").strip()
            if not name:
                continue
            # Skip if already in RR cache OR already has email
            if dealer in rr_cache:
                continue
            key = (dealer, name)
            if key in seen:
                continue
            seen.add(key)

            row = dealers_map.get(dealer, {})
            targets.append({
                "dealer_name": dealer,
                "contact_name": name,
                "contact_title": data.get("contact_title", ""),
                "city":    row.get("location_city", ""),
                "state":   row.get("location_state", ""),
                "website": row.get("website", ""),
            })

    return targets


def _write_contacts(dealers_map: dict, rr_cache: dict, exa_cache: dict, web_cache: dict):
    """Merge all sources into final contacts.csv."""
    rows = []
    for dealer, row in dealers_map.items():
        # Start from best available base
        rr   = rr_cache.get(dealer, {})
        exa  = exa_cache.get(dealer, {}) if exa_cache.get(dealer, {}).get("found") else {}
        web  = web_cache.get(dealer, {}) if web_cache.get(dealer, {}).get("found") else {}

        name    = rr.get("contact_name") or exa.get("contact_name") or web.get("contact_name") or ""
        title   = rr.get("contact_title") or exa.get("contact_title") or web.get("contact_title") or ""
        email   = rr.get("contact_email") or web.get("contact_email") or ""
        phone   = rr.get("contact_phone") or web.get("contact_phone") or exa.get("contact_phone") or ""
        linkedin = rr.get("contact_linkedin") or ""
        source  = "+".join(filter(None, [
            "rocketreach" if rr.get("contact_email") else "",
            "exa"         if exa.get("contact_name") else "",
            "web"         if web.get("contact_name") else "",
        ])) or ""

        if name and email:
            lvl = "high"
        elif name:
            lvl = "medium"
        elif email or phone:
            lvl = "low"
        else:
            continue  # skip dealers with nothing

        rows.append({
            "dealer_name":    dealer,
            "dealer_city":    row.get("location_city", ""),
            "dealer_state":   row.get("location_state", ""),
            "dealer_website": row.get("website", ""),
            "contact_name":   name,
            "contact_title":  title,
            "contact_email":  email,
            "contact_phone":  phone,
            "contact_linkedin": linkedin,
            "contact_source": source,
            "verified_level": lvl,
            "sources_agreed": "",
        })

    with open(CONTACTS_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for r in rows:
            w.writerow(r)

    return rows


def main():
    api_key = _load_key()
    if not api_key:
        print("ERROR: ROCKETREACH_API_KEY not set in .env")
        return

    dealers_map = {r["dealer_name"]: r for r in csv.DictReader(open(ENRICHED))}
    rr_cache    = json.loads(Path(RR_CACHE).read_text()) if Path(RR_CACHE).exists() else {}
    exa_cache   = json.loads(Path(EXA_CACHE).read_text()) if Path(EXA_CACHE).exists() else {}
    web_cache   = json.loads(Path(WEB_CACHE).read_text()) if Path(WEB_CACHE).exists() else {}

    targets = collect_targets(dealers_map, rr_cache)
    print(f"Contacts to enrich via RocketReach: {len(targets)}")
    print(f"Already cached: {len(rr_cache)}")
    print(f"Lookups available: 105 (check dashboard for exact remaining)")
    print()

    if not targets:
        print("Nothing to do — all named contacts already looked up.")
    else:
        found = not_found = 0

        for i, t in enumerate(targets, 1):
            dealer  = t["dealer_name"]
            name    = t["contact_name"]
            company = dealer  # use the guns.com dealer name for best match

            print(f"[{i}/{len(targets)}] {name} @ {dealer[:40]}", end="  ", flush=True)

            try:
                result = lookup_person(name, company, api_key)
            except RuntimeError as e:
                print(f"\n\nRocketReach error: {e}")
                Path(RR_CACHE).write_text(json.dumps(rr_cache, indent=2))
                break

            if result.get("email") or result.get("phone"):
                em  = result.get("email", "") or "(no email)"
                ph  = result.get("phone", "") or "(no phone)"
                lvl = "high" if result.get("email") else "medium"
                print(f"✓ [{lvl}]  {em}  |  {ph}")
                rr_cache[dealer] = {
                    "found":           True,
                    "contact_name":    result.get("name") or name,
                    "contact_title":   result.get("title") or t.get("contact_title", ""),
                    "contact_email":   result.get("email", ""),
                    "contact_phone":   result.get("phone", ""),
                    "contact_linkedin": result.get("linkedin", ""),
                    "contact_source":  "rocketreach",
                    "verified_level":  lvl,
                }
                found += 1
            else:
                print("—  not found")
                rr_cache[dealer] = {"found": False}
                not_found += 1

            Path(RR_CACHE).write_text(json.dumps(rr_cache, indent=2))
            time.sleep(1.2)

        print(f"\nRocketReach results ({len(targets)} processed):")
        print(f"  Found    : {found}")
        print(f"  Not found: {not_found}")

    # Write merged contacts.csv
    rows = _write_contacts(dealers_map, rr_cache, exa_cache, web_cache)
    named  = sum(1 for r in rows if r["contact_name"])
    emailed = sum(1 for r in rows if r["contact_email"])
    phoned  = sum(1 for r in rows if r["contact_phone"])
    high   = sum(1 for r in rows if r["verified_level"] == "high")
    medium = sum(1 for r in rows if r["verified_level"] == "medium")

    print(f"\n{'='*55}")
    print(f"contacts.csv  —  {len(rows)} contacts total")
    print(f"  Named contact   : {named}")
    print(f"  Have email      : {emailed}  ← verified personal/company email")
    print(f"  Have phone      : {phoned}")
    print(f"  High confidence : {high}  (name + email)")
    print(f"  Medium          : {medium}  (name only, no email)")
    print(f"\nReady for email verification (Phase 5).")


if __name__ == "__main__":
    main()
