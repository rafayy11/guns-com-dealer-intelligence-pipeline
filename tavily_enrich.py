#!/usr/bin/env python3
"""
Tavily AI search enrichment — finds emails and phones via Tavily's index.

Tavily extracts structured content from web pages with AI, different crawler
index from Exa so catches different pages. Targets dealers still missing
email or phone after other enrichment passes.

Saves to cache/tavily_cache.json

Run: python3 tavily_enrich.py
"""

import csv
import json
import os
import re
import time
import requests
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

ENRICHED      = "dealers_enriched.csv"
TAVILY_CACHE  = "cache/tavily_cache.json"
TAVILY_URL    = "https://api.tavily.com/search"

EMAIL_RE    = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_RE    = re.compile(r'(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}')
PHONE_NOISE = re.compile(r'^(?:0{7,}|1{7,}|\d{5,6}$)')

NOISE_EMAIL_DOMAINS = {
    "gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com",
    "aol.com","msn.com","live.com","me.com","mac.com",
    "wiza.co","hunter.io","apollo.io","rocketreach.co","clearbit.com",
    "example.com","yourdomain.com","domain.com",
}

GENERIC_PREFIXES = [
    "info@","sales@","contact@","support@","admin@","mail@","hello@",
    "store@","shop@","service@","office@","customerservice@","contactus@",
    "orders@","events@","questions@","online@","feedback@","webmaster@",
    "noreply@","no-reply@","donotreply@",
]

PLATFORM_DOMAINS = {
    "fandom.com","yelp.com","facebook.com","google.com","yellowpages.com",
    "bbb.org","manta.com","tripadvisor.com","hub.biz","bizapedia.com",
    "gunbroker.com","angieslist.com","wheree.com","chamberofcommerce.com",
}


_keys = []
_key_index = 0

def _load_keys():
    global _keys
    keys = []
    for var in ["TAVILY_API_KEY", "TAVILY_API_KEY_2", "TAVILY_API_KEY_3"]:
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    _keys = keys
    print(f"Tavily keys loaded: {len(_keys)}")

def _current_key():
    return _keys[_key_index]

def _rotate_key():
    global _key_index
    _key_index += 1
    if _key_index >= len(_keys):
        raise RuntimeError("All Tavily API keys exhausted")
    print(f"  [Key rotated → key {_key_index+1}/{len(_keys)}]")


def extract_domain(website):
    if not website:
        return ""
    try:
        parsed = urlparse(website if "://" in website else "https://" + website)
        host = parsed.netloc or parsed.path
        return re.sub(r"^www\.", "", host).split("/")[0].lower().strip()
    except Exception:
        return ""


def valid_domain(domain):
    if not domain or "." not in domain:
        return False
    for p in PLATFORM_DOMAINS:
        if domain == p or domain.endswith("." + p):
            return False
    return True


def tavily_search(query, max_results=5):
    payload = {
        "query":               query,
        "search_depth":        "basic",
        "max_results":         max_results,
        "include_raw_content": True,
        "include_answer":      True,
    }
    headers = {
        "Content-Type":  "application/json",
        "Authorization": f"Bearer {_current_key()}",
    }
    for attempt in range(3):
        try:
            r = requests.post(TAVILY_URL, json=payload, headers=headers, timeout=(5, 20))
            if r.status_code == 429:
                time.sleep(5); continue
            if r.status_code in (401, 403):
                raise RuntimeError(f"Tavily auth error {r.status_code}: {r.text[:200]}")
            if r.status_code == 402:
                print(f"  [Key {_key_index+1} out of credits]")
                _rotate_key()
                return tavily_search(query, max_results)
            if r.status_code != 200:
                print(f"    [Tavily HTTP {r.status_code}] {r.text[:80]}")
                return {}
            return r.json()
        except RuntimeError:
            raise
        except Exception as e:
            wait = 3 * (attempt + 1)
            print(f"    [Tavily retry {attempt+1}/3 in {wait}s: {e}]")
            time.sleep(wait)
    return {}


