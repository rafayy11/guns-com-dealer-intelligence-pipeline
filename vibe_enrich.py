#!/usr/bin/env python3
"""
Vibe Prospecting (Explorium) enrichment — Phase 4d.

Takes all named contacts from Exa + web caches, matches them via
Explorium's Match Prospects API (name + company), then enriches
with email + phone via the Contact Details API.

Credit cost: 5 credits per contact (email + phone).
Rotates through 5 API keys (100 credits each = 500 total = 100 contacts).

Base URL : https://api.explorium.ai
Match    : POST /v1/prospects/match
Enrich   : POST /v1/prospects/contacts_information/enrich

Run: python3 vibe_enrich.py
"""

import csv
import json
import os
import time
import urllib.request
import urllib.error
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

EXA_CACHE   = "cache/exa_contacts_cache.json"
WEB_CACHE   = "cache/contacts_web_cache.json"
ENRICHED    = "dealers_enriched.csv"
VIBE_CACHE  = "cache/vibe_enrich_cache.json"
CONTACTS_OUT = "contacts.csv"

BASE_URL    = "https://api.explorium.ai"
MATCH_EP    = f"{BASE_URL}/v1/prospects/match"
ENRICH_EP   = f"{BASE_URL}/v1/prospects/contacts_information/enrich"

# False-positive names from Exa that we already know are not real people
SKIP_NAMES = {
    "puerto rico", "gold expands", "bobby bones",
}


def _load_keys() -> list[str]:
    keys = []
    for i in range(1, 10):
        k = os.getenv(f"VIBE_API_KEY_{i}", "").strip()
        if k:
            keys.append(k)
    return keys


class KeyRotator:
    def __init__(self, keys: list[str]):
        self.keys = keys
        self.idx  = 0
        self.exhausted = set()

    def current(self) -> str | None:
        available = [k for k in self.keys if k not in self.exhausted]
        if not available:
            return None
        return available[self.idx % len(available)]

    def rotate(self):
        self.idx += 1

    def exhaust_current(self):
        key = self.current()
        if key:
            self.exhausted.add(key)
            print(f"  Key exhausted — switching ({len(self.exhausted)}/{len(self.keys)} used up)")


