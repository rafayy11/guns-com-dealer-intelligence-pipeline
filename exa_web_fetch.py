#!/usr/bin/env python3
"""
Exa web fetch — scrapes dealer contact/about pages via Exa's fetch API.

Exa fetches pages on their servers, bypassing Cloudflare and JS rendering.
Targets dealers still missing email or phone after all other enrichment.

Tries in order: /contact, /contact-us, /about, /about-us, /staff, /team
Extracts email and phone from fetched page content.

Run: python3 exa_web_fetch.py
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
WEB_FETCH_CACHE = "cache/exa_web_fetch_cache.json"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"

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

CONTACT_PATHS = [
    "/contact", "/contact-us", "/contactus", "/contact_us",
    "/about", "/about-us", "/aboutus", "/about_us",
    "/staff", "/team", "/our-team", "/meet-the-team",
    "/info", "/reach-us", "/get-in-touch",
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


def exa_fetch(url):
    """Fetch a single URL via Exa's contents API with key rotation."""
    payload = json.dumps({
        "ids":  [url],
        "text": {"maxCharacters": 8000},
    }).encode()
    for attempt in range(len(_keys) + 1):
        key = _get_key()
        req = urllib.request.Request(
            EXA_CONTENTS_URL, data=payload,
            headers={
                "x-api-key":    key,
                "Content-Type": "application/json",
                "Accept":       "application/json",
            }, method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
                results = data.get("results", [])
                return results[0].get("text", "") if results else ""
        except urllib.error.HTTPError as e:
            body = e.read().decode()[:300]
            if e.code == 402 or "credits" in body.lower():
                print(f"\n  [Key {_key_index+1} out of credits]", end="")
                _rotate_key()
                continue
            if e.code == 429:
                time.sleep(5); continue
            if e.code in (401, 403):
                print(f"\n  [Key auth error {e.code}]", end="")
                _rotate_key()
                continue
            return ""
        except Exception:
            return ""
    return ""


def extract_email_phone(text, domain):
    domain_root = domain.split(".")[0] if domain else ""
    best_email = ""
    best_phone = ""

    for em in EMAIL_RE.findall(text):
        em = em.lower().strip(".,;:")
        em_domain = em.split("@")[-1] if "@" in em else ""
        if not em_domain or em_domain in NOISE_EMAIL_DOMAINS:
            continue
        if domain_root and domain_root not in em_domain:
            continue
        if any(em.startswith(p) for p in GENERIC_PREFIXES):
            if not best_email:
                best_email = em  # keep generic as fallback
            continue
        best_email = em  # personal email — prefer this
        break

    for ph in PHONE_RE.findall(text):
        ph = re.sub(r'[^\d+]', '', ph)
        if len(ph) >= 10 and not PHONE_NOISE.match(ph):
            best_phone = ph
            break

    return best_email, best_phone


def fetch_dealer_contact(website, domain):
    """Try multiple contact page paths, return first email+phone found."""
    base = website.rstrip("/")
    best_email = ""
    best_phone = ""

    for path in CONTACT_PATHS:
        url = base + path
        try:
            text = exa_fetch(url)
        except RuntimeError:
            raise

        if not text:
            time.sleep(0.1)
            continue

        email, phone = extract_email_phone(text, domain)
        if email and not best_email:
            best_email = email
        if phone and not best_phone:
            best_phone = phone

        if best_email and best_phone:
            break

        time.sleep(0.15)

    return best_email, best_phone


def collect_targets(dealers, fetch_cache):
    targets = []
    for d in dealers:
        name    = d["dealer_name"]
        website = d.get("website","").strip()
        if not website:
            continue
        if name in fetch_cache:
            continue
        has_email = bool(d.get("contact_email","").strip())
        has_phone = bool(d.get("contact_phone","").strip())
        if has_email and has_phone:
            continue
        listings = int(d.get("total_listings","0") or 0)
        targets.append({
            "dealer":    name,
            "website":   website,
            "domain":    extract_domain(website),
            "listings":  listings,
            "has_email": has_email,
            "has_phone": has_phone,
        })

    targets.sort(key=lambda x: x["listings"], reverse=True)
    return targets


def main():
    global _keys
    _keys = _load_keys()
    if not _keys:
        print("ERROR: No EXA_API_KEY found in .env"); return
    print(f"Exa keys loaded: {len(_keys)}")

    dealers     = list(csv.DictReader(open(ENRICHED)))
    fetch_cache = json.loads(Path(WEB_FETCH_CACHE).read_text()) if Path(WEB_FETCH_CACHE).exists() else {}

    targets = collect_targets(dealers, fetch_cache)
    print(f"Exa web-fetch targets : {len(targets)} dealers missing email or phone")
    print(f"Already fetched       : {len(fetch_cache)}")
    print(f"(Cloudflare-protected sites handled via Exa server-side fetch)")
    print()

    if not targets:
        print("Nothing to do."); return

    found = not_found = 0

    for i, t in enumerate(targets, 1):
        dealer  = t["dealer"]
        website = t["website"]
        domain  = t["domain"]
        missing = []
        if not t["has_email"]: missing.append("email")
        if not t["has_phone"]: missing.append("phone")
        print(f"[{i:3d}/{len(targets)}] {t['listings']:4d}L  {domain:35s}  missing:{'+'.join(missing)}", end="  ", flush=True)

        try:
            email, phone = fetch_dealer_contact(website, domain)
        except RuntimeError as e:
            print(f"\nExa rate limit hit: {e}")
            Path(WEB_FETCH_CACHE).write_text(json.dumps(fetch_cache, indent=2))
            break

        if email or phone:
            extras = []
            if email: extras.append(email)
            if phone: extras.append(phone)
            print(f"✓ [{', '.join(extras)}]")
            fetch_cache[dealer] = {
                "found":          True,
                "contact_email":  email,
                "contact_phone":  phone,
                "contact_source": "exa_web_fetch",
                "domain":         domain,
            }
            found += 1
        else:
            print("— nothing on contact pages")
            fetch_cache[dealer] = {"found": False, "domain": domain}
            not_found += 1

        Path(WEB_FETCH_CACHE).write_text(json.dumps(fetch_cache, indent=2))
        time.sleep(0.2)

    print(f"\nExa web-fetch results ({len(targets)} attempted):")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"\nRun merge_contacts.py after this completes.")


if __name__ == "__main__":
    main()