def extract_from_tavily(data, domain):
    domain_root  = domain.split(".")[0] if domain else ""
    best_email   = ""
    best_phone   = ""
    generic_email = ""

    # Gather all text: answer + result content + raw content
    texts = []
    texts.append(data.get("answer", "") or "")
    for r in data.get("results", []):
        texts.append(r.get("content", "") or "")
        texts.append(r.get("raw_content", "") or "")
        texts.append(r.get("title", "") or "")

    full_text = " ".join(filter(None, texts))

    for em in EMAIL_RE.findall(full_text):
        em = em.lower().strip(".,;:()")
        em_domain = em.split("@")[-1] if "@" in em else ""
        if not em_domain or em_domain in NOISE_EMAIL_DOMAINS:
            continue
        if domain_root and domain_root not in em_domain:
            continue
        if any(em.startswith(p) for p in GENERIC_PREFIXES):
            if not generic_email:
                generic_email = em
            continue
        best_email = em
        break

    if not best_email and generic_email:
        best_email = generic_email

    for ph in PHONE_RE.findall(full_text):
        ph = re.sub(r'[^\d+]', '', ph)
        if len(ph) >= 10 and not PHONE_NOISE.match(ph):
            best_phone = ph
            break

    return best_email, best_phone


def search_dealer(dealer, domain):
    queries = [
        f'email contact {domain}',
        f'"{dealer}" owner email phone contact information',
        f'{domain} contact us email address',
    ]

    best_email = ""
    best_phone = ""

    for i, query in enumerate(queries):
        try:
            data = tavily_search(query, max_results=5)
        except RuntimeError:
            raise

        email, phone = extract_from_tavily(data, domain)
        if email and not best_email:
            best_email = email
        if phone and not best_phone:
            best_phone = phone
        if best_email and best_phone:
            break
        time.sleep(0.3)

    return best_email, best_phone


def collect_targets(dealers, tavily_cache, contacts_map):
    targets = []
    for d in dealers:
        name    = d["dealer_name"]
        website = d.get("website","").strip()
        domain  = extract_domain(website)
        if not valid_domain(domain):
            continue
        if name in tavily_cache:
            continue
        c = contacts_map.get(name, {})
        has_email = bool(c.get("contact_email","").strip())
        has_phone = bool(c.get("contact_phone","").strip())
        if has_email and has_phone:
            continue
        listings = int(d.get("total_listings","0") or 0)
        targets.append({
            "dealer":   name,
            "domain":   domain,
            "listings": listings,
        })

    targets.sort(key=lambda x: x["listings"], reverse=True)
    return targets


def main():
    _load_keys()
    if not _keys:
        print("ERROR: TAVILY_API_KEY not set in .env"); return

    contacts_map = {}
    if Path("contacts.csv").exists():
        for r in csv.DictReader(open("contacts.csv")):
            contacts_map[r["dealer_name"]] = r

    dealers      = list(csv.DictReader(open(ENRICHED)))
    tavily_cache = json.loads(Path(TAVILY_CACHE).read_text()) if Path(TAVILY_CACHE).exists() else {}
    targets      = collect_targets(dealers, tavily_cache, contacts_map)

    print(f"Tavily targets   : {len(targets)} dealers missing email or phone")
    print(f"Already cached   : {len(tavily_cache)}")
    print(f"Est. cost        : ~${len(targets)*3*0.01:.2f} (3 queries each @ $0.01/query)")
    print()

    if not targets:
        print("Nothing to do."); return

    found = not_found = 0

    for i, t in enumerate(targets, 1):
        dealer  = t["dealer"]
        domain  = t["domain"]
        print(f"[{i:3d}/{len(targets)}] {t['listings']:4d}L  {domain:35s} {dealer[:28]}", end="  ", flush=True)

        try:
            email, phone = search_dealer(dealer, domain)
        except RuntimeError as e:
            print(f"\nTavily error: {e}")
            Path(TAVILY_CACHE).write_text(json.dumps(tavily_cache, indent=2))
            break

        if email or phone:
            extras = []
            if email: extras.append(email)
            if phone: extras.append(phone)
            print(f"✓ [{', '.join(extras)}]")
            tavily_cache[dealer] = {
                "found":          True,
                "contact_email":  email,
                "contact_phone":  phone,
                "contact_source": "tavily",
                "domain":         domain,
            }
            found += 1
        else:
            print("— not found")
            tavily_cache[dealer] = {"found": False, "domain": domain}
            not_found += 1

        Path(TAVILY_CACHE).write_text(json.dumps(tavily_cache, indent=2))
        time.sleep(0.3)

    print(f"\nTavily results ({len(targets)} searched):")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"\nRun merge_contacts.py after this completes.")


if __name__ == "__main__":
    main()
