#!/usr/bin/env python3
"""
Apollo domain enrichment — uses people/match endpoint with domain lookup.

Different from apollo_contacts.py (which used mixed_people/search by company name).
people/match takes a domain and returns the best-guess owner/manager contact
Apollo has indexed for that domain. More accurate for domain-verified results.

Run: python3 apollo_domain_enrich.py
"""

import csv
import json
import os
import re
import time
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

ENRICHED        = "dealers_enriched.csv"
APOLLO_DOM_CACHE = "cache/apollo_domain_cache.json"

APOLLO_MATCH_URL  = "https://api.apollo.io/api/v1/people/match"
APOLLO_SEARCH_URL = "https://api.apollo.io/api/v1/people/search"

TITLE_PRIORITY = [
    "owner","co-owner","founder","co-founder","proprietor",
    "president","ceo","chief executive",
    "general manager","gm","operations manager","director of operations",
    "store manager","shop manager","manager","director","partner","principal",
]

GENERIC_PREFIXES = [
    "info@","sales@","contact@","support@","admin@","mail@","hello@",
    "store@","shop@","service@","office@","customerservice@","contactus@",
    "orders@","events@","questions@","online@","feedback@","webmaster@",
]


def _load_key():
    return os.getenv("APOLLO_API_KEY", "")


def extract_domain(website):
    if not website:
        return ""
    try:
        parsed = urlparse(website if "://" in website else "https://" + website)
        host = parsed.netloc or parsed.path
        return re.sub(r"^www\.", "", host).split("/")[0].lower().strip()
    except Exception:
        return ""


def _apollo_request(url, api_key, payload, retries=3):
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={
            "x-api-key":    api_key,
            "Content-Type": "application/json",
            "Accept":       "application/json",
        }, method="POST",
    )
    for attempt in range(retries):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:200]
            if e.code == 429:
                raise RuntimeError(f"Apollo rate limit: {body}")
            if e.code in (401, 403):
                raise RuntimeError(f"Apollo auth error {e.code}: {body}")
            print(f"    [Apollo HTTP {e.code}] {body[:80]}")
            return {}
        except Exception as e:
            wait = 3 * (attempt + 1)
            print(f"    [Apollo retry {attempt+1} in {wait}s: {e}]")
            time.sleep(wait)
    return {}


def apollo_match_by_domain(domain, api_key):
    """Use Apollo people/match with domain to find best contact."""
    result = _apollo_request(APOLLO_MATCH_URL, api_key, {
        "domain": domain,
        "reveal_personal_emails": False,
    })
    person = result.get("person") or {}
    if not person:
        return None

    emails = person.get("email_addresses") or []
    email  = ""
    for e in emails:
        val = e.get("email","") if isinstance(e, dict) else str(e)
        if val and "@" in val and not any(val.startswith(p) for p in GENERIC_PREFIXES):
            email = val
            break

    phones = person.get("phone_numbers") or []
    phone  = ""
    for p in phones:
        val = p.get("raw_number","") or p.get("number","") if isinstance(p,dict) else str(p)
        if val:
            phone = val
            break

    name  = f"{person.get('first_name','')} {person.get('last_name','')}".strip()
    title = person.get("title","") or person.get("headline","") or ""

    return {
        "name":  name,
        "title": title,
        "email": email,
        "phone": phone,
        "linkedin": person.get("linkedin_url","") or "",
    }


def apollo_search_by_domain(domain, api_key):
    """Fallback: search by organization domain, pick best title match."""
    result = _apollo_request(APOLLO_SEARCH_URL, api_key, {
        "q_organization_domains_list": [domain],
        "person_titles": TITLE_PRIORITY[:8],
        "page": 1,
        "per_page": 10,
        "prospected_by_current_team": "no",
    })
    people = result.get("people") or []
    if not people:
        return None

    for kw in TITLE_PRIORITY:
        for p in people:
            title = (p.get("title") or "").lower()
            if kw in title:
                emails = p.get("email_addresses") or []
                email  = ""
                for e in emails:
                    val = e.get("email","") if isinstance(e,dict) else str(e)
                    if val and "@" in val:
                        email = val; break
                phones = p.get("phone_numbers") or []
                phone  = ""
                for ph in phones:
                    val = ph.get("raw_number","") if isinstance(ph,dict) else str(ph)
                    if val:
                        phone = val; break
                name = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
                return {"name":name,"title":p.get("title",""),"email":email,"phone":phone,"linkedin":p.get("linkedin_url","")}

    p = people[0]
    name  = f"{p.get('first_name','')} {p.get('last_name','')}".strip()
    email = ""
    for e in (p.get("email_addresses") or []):
        val = e.get("email","") if isinstance(e,dict) else str(e)
        if val and "@" in val:
            email = val; break
    phone = ""
    for ph in (p.get("phone_numbers") or []):
        val = ph.get("raw_number","") if isinstance(ph,dict) else str(ph)
        if val:
            phone = val; break
    return {"name":name,"title":p.get("title",""),"email":email,"phone":phone,"linkedin":p.get("linkedin_url","")}


