#!/usr/bin/env python3
"""
RocketReach domain-based enrichment — Phase 4f.

For dealers with a verified website but no contact yet, searches RocketReach
by domain to find owner/manager-level people, then enriches for email + phone.

Different from rocketreach_enrich.py (which needed a known name).
This discovers names from the domain first.

Strategy:
  POST /api/v2/person/search  with current_employer_domain filter
  → pick best owner/manager prospect
  → GET /api/v2/person/lookup?id=X  for full email + phone

Run: python3 rocketreach_domain_enrich.py
"""

import csv
import json
import os
import re
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

ENRICHED    = "dealers_enriched.csv"
CONTACTS_OUT = "contacts.csv"
RR_DOMAIN_CACHE = "cache/rocketreach_domain_cache.json"

RR_SEARCH   = "https://api.rocketreach.co/api/v2/person/search"
RR_LOOKUP   = "https://api.rocketreach.co/api/v2/person/lookup"

# Priority order for picking the best prospect
TITLE_PRIORITY = [
    "owner", "co-owner", "proprietor", "founder",
    "president", "ceo", "chief executive",
    "general manager", "operations manager", "operations director",
    "director of operations", "store manager", "shop manager", "manager",
]

# Skip dealers already well-covered
SKIP_QUALITY_FLAGS = {"false_positive"}


def _load_key() -> str:
    return os.getenv("ROCKETREACH_API_KEY", "")


