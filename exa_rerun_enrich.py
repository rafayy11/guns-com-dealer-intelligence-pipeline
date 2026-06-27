#!/usr/bin/env python3
"""
Exa re-run enrichment — targets all dealers missing email OR phone.

For dealers already in exa_domain_cache with no email found, runs fresh
email-focused queries. For never-searched dealers, runs standard queries.
Saves to cache/exa_rerun_cache.json (merged into main pipeline via merge_contacts).

Run: python3 exa_rerun_enrich.py
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

ENRICHED       = "dealers_enriched.csv"
RERUN_CACHE    = "cache/exa_rerun_cache.json"
EXA_SEARCH_URL = "https://api.exa.ai/search"

PLATFORM_DOMAINS = {
    "fandom.com","yelp.com","facebook.com","google.com","yellowpages.com",
    "bbb.org","manta.com","tripadvisor.com","hub.biz","bizapedia.com",
    "gunbroker.com","angieslist.com","wheree.com","chamberofcommerce.com",
}

EMAIL_RE  = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_RE  = re.compile(r'(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}')
PHONE_NOISE = re.compile(r'^(?:0{7,}|1{7,}|\d{5,6}$)')

NOISE_EMAIL_DOMAINS = {
    "gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com",
    "aol.com","msn.com","live.com","me.com","mac.com",
    "wiza.co","hunter.io","apollo.io","rocketreach.co","clearbit.com",
}

GENERIC_PREFIXES = [
    "info@","sales@","contact@","support@","admin@","mail@","hello@",
    "store@","shop@","service@","office@","customerservice@","contactus@",
    "orders@","events@","questions@","online@","feedback@","webmaster@",
    "noreply@","no-reply@","donotreply@",
]


def _load_keys():
    keys = []
    for k in ["EXA_API_KEY","EXA_API_KEY_2","EXA_API_KEY_3","EXA_API_KEY_4"]:
        v = os.getenv(k,"")
        if v:
            keys.append(v)
    return keys

_key_index = 0
_keys = []

def _get_key():
    global _keys, _key_index
    if not _keys:
        _keys = _load_keys()
    return _keys[_key_index % len(_keys)]

def _rotate_key():
    global _key_index
    _key_index += 1
    if _key_index >= len(_keys):
        raise RuntimeError("All Exa API keys exhausted")
    print(f"\n  [Key rotated → key {_key_index+1}/{len(_keys)}]")
    return _keys[_key_index]


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


def exa_search(query, num_results=10):
    payload = json.dumps({
        "query":         query,
        "numResults":    num_results,
        "maxCharacters": 5000,
        "type":          "neural",
    }).encode()
    for attempt in range(3):
        key = _get_key()
        req = urllib.request.Request(
            EXA_SEARCH_URL, data=payload,
            headers={
                "x-api-key":    key,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read()).get("results", [])
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if e.code == 402 or "credits" in body.lower():
                print(f"\n  [Key {_key_index+1} out of credits]", end="")
                _rotate_key()
                continue
            if e.code == 429:
                time.sleep(5); continue
            if e.code in (401, 403):
                print(f"\n  [Key {_key_index+1} auth error {e.code}]", end="")
                _rotate_key()
                continue
            print(f"    [Exa HTTP {e.code}] {body[:100]}")
            return []
        except Exception as e:
            wait = 3 * (attempt + 1)
            print(f"    [Exa retry {attempt+1}/3 in {wait}s: {e}]")
            time.sleep(wait)
    return []


def extract_email_phone(results, domain):
    domain_root = domain.split(".")[0] if domain else ""
    best_email = ""
    best_phone = ""

    for r in results:
        text = " ".join(filter(None, [r.get("text",""), r.get("title","")]))

        if not best_email:
            for em in EMAIL_RE.findall(text):
                em = em.lower().strip(".,;:")
                em_domain = em.split("@")[-1] if "@" in em else ""
                if not em_domain or em_domain in NOISE_EMAIL_DOMAINS:
                    continue
                if any(em.startswith(p) for p in GENERIC_PREFIXES):
                    continue
                if domain_root and domain_root not in em_domain:
                    continue
                best_email = em
                break

        if not best_phone:
            for ph in PHONE_RE.findall(text):
                ph = re.sub(r'[^\d+]', '', ph)
                if len(ph) >= 10 and not PHONE_NOISE.match(ph):
                    best_phone = ph
                    break

        if best_email and best_phone:
            break

    return best_email, best_phone


def search_for_dealer(dealer, domain):
    """Run email-focused queries for a dealer. Returns (email, phone)."""
    queries = [
        # Direct email pattern search
        f'"{domain}" email owner contact',
        f'site:{domain} email',
        f'"{dealer}" owner email contact information',
        f'"{domain}" "@{domain}"',
        f'"{dealer}" contact us email phone',
    ]

    best_email = ""
    best_phone = ""

    for query in queries:
        try:
            results = exa_search(query, num_results=10)
        except RuntimeError:
            raise
        email, phone = extract_email_phone(results, domain)
        if email and not best_email:
            best_email = email
        if phone and not best_phone:
            best_phone = phone
        if best_email and best_phone:
            break
        time.sleep(0.15)

    return best_email, best_phone


def collect_targets(dealers, rerun_cache, top_n=500):
    """
    Target dealers missing email OR phone.
    Skip if already in rerun_cache (already re-processed).
    """
    targets = []
    for d in dealers:
        name    = d["dealer_name"]
        website = d.get("website","").strip()
        domain  = extract_domain(website)

        if not valid_domain(domain):
            continue
        if name in rerun_cache:
            continue

        has_email = bool(d.get("contact_email","").strip())
        has_phone = bool(d.get("contact_phone","").strip())

        if has_email and has_phone:
            continue  # already complete

        listings = int(d.get("total_listings","0") or 0)
        targets.append({
            "dealer":   name,
            "domain":   domain,
            "website":  website,
            "listings": listings,
            "has_email": has_email,
            "has_phone": has_phone,
        })

    targets.sort(key=lambda x: x["listings"], reverse=True)
    return targets[:top_n]


def main():
    global _keys
    _keys = _load_keys()
    if not _keys:
        print("ERROR: No EXA_API_KEY found in .env"); return
    print(f"Exa keys loaded: {len(_keys)}")

    dealers     = list(csv.DictReader(open(ENRICHED)))
    rerun_cache = json.loads(Path(RERUN_CACHE).read_text()) if Path(RERUN_CACHE).exists() else {}

    targets = collect_targets(dealers, rerun_cache)
    print(f"Exa re-run targets : {len(targets)} dealers missing email or phone")
    print(f"Already re-run     : {len(rerun_cache)}")
    print()

    if not targets:
        print("Nothing to do.")
        return

    found = not_found = 0

    for i, t in enumerate(targets, 1):
        dealer  = t["dealer"]
        domain  = t["domain"]
        missing = []
        if not t["has_email"]: missing.append("email")
        if not t["has_phone"]: missing.append("phone")
        print(f"[{i:3d}/{len(targets)}] {t['listings']:4d}L  {domain:35s}  missing:{'+'.join(missing)}", end="  ", flush=True)

        try:
            email, phone = search_for_dealer(dealer, domain)
        except RuntimeError as e:
            print(f"\nExa error: {e}")
            Path(RERUN_CACHE).write_text(json.dumps(rerun_cache, indent=2))
            break

        if email or phone:
            extras = []
            if email: extras.append(email)
            if phone: extras.append(phone)
            print(f"✓ [{', '.join(extras)}]")
            rerun_cache[dealer] = {
                "found":         True,
                "contact_email": email,
                "contact_phone": phone,
                "contact_source": "exa_rerun",
                "domain":        domain,
            }
            found += 1
        else:
            print("— nothing found")
            rerun_cache[dealer] = {"found": False, "domain": domain}
            not_found += 1

        Path(RERUN_CACHE).write_text(json.dumps(rerun_cache, indent=2))
        time.sleep(0.2)

    print(f"\nExa re-run results ({len(targets)} searched):")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"\nRun merge_contacts.py after this completes.")


if __name__ == "__main__":
    main()