def collect_targets(dealers, apollo_dom_cache):
    targets = []
    for d in dealers:
        name    = d["dealer_name"]
        website = d.get("website","").strip()
        domain  = extract_domain(website)
        if not domain or name in apollo_dom_cache:
            continue
        has_email = bool(d.get("contact_email","").strip())
        has_phone = bool(d.get("contact_phone","").strip())
        if has_email and has_phone:
            continue
        listings = int(d.get("total_listings","0") or 0)
        targets.append({"dealer": name, "domain": domain, "listings": listings})

    targets.sort(key=lambda x: x["listings"], reverse=True)
    return targets


def main():
    api_key = _load_key()
    if not api_key:
        print("ERROR: APOLLO_API_KEY not set in .env"); return

    dealers          = list(csv.DictReader(open(ENRICHED)))
    apollo_dom_cache = json.loads(Path(APOLLO_DOM_CACHE).read_text()) if Path(APOLLO_DOM_CACHE).exists() else {}

    targets = collect_targets(dealers, apollo_dom_cache)
    print(f"Apollo domain targets : {len(targets)} dealers missing email or phone")
    print(f"Already cached        : {len(apollo_dom_cache)}")
    print()

    if not targets:
        print("Nothing to do."); return

    found = not_found = 0

    for i, t in enumerate(targets, 1):
        dealer  = t["dealer"]
        domain  = t["domain"]
        print(f"[{i:3d}/{len(targets)}] {t['listings']:4d}L  {domain:35s} {dealer[:30]}", end="  ", flush=True)

        try:
            contact = apollo_match_by_domain(domain, api_key)
            if not contact or (not contact.get("email") and not contact.get("phone") and not contact.get("name")):
                contact = apollo_search_by_domain(domain, api_key)
        except RuntimeError as e:
            print(f"\nApollo error: {e}")
            Path(APOLLO_DOM_CACHE).write_text(json.dumps(apollo_dom_cache, indent=2))
            break

        if contact and (contact.get("name") or contact.get("email") or contact.get("phone")):
            em = contact.get("email","") or ""
            ph = contact.get("phone","") or ""

            # Verify email domain matches dealer domain
            if em and "@" in em:
                em_root = em.split("@")[-1].split(".")[0]
                dom_root = domain.split(".")[0]
                if em_root != dom_root and dom_root not in em.split("@")[-1]:
                    em = ""  # wrong company email

            extras = []
            if em: extras.append(em)
            if ph: extras.append(ph)
            label = f"✓ {contact.get('name','?')} ({contact.get('title','')[:25]})"
            print(label + (f"  [{', '.join(extras)}]" if extras else "  [no email/phone]"))

            apollo_dom_cache[dealer] = {
                "found":          bool(em or ph or contact.get("name")),
                "contact_name":   contact.get("name",""),
                "contact_title":  contact.get("title",""),
                "contact_email":  em,
                "contact_phone":  ph,
                "contact_linkedin": contact.get("linkedin",""),
                "contact_source": "apollo_domain",
                "domain":         domain,
            }
            if em or ph:
                found += 1
            else:
                not_found += 1
        else:
            print("— not found")
            apollo_dom_cache[dealer] = {"found": False, "domain": domain}
            not_found += 1

        Path(APOLLO_DOM_CACHE).write_text(json.dumps(apollo_dom_cache, indent=2))
        time.sleep(0.5)

    print(f"\nApollo domain results ({len(targets)} attempted):")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"\nRun merge_contacts.py after this completes.")


if __name__ == "__main__":
    main()