def _post(url: str, api_key: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={
            "api_key":      api_key,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:400]
        if e.code == 401:
            raise RuntimeError(f"Vibe auth error (bad key?): {body}")
        if e.code == 402:
            raise RuntimeError(f"Vibe credits exhausted: {body}")
        if e.code == 429:
            print("    [Vibe 429 rate-limit] sleeping 5s...")
            time.sleep(5)
            return {}
        print(f"    [Vibe HTTP {e.code}] {body[:200]}")
        return {}
    except Exception as e:
        print(f"    [Vibe error] {e}")
        return {}


def match_prospects(contacts: list[dict], api_key: str) -> list[dict | None]:
    """
    Batch match up to 50 prospects at once.
    Returns list of prospect_id (str) or None for each input.
    """
    to_match = [
        {"full_name": c["name"], "company_name": c["company"]}
        for c in contacts
    ]
    result = _post(MATCH_EP, api_key, {"prospects_to_match": to_match})

    matched = result.get("matched_prospects", [])
    out = []
    for item in matched:
        pid = item.get("prospect_id")
        out.append(pid if pid else None)
    return out


def enrich_contact(prospect_id: str, api_key: str) -> dict:
    """Enrich one prospect (email + phone). Costs 5 credits."""
    result = _post(ENRICH_EP, api_key, {
        "prospect_id": prospect_id,
        "parameters":  {"contact_types": ["email", "phone"]},
    })

    status = result.get("response_context", {}).get("request_status", "")
    if status == "miss":
        return {}

    data = result.get("data", {})
    if isinstance(data, list):
        data = data[0] if data else {}

    emails = data.get("emails", []) or []
    phones = data.get("phone_numbers", []) or []

    # Best email: prefer professional, then valid, then any
    email = data.get("professions_email", "") or ""
    if not email:
        for e in emails:
            val = e.get("email", "") if isinstance(e, dict) else str(e)
            if val and "@" in val:
                email = val
                if not any(p in val for p in ["gmail", "yahoo", "hotmail", "outlook"]):
                    break

    # Best phone: prefer mobile
    phone = data.get("mobile_phone", "") or ""
    if not phone:
        for p in phones:
            val = p.get("phone_number", "") if isinstance(p, dict) else str(p)
            if val:
                phone = val
                break

    return {"email": email, "phone": phone}


def collect_targets(dealers_map: dict, vibe_cache: dict) -> list[dict]:
    """Gather all named contacts not yet in Vibe cache."""
    seen    = set()
    targets = []

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
            if name.lower() in SKIP_NAMES:
                continue
            if dealer in vibe_cache:
                continue
            key = (dealer, name)
            if key in seen:
                continue
            seen.add(key)
            row = dealers_map.get(dealer, {})
            targets.append({
                "dealer":  dealer,
                "name":    name,
                "company": dealer,
                "title":   data.get("contact_title", ""),
                "city":    row.get("location_city", ""),
                "state":   row.get("location_state", ""),
            })

    return targets


def _write_contacts(dealers_map: dict, vibe_cache: dict,
                    exa_cache: dict, web_cache: dict):
    """Merge all sources into contacts.csv."""
    rr_cache  = {}
    rr_path   = Path("cache/rocketreach_cache.json")
    atf_cache = {}
    atf_path  = Path("cache/atf_ffl_cache.json")
    if rr_path.exists():
        rr_cache = json.loads(rr_path.read_text())
    if atf_path.exists():
        atf_cache = json.loads(atf_path.read_text())

    FIELDS = [
        "dealer_name", "dealer_city", "dealer_state", "dealer_website",
        "contact_name", "contact_title", "contact_email", "contact_phone",
        "contact_linkedin", "contact_source", "verified_level", "sources_agreed",
    ]

    rows = []
    for dealer, row in dealers_map.items():
        vibe = vibe_cache.get(dealer, {})
        rr   = rr_cache.get(dealer, {})
        exa  = exa_cache.get(dealer, {}) if exa_cache.get(dealer, {}).get("found") else {}
        web  = web_cache.get(dealer, {}) if web_cache.get(dealer, {}).get("found") else {}
        atf  = atf_cache.get(dealer, {}) if atf_cache.get(dealer, {}).get("found") else {}

        name  = (vibe.get("contact_name") or rr.get("contact_name") or
                 exa.get("contact_name") or web.get("contact_name") or
                 atf.get("licensee_name") or "")
        title = (vibe.get("contact_title") or rr.get("contact_title") or
                 exa.get("contact_title") or web.get("contact_title") or "")
        email = (vibe.get("contact_email") or rr.get("contact_email") or
                 web.get("contact_email") or "")
        phone = (vibe.get("contact_phone") or rr.get("contact_phone") or
                 web.get("contact_phone") or exa.get("contact_phone") or
                 atf.get("phone") or "")
        linkedin = vibe.get("contact_linkedin") or rr.get("contact_linkedin") or ""

        sources = []
        if vibe.get("contact_email"):       sources.append("vibe")
        if rr.get("contact_email"):         sources.append("rocketreach")
        if exa.get("contact_name"):         sources.append("exa")
        if web.get("contact_name"):         sources.append("web")
        if atf.get("licensee_name") or atf.get("phone"): sources.append("atf_ffl")
        source = "+".join(sources)

        if name and email:    lvl = "high"
        elif name:            lvl = "medium"
        elif email or phone:  lvl = "low"
        else:
            continue

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
        w.writerows(rows)

    return rows


def main():
    keys = _load_keys()
    if not keys:
        print("ERROR: No VIBE_API_KEY_1..5 found in .env")
        return

    print(f"Loaded {len(keys)} Vibe API keys")

    dealers_map = {r["dealer_name"]: r for r in csv.DictReader(open(ENRICHED))}
    vibe_cache  = json.loads(Path(VIBE_CACHE).read_text()) if Path(VIBE_CACHE).exists() else {}
    exa_cache   = json.loads(Path(EXA_CACHE).read_text()) if Path(EXA_CACHE).exists() else {}
    web_cache   = json.loads(Path(WEB_CACHE).read_text()) if Path(WEB_CACHE).exists() else {}

    targets = collect_targets(dealers_map, vibe_cache)
    print(f"Contacts to enrich via Vibe: {len(targets)}")
    print(f"Already cached: {len(vibe_cache)}")
    print(f"Credits available: ~{len(keys) * 100} total, {len(keys) * 20} contacts at 5cr each")
    print()

    if not targets:
        print("Nothing to do — all named contacts already processed.")
    else:
        rotator = KeyRotator(keys)
        found = not_found = matched = 0

        # Process in batches of 50 (API limit)
        BATCH = 50
        for batch_start in range(0, len(targets), BATCH):
            batch = targets[batch_start:batch_start + BATCH]
            api_key = rotator.current()
            if not api_key:
                print("\nAll Vibe API keys exhausted.")
                break

            print(f"\nMatching batch {batch_start//BATCH + 1}: {len(batch)} contacts...")
            try:
                prospect_ids = match_prospects(batch, api_key)
            except RuntimeError as e:
                print(f"Match error: {e}")
                rotator.exhaust_current()
                rotator.rotate()
                api_key = rotator.current()
                if not api_key:
                    break
                # Retry this batch with new key
                try:
                    prospect_ids = match_prospects(batch, api_key)
                except RuntimeError as e2:
                    print(f"Retry failed: {e2}")
                    break

            for i, (target, pid) in enumerate(zip(batch, prospect_ids), 1):
                dealer = target["dealer"]
                name   = target["name"]
                idx    = batch_start + i
                print(f"  [{idx}/{len(targets)}] {name:25s} @ {dealer[:35]:35s}", end="  ", flush=True)

                if not pid:
                    print("— no match")
                    vibe_cache[dealer] = {"found": False, "reason": "no_match"}
                    not_found += 1
                else:
                    matched += 1
                    api_key = rotator.current()
                    if not api_key:
                        print("\nCredits exhausted mid-run.")
                        break

                    try:
                        result = enrich_contact(pid, api_key)
                    except RuntimeError as e:
                        if "exhausted" in str(e).lower():
                            rotator.exhaust_current()
                            rotator.rotate()
                            api_key = rotator.current()
                            if api_key:
                                result = enrich_contact(pid, api_key)
                            else:
                                print("\nAll credits exhausted.")
                                Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                                break
                        else:
                            print(f"Error: {e}")
                            result = {}

                    em = result.get("email", "") or ""
                    ph = result.get("phone", "") or ""

                    if em or ph:
                        lvl = "high" if em else "medium"
                        print(f"✓  {em or '(no email)':35s}  {ph or '(no phone)'}")
                        vibe_cache[dealer] = {
                            "found":           True,
                            "contact_name":    name,
                            "contact_title":   target.get("title", ""),
                            "contact_email":   em,
                            "contact_phone":   ph,
                            "contact_source":  "vibe",
                            "verified_level":  lvl,
                            "prospect_id":     pid,
                        }
                        found += 1
                    else:
                        print("— matched but no contacts")
                        vibe_cache[dealer] = {"found": False, "reason": "no_contacts", "prospect_id": pid}
                        not_found += 1

                    Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                    rotator.rotate()
                    time.sleep(0.5)

        print(f"\nVibe results ({len(targets)} attempted):")
        print(f"  Matched in DB  : {matched}")
        print(f"  Got email/phone: {found}")
        print(f"  Not found      : {not_found}")

    # Write merged contacts.csv
    rows = _write_contacts(dealers_map, vibe_cache, exa_cache, web_cache)
    named   = sum(1 for r in rows if r["contact_name"])
    emailed = sum(1 for r in rows if r["contact_email"])
    phoned  = sum(1 for r in rows if r["contact_phone"])
    high    = sum(1 for r in rows if r["verified_level"] == "high")
    medium  = sum(1 for r in rows if r["verified_level"] == "medium")

    print(f"\n{'='*55}")
    print(f"contacts.csv  —  {len(rows)} contacts total")
    print(f"  Named contact   : {named}")
    print(f"  Have email      : {emailed}")
    print(f"  Have phone      : {phoned}")
    print(f"  High confidence : {high}  (name + email)")
    print(f"  Medium          : {medium}  (name only)")
    print(f"\nVibe cache → {VIBE_CACHE}")


if __name__ == "__main__":
    main()