def _rr_request(url: str, api_key: str, params: dict = None,
                payload: dict = None, _retries: int = 3) -> dict:
    import gzip
    if params:
        url = url + "?" + urllib.parse.urlencode(params)
    data = json.dumps(payload).encode() if payload else None
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "Api-Key":         api_key,
            "Content-Type":    "application/json",
            "Accept":          "application/json",
            "User-Agent":      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
            "Accept-Language": "en-US,en;q=0.9",
            "Origin":          "https://rocketreach.co",
            "Referer":         "https://rocketreach.co/",
        },
        method="POST" if payload else "GET",
    )
    for attempt in range(_retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as resp:
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
            if e.code in (401, 403):
                raise RuntimeError(f"RocketReach auth error {e.code}")
            body = e.read().decode("utf-8", errors="ignore")[:200]
            print(f"    [RR HTTP {e.code}] {body}")
            return {}
        except urllib.error.URLError as e:
            if "Temporary failure" in str(e) or "Name or service" in str(e):
                wait = 10 * (attempt + 1)
                print(f"    [DNS retry {attempt+1}/{_retries} in {wait}s]")
                time.sleep(wait)
                continue
            print(f"    [URLError] {e}")
            return {}
        except Exception as e:
            print(f"    [Error] {e}")
            return {}
    return {}


def extract_domain(website: str) -> str:
    if not website:
        return ""
    try:
        parsed = urlparse(website if "://" in website else "https://" + website)
        host = parsed.netloc or parsed.path
        return re.sub(r"^www\.", "", host).split("/")[0].lower().strip()
    except Exception:
        return ""


PLATFORM_DOMAINS = {
    "fandom.com", "yelp.com", "facebook.com", "google.com",
    "yellowpages.com", "bbb.org", "manta.com", "tripadvisor.com",
    "angieslist.com", "thumbtack.com", "gunbroker.com",
    "hub.biz", "bizapedia.com", "dnb.com", "opencorporates.com",
    "chamberofcommerce.com", "merchantcircle.com", "mapquest.com",
}


def is_valid_dealer_domain(domain: str) -> bool:
    if not domain:
        return False
    for p in PLATFORM_DOMAINS:
        if domain == p or domain.endswith("." + p):
            return False
    # Skip government domains
    if domain.endswith(".gov"):
        return False
    return True


def search_by_company(company_name: str, city: str, state: str, api_key: str) -> list[dict]:
    """Search RocketReach for owner/manager at this company."""
    # Title-filtered search
    result = _rr_request(RR_SEARCH, api_key, payload={
        "query": {
            "current_employer": [company_name],
            "current_title":    ["owner", "president", "ceo", "founder",
                                 "general manager", "director", "manager"],
        },
        "start":    1,
        "pageSize": 10,
    })
    profiles = result.get("profiles", [])
    if profiles:
        return profiles
    # Fallback: just company name, any title
    result2 = _rr_request(RR_SEARCH, api_key, payload={
        "query": {"current_employer": [company_name]},
        "start": 1,
        "pageSize": 10,
    })
    return result2.get("profiles", [])


def pick_best(profiles: list[dict]) -> dict | None:
    if not profiles:
        return None
    for kw in TITLE_PRIORITY:
        for p in profiles:
            title = (p.get("current_title") or p.get("title") or "").lower()
            if kw in title:
                return p
    return profiles[0]


def enrich_profile(profile_id: str, api_key: str) -> dict:
    """Get full email + phone for a profile ID."""
    result = _rr_request(RR_LOOKUP, api_key, params={
        "id":     profile_id,
        "extras": "phones",
    })
    if not result or result.get("status") == "failed":
        return {}

    emails = result.get("emails", []) or []
    phones = result.get("phones", []) or result.get("phone_numbers", []) or []

    # Prefer work email
    email = ""
    for e in emails:
        val = e.get("email", "") if isinstance(e, dict) else str(e)
        if val and "@" in val:
            if not email:
                email = val
            if not any(p in val for p in ["gmail", "yahoo", "hotmail", "outlook", "icloud"]):
                email = val
                break

    phone = ""
    for p in phones:
        val = p.get("number", "") if isinstance(p, dict) else str(p)
        if val:
            phone = val
            break

    return {
        "name":    result.get("name", "") or f"{result.get('first_name','')} {result.get('last_name','')}".strip(),
        "title":   result.get("current_title", "") or result.get("title", "") or "",
        "email":   email,
        "phone":   phone,
        "linkedin": result.get("linkedin_url", "") or "",
    }


def collect_targets(dealers: list[dict], rr_domain_cache: dict,
                    existing_contacts_csv: str,
                    top_n: int = 50) -> list[dict]:
    """
    Target dealers that have a valid website but no verified email/phone yet.
    Sorted by total_listings desc so highest-value dealers are enriched first.
    Capped at top_n to stay within RocketReach lookup budget.
    """
    covered = set()
    if Path(existing_contacts_csv).exists():
        for r in csv.DictReader(open(existing_contacts_csv)):
            if r.get("contact_email"):
                covered.add(r["dealer_name"])

    candidates = []
    for d in dealers:
        dealer   = d["dealer_name"]
        website  = d.get("website", "").strip()
        domain   = extract_domain(website)
        listings = int(d.get("total_listings", "0") or 0)

        if dealer in rr_domain_cache:
            continue
        if dealer in covered:
            continue
        if not is_valid_dealer_domain(domain):
            continue

        candidates.append({
            "dealer":         dealer,
            "domain":         domain,
            "city":           d.get("location_city", ""),
            "state":          d.get("location_state", ""),
            "website":        website,
            "total_listings": listings,
            "company_name":   dealer,
        })

    # Sort highest listing count first, then cap
    candidates.sort(key=lambda x: x["total_listings"], reverse=True)
    return candidates[:top_n]


def main():
    api_key = _load_key()
    if not api_key:
        print("ERROR: ROCKETREACH_API_KEY not set in .env")
        return

    dealers     = list(csv.DictReader(open(ENRICHED)))
    rr_d_cache  = json.loads(Path(RR_DOMAIN_CACHE).read_text()) if Path(RR_DOMAIN_CACHE).exists() else {}

    targets = collect_targets(dealers, rr_d_cache, CONTACTS_OUT, top_n=50)
    print(f"Top 50 dealers by listing count (RocketReach domain search)")
    print(f"Already cached: {len(rr_d_cache)}")
    print(f"Est. lookup usage: up to {len(targets)*2} of your ~105 available")
    print()

    if not targets:
        print("Nothing to do — all dealers with valid websites already processed.")
        return

    found = not_found = 0

    for i, t in enumerate(targets, 1):
        dealer = t["dealer"]
        domain = t["domain"]
        print(f"[{i}/{len(targets)}] {domain:35s} {dealer[:35]}", end="  ", flush=True)

        try:
            profiles = search_by_company(t["company_name"], t["city"], t["state"], api_key)
        except RuntimeError as e:
            print(f"\nRocketReach error: {e}")
            Path(RR_DOMAIN_CACHE).write_text(json.dumps(rr_d_cache, indent=2))
            break

        # Filter to profiles whose current employer domain matches our domain
        domain_root = domain.split(".")[0] if domain else ""
        if domain_root:
            filtered = []
            for p in profiles:
                emp = (p.get("current_employer") or "").lower()
                emp_domain = (p.get("current_employer_domain") or "").lower()
                if domain_root in emp or domain_root in emp_domain:
                    filtered.append(p)
            if filtered:
                profiles = filtered

        best = pick_best(profiles)
        if not best:
            print("— no profiles found")
            rr_d_cache[dealer] = {"found": False, "reason": "no_profiles", "domain": domain}
            not_found += 1
            Path(RR_DOMAIN_CACHE).write_text(json.dumps(rr_d_cache, indent=2))
            time.sleep(1.0)
            continue

        profile_id = best.get("id") or best.get("profile_id") or ""
        preview_name  = best.get("name", "") or f"{best.get('first_name','')} {best.get('last_name','')}".strip()
        preview_title = best.get("current_title", "") or best.get("title", "") or ""
        print(f"found: {preview_name} ({preview_title})", end=" → ", flush=True)

        if not profile_id:
            print("no ID to enrich")
            rr_d_cache[dealer] = {"found": False, "reason": "no_id", "domain": domain}
            not_found += 1
            Path(RR_DOMAIN_CACHE).write_text(json.dumps(rr_d_cache, indent=2))
            time.sleep(1.0)
            continue

        try:
            contact = enrich_profile(profile_id, api_key)
        except RuntimeError as e:
            print(f"enrich error: {e}")
            Path(RR_DOMAIN_CACHE).write_text(json.dumps(rr_d_cache, indent=2))
            break

        em = contact.get("email", "") or ""
        ph = contact.get("phone", "") or ""

        # Verify email domain matches the dealer's website — reject cross-company matches
        em_domain = em.split("@")[-1].lower() if "@" in em else ""
        domain_root = domain.split(".")[0] if domain else ""
        email_matches = domain_root and em_domain and domain_root in em_domain

        if em and not email_matches:
            print(f"✗ wrong-company email ({em}) — discarding email")
            em = ""  # Phone still usable if present

        if em or ph:
            lvl = "high" if em else "medium"
            print(f"✓ {em or '(no email)'}  |  {ph or '(no phone)'}")
            rr_d_cache[dealer] = {
                "found":          True,
                "contact_name":   contact.get("name") or preview_name,
                "contact_title":  contact.get("title") or preview_title,
                "contact_email":  em,
                "contact_phone":  ph,
                "contact_linkedin": contact.get("linkedin", ""),
                "contact_source": "rocketreach_domain",
                "verified_level": lvl,
                "domain":         domain,
            }
            found += 1
        else:
            print("— profile found, no verified contact info")
            rr_d_cache[dealer] = {"found": False, "reason": "no_verified_contacts",
                                  "profile_name": preview_name, "domain": domain}
            not_found += 1

        Path(RR_DOMAIN_CACHE).write_text(json.dumps(rr_d_cache, indent=2))
        time.sleep(1.5)

    print(f"\nRocketReach domain results ({len(targets)} attempted):")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"\nRun merge_contacts.py to regenerate contacts.csv with new results.")
    print(f"Cache saved to: {RR_DOMAIN_CACHE}")


if __name__ == "__main__":
    main()
