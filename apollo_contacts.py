#!/usr/bin/env python3
"""
Phase 2 — Contact Enrichment: Apollo.io people search.

For dealers still missing a named owner/manager after Phase 1 (website scraping),
searches Apollo's contact database by company name + city/state + title filter.

Apollo returns individual contacts with their direct/personal email and sometimes
direct phone — exactly the person-level data we need.

Skips dealers that already have a high/medium contact from Phase 1.

Cache:   cache/apollo_contacts_cache.json
Output:  contacts.csv  (appended/merged)

Run: python3 apollo_contacts.py
"""

import csv
import json
import os
import re
import time
import random
import urllib.request
import urllib.error
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

ENRICHED     = "dealers_enriched.csv"
CONTACTS_OUT = "contacts.csv"
CACHE        = "cache/apollo_contacts_cache.json"

APOLLO_PEOPLE_URL = "https://api.apollo.io/v1/mixed_people/search"
APOLLO_ORG_URL    = "https://api.apollo.io/v1/organizations/enrich"

# Titles Apollo should match against
TARGET_TITLES = [
    "owner", "co-owner", "proprietor", "founder", "co-founder",
    "president", "ceo", "chief executive officer",
    "general manager", "operations manager", "operations director",
    "director of operations", "store manager", "shop manager",
]

FIELDS = [
    "dealer_name", "dealer_city", "dealer_state", "dealer_website",
    "contact_name", "contact_title", "contact_email", "contact_phone",
    "contact_source", "verified_level",
]


def _load_apollo_key() -> str:
    return os.getenv("APOLLO_API_KEY", "")


