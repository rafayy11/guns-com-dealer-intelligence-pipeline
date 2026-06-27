#!/usr/bin/env python3
"""
Serper.dev Google Search enrichment — finds emails via Google index.

Google indexes email addresses that appear on dealer websites, directories,
press releases, and local listings. Three queries per dealer:
  1. "@domain.com"           — direct email pattern search
  2. site:domain.com email   — Google's own index of their contact pages
  3. "Dealer Name" owner email contact

Saves to cache/serper_cache.json

Run: python3 serper_enrich.py
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
SERPER_CACHE  = "cache/serper_cache.json"
SERPER_URL    = "https://google.serper.dev/search"

EMAIL_RE    = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_RE    = re.compile(r'(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}')
PHONE_NOISE = re.compile(r'^(?:0{7,}|1{7,}|\d{5,6}$)')

NOISE_EMAIL_DOMAINS = {
    "gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com",
    "aol.com","msn.com","live.com","me.com","mac.com",
    "wiza.co","hunter.io","apollo.io","rocketreach.co","clearbit.com",
    "example.com","yourdomain.com","domain.com","email.com",
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
    for var in ["SERPER_API_KEY", "SERPER_API_KEY_2", "SERPER_API_KEY_3"]:
        k = os.getenv(var, "").strip()
        if k:
            keys.append(k)
    _keys = keys
    print(f"Serper keys loaded: {len(_keys)}")

def _current_key():
    return _keys[_key_index]

def _rotate_key():
    global _key_index
    _key_index += 1
    if _key_index >= len(_keys):
        raise RuntimeError("All Serper API keys exhausted")
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


def serper_search(query, num=10):
    headers = {
        "X-API-KEY":    _current_key(),
        "Content-Type": "application/json",
    }
    for attempt in range(3):
        try:
            r = requests.post(SERPER_URL, json={"q": query, "num": num},
                              headers=headers, timeout=(5, 15))
            if r.status_code == 429:
                time.sleep(5); continue
            if r.status_code in (401, 403):
                print(f"  [Key {_key_index+1} auth error {r.status_code}]")
                _rotate_key()
                return serper_search(query, num)
            if r.status_code != 200:
                print(f"    [Serper HTTP {r.status_code}] {r.text[:80]}")
                return {}
            return r.json()
        except RuntimeError:
            raise
        except Exception as e:
            wait = 3 * (attempt + 1)
            print(f"    [Serper retry {attempt+1}/3 in {wait}s: {e}]")
            time.sleep(wait)
    return {}


def extract_from_serper(data, domain):
    """Extract best email and phone from Serper search results."""
    domain_root = domain.split(".")[0] if domain else ""
    best_email  = ""
    best_phone  = ""
    generic_email = ""

    # Pull all text: organic results snippet + title + sitelinks
    texts = []
    for r in data.get("organic", []):
        texts.append(r.get("snippet", ""))
        texts.append(r.get("title", ""))
        for s in r.get("sitelinks", []):
            texts.append(s.get("snippet", ""))
    for r in data.get("peopleAlsoAsk", []):
        texts.append(r.get("snippet", ""))
    # Knowledge graph
    kg = data.get("knowledgeGraph", {})
    texts.append(kg.get("description", ""))

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
        f'site:{domain} email contact',
        f'{dealer} email contact owner',
        f'{domain} contact email phone',
    ]

    best_email = ""
    best_phone = ""

    for query in queries:
        try:
            data = serper_search(query, num=10)
        except RuntimeError:
            raise
        email, phone = extract_from_serper(data, domain)
        if email and not best_email:
            best_email = email
        if phone and not best_phone:
            best_phone = phone
        if best_email and best_phone:
            break
        time.sleep(0.2)

    return best_email, best_phone


def collect_targets(dealers, serper_cache):
    targets = []
    for d in dealers:
        name    = d["dealer_name"]
        website = d.get("website","").strip()
        domain  = extract_domain(website)
        if not valid_domain(domain):
            continue
        if name in serper_cache:
            continue
        has_email = bool(d.get("contact_email","").strip()) if "contact_email" in d else False
        has_phone = bool(d.get("contact_phone","").strip()) if "contact_phone" in d else False
        if has_email and has_phone:
            continue
        listings = int(d.get("total_listings","0") or 0)
        targets.append({
            "dealer":  name,
            "domain":  domain,
            "listings": listings,
        })

    targets.sort(key=lambda x: x["listings"], reverse=True)
    return targets


def main():
    _load_keys()
    if not _keys:
        print("ERROR: SERPER_API_KEY not set in .env"); return

    # Use contacts.csv for has_email/has_phone lookup
    contacts_map = {}
    if Path("contacts.csv").exists():
        for r in csv.DictReader(open("contacts.csv")):
            contacts_map[r["dealer_name"]] = r

    dealers = list(csv.DictReader(open(ENRICHED)))
    # Inject contact info for filtering
    for d in dealers:
        c = contacts_map.get(d["dealer_name"], {})
        d["contact_email"] = c.get("contact_email","")
        d["contact_phone"] = c.get("contact_phone","")

    serper_cache = json.loads(Path(SERPER_CACHE).read_text()) if Path(SERPER_CACHE).exists() else {}
    targets = collect_targets(dealers, serper_cache)

    print(f"Serper targets   : {len(targets)} dealers missing email or phone")
    print(f"Already cached   : {len(serper_cache)}")
    print(f"Est. cost        : ~${len(targets)*3/1000:.2f} (3 queries each @ $0.001/query)")
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
            print(f"\nSerper error: {e}")
            Path(SERPER_CACHE).write_text(json.dumps(serper_cache, indent=2))
            break

        if email or phone:
            extras = []
            if email: extras.append(email)
            if phone: extras.append(phone)
            print(f"✓ [{', '.join(extras)}]")
            serper_cache[dealer] = {
                "found":          True,
                "contact_email":  email,
                "contact_phone":  phone,
                "contact_source": "serper",
                "domain":         domain,
            }
            found += 1
        else:
            print("— not found")
            serper_cache[dealer] = {"found": False, "domain": domain}
            not_found += 1

        Path(SERPER_CACHE).write_text(json.dumps(serper_cache, indent=2))
        time.sleep(0.3)

    print(f"\nSerper results ({len(targets)} searched):")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"\nRun merge_contacts.py after this completes.")


if __name__ == "__main__":
    main()