def _apollo_search(name: str, city: str, state: str, domain: str, api_key: str) -> list:
    """Search Apollo: try domain first (most accurate), fall back to company name."""
    results = []

    # Domain-based search — most accurate for small businesses
    if domain:
        payload = json.dumps({
            "q_organization_domains_list": [domain],
            "person_titles":               TARGET_TITLES,
            "page":                        1,
            "per_page":                    5,
        }).encode("utf-8")
        req = urllib.request.Request(
            APOLLO_PEOPLE_URL, data=payload,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "x-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                results = data.get("people", [])
        except urllib.error.HTTPError as e:
            if e.code not in (422, 404):
                print(f"    [apollo HTTP {e.code}]")
        except Exception as e:
            print(f"    [apollo error] {e}")

    # If domain search found nothing, try company name
    if not results:
        payload = json.dumps({
            "q_organization_name":    name,
            "person_titles":          TARGET_TITLES,
            "organization_locations": [f"{city}, {state}".strip(", ")] if city or state else [],
            "page":                   1,
            "per_page":               5,
        }).encode("utf-8")
        req = urllib.request.Request(
            APOLLO_PEOPLE_URL, data=payload,
            headers={"Content-Type": "application/json", "Cache-Control": "no-cache", "x-api-key": api_key},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
                results = data.get("people", [])
        except urllib.error.HTTPError as e:
            if e.code not in (422, 404):
                print(f"    [apollo name HTTP {e.code}]")
        except Exception as e:
            print(f"    [apollo name error] {e}")

    return results


def _score_person(person: dict, dealer_name: str) -> int:
    """Score a person record — higher = more relevant."""
    score = 0
    title = (person.get("title") or "").lower()
    org   = (person.get("organization", {}) or {}).get("name", "").lower()

    # Title quality
    if any(t in title for t in ["owner", "founder", "ceo", "president", "proprietor"]):
        score += 5
    elif any(t in title for t in ["general manager", "operations manager", "operations director"]):
        score += 4
    elif "manager" in title or "director" in title:
        score += 2

    # Company name match
    dealer_tokens = set(re.sub(r"[^a-z0-9 ]", " ", dealer_name.lower()).split())
    org_tokens    = set(re.sub(r"[^a-z0-9 ]", " ", org).split())
    overlap       = dealer_tokens & org_tokens
    score        += len(overlap)

    # Has email
    if person.get("email"):
        score += 3
    # Has phone
    if person.get("direct_dial_phone") or person.get("mobile_phone"):
        score += 2

    return score


def _extract_contact(person: dict) -> dict:
    email = (person.get("email") or "").lower().strip()
    phone = (
        person.get("direct_dial_phone") or
        person.get("mobile_phone") or
        person.get("work_direct_phone") or ""
    ).strip()
    name  = (
        (person.get("name") or "").strip() or
        f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
    )
    title = (person.get("title") or "").strip()
    return {
        "contact_name":   name,
        "contact_title":  title,
        "contact_email":  email,
        "contact_phone":  phone,
        "contact_source": "apollo",
        "verified_level": "high" if (email and name) else ("medium" if name else "low"),
    }


def _load_contacts() -> dict:
    """Load existing contacts.csv into a dict keyed by dealer_name."""
    if not Path(CONTACTS_OUT).exists():
        return {}
    contacts = {}
    for row in csv.DictReader(open(CONTACTS_OUT)):
        contacts[row["dealer_name"]] = row
    return contacts


def _write_contacts(contacts: dict, row_map: dict):
    with open(CONTACTS_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for name, c in contacts.items():
            dealer = row_map.get(name, {})
            row = {
                "dealer_name":    name,
                "dealer_city":    c.get("dealer_city") or dealer.get("location_city", ""),
                "dealer_state":   c.get("dealer_state") or dealer.get("location_state", ""),
                "dealer_website": c.get("dealer_website") or dealer.get("website", ""),
                "contact_name":   c.get("contact_name", ""),
                "contact_title":  c.get("contact_title", ""),
                "contact_email":  c.get("contact_email", ""),
                "contact_phone":  c.get("contact_phone", ""),
                "contact_source": c.get("contact_source", ""),
                "verified_level": c.get("verified_level", ""),
            }
            w.writerow(row)


def main():
    api_key = _load_apollo_key()
    if not api_key:
        print("ERROR: APOLLO_API_KEY not found in .env")
        return

    dealers  = list(csv.DictReader(open(ENRICHED)))
    row_map  = {r["dealer_name"]: r for r in dealers}
    contacts = _load_contacts()

    # Run on ALL dealers — Apollo independently verifies or completes Phase 1 finds.
    # Even if Phase 1 found a name, Apollo may add email/phone or corroborate.
    targets = dealers
    print(f"Dealers needing Apollo enrichment: {len(targets)}")

    cache = json.loads(Path(CACHE).read_text()) if Path(CACHE).exists() else {}
    todo  = [r for r in targets if r["dealer_name"] not in cache]
    # Sort highest-listing dealers first — more likely to be in Apollo's database
    todo.sort(key=lambda r: int(r.get("total_listings","0") or 0), reverse=True)
    print(f"Cache: {len(cache)} done | Remaining: {len(todo)} (sorted by listing count desc)")

    found = not_found = 0

    from urllib.parse import urlparse
    import re as _re

    def _get_domain(website):
        if not website: return ""
        try:
            p = urlparse(website if "://" in website else "https://" + website)
            h = p.netloc or p.path
            return _re.sub(r"^www\.", "", h).split("/")[0].lower().strip()
        except: return ""

    for i, row in enumerate(todo, 1):
        name    = row["dealer_name"]
        city    = row.get("location_city", "")
        state   = row.get("location_state", "")
        domain  = _get_domain(row.get("website", ""))

        print(f"[{i}/{len(todo)}] {name[:45]}", end="  ", flush=True)

        people = _apollo_search(name, city, state, domain, api_key)

        if not people:
            print("not in Apollo")
            cache[name] = {"found": False}
            not_found += 1
        else:
            # Score and pick best person
            scored = [(p, _score_person(p, name)) for p in people]
            scored.sort(key=lambda x: -x[1])
            best   = scored[0][0]
            contact = _extract_contact(best)

            em  = contact["contact_email"] or "(no email)"
            ph  = contact["contact_phone"] or "(no phone)"
            lvl = contact["verified_level"]
            print(f"✓ [{lvl}] {contact['contact_name']} | {em} | {ph}")

            cache[name] = {"found": True, **contact}
            contacts[name] = {
                **contacts.get(name, {}),
                "dealer_city":  city,
                "dealer_state": state,
                "dealer_website": row.get("website", ""),
                **contact,
            }
            found += 1

        Path(CACHE).write_text(json.dumps(cache, indent=2))
        time.sleep(random.uniform(0.5, 1.2))

    # Apply cache to contacts dict
    for name, data in cache.items():
        if data.get("found") and name not in contacts:
            dealer = row_map.get(name, {})
            contacts[name] = {
                "dealer_city":  dealer.get("location_city", ""),
                "dealer_state": dealer.get("location_state", ""),
                "dealer_website": dealer.get("website", ""),
                "contact_name":   data.get("contact_name", ""),
                "contact_title":  data.get("contact_title", ""),
                "contact_email":  data.get("contact_email", ""),
                "contact_phone":  data.get("contact_phone", ""),
                "contact_source": data.get("contact_source", ""),
                "verified_level": data.get("verified_level", ""),
            }

    _write_contacts(contacts, row_map)

    print(f"\nApollo results ({len(todo)} processed):")
    print(f"  Found  : {found}")
    print(f"  Not found : {not_found}")

    with_name  = sum(1 for c in contacts.values() if c.get("contact_name"))
    with_email = sum(1 for c in contacts.values() if c.get("contact_email"))
    print(f"\nRunning totals across all phases so far:")
    print(f"  Named contacts : {with_name}/{len(dealers)}")
    print(f"  With email     : {with_email}/{len(dealers)}")
    print(f"\nNext: python3 exa_contacts.py")


if __name__ == "__main__":
    main()
