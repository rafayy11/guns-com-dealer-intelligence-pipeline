#!/usr/bin/env python3
"""
Vibe Prospecting domain-based enrichment — Phase 4e.

For all 436 dealers with known websites:
  1. Extract domain from URL
  2. Match business via domain → business_id  (high accuracy, domain is unique)
  3. Fetch prospects at that business filtered to owner/C-suite/manager level
  4. Enrich best prospect for verified email + direct phone  (5 credits each)

Also re-runs enrichment for named contacts from Exa/web that weren't matched before.

Rotates through all VIBE_API_KEY_1..N from .env.
Saves progress to cache/vibe_domain_cache.json (fully resumable).

Run: python3 vibe_domain_enrich.py
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

ENRICHED     = "dealers_enriched.csv"
EXA_CACHE    = "cache/exa_contacts_cache.json"
WEB_CACHE    = "cache/contacts_web_cache.json"
VIBE_CACHE   = "cache/vibe_domain_cache.json"   # separate from old vibe_enrich_cache
OLD_VIBE     = "cache/vibe_enrich_cache.json"
CONTACTS_OUT = "contacts.csv"

BASE         = "https://api.explorium.ai"
BIZ_MATCH_EP = f"{BASE}/v1/businesses/match"
PROSP_EP     = f"{BASE}/v1/prospects"
PROSP_MATCH  = f"{BASE}/v1/prospects/match"
ENRICH_EP    = f"{BASE}/v1/prospects/contacts_information/enrich"

# Job levels that represent decision-makers who feel operational pain
TARGET_LEVELS = ["owner", "c_suite", "vp", "director", "manager"]
TARGET_TITLES = [
    "owner", "co-owner", "proprietor", "founder",
    "president", "ceo", "chief executive",
    "general manager", "operations manager", "operations director",
    "director of operations", "store manager", "shop manager", "manager",
]

# Skip confirmed false positives
SKIP_NAMES = {"puerto rico", "gold expands", "bobby bones", "many ways"}


# ── Key management ────────────────────────────────────────────────────────────

def _load_keys() -> list[str]:
    keys = []
    for i in range(1, 12):
        k = os.getenv(f"VIBE_API_KEY_{i}", "").strip()
        if k and k not in keys:
            keys.append(k)
    return keys


class KeyRotator:
    def __init__(self, keys: list[str]):
        self.keys      = keys
        self._idx      = 0
        self.exhausted = set()

    @property
    def active(self) -> list[str]:
        return [k for k in self.keys if k not in self.exhausted]

    def current(self) -> str | None:
        av = self.active
        return av[self._idx % len(av)] if av else None

    def rotate(self):
        self._idx += 1

    def exhaust_current(self):
        k = self.current()
        if k:
            self.exhausted.add(k)
            remaining = len(self.active)
            print(f"\n  [KEY] Exhausted key #{self._idx % len(self.keys) + 1} — {remaining} key(s) remaining")
            self._idx += 1


# ── HTTP helper ───────────────────────────────────────────────────────────────

def _post(url: str, api_key: str, payload: dict, rotator: KeyRotator = None,
          _retries: int = 3) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={"api_key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    for attempt in range(_retries):
        try:
            with urllib.request.urlopen(req, timeout=25) as r:
                return json.loads(r.read())
        except urllib.error.HTTPError as e:
            body = e.read().decode("utf-8", errors="ignore")[:300]
            if e.code == 402:
                if rotator:
                    rotator.exhaust_current()
                raise RuntimeError("credits_exhausted")
            if e.code == 401:
                if rotator:
                    rotator.exhaust_current()
                return {}
            if e.code == 429:
                time.sleep(10)
                continue
            print(f"    [HTTP {e.code}] {body[:150]}")
            return {}
        except urllib.error.URLError as e:
            if "Name or service not known" in str(e) or "Temporary failure" in str(e):
                wait = 10 * (attempt + 1)
                print(f"    [DNS error, retry {attempt+1}/{_retries} in {wait}s]")
                time.sleep(wait)
                continue
            print(f"    [URLError] {e.reason}")
            return {}
        except Exception as e:
            print(f"    [Error] {e}")
            return {}
    return {}


# ── Domain extraction ─────────────────────────────────────────────────────────

def extract_domain(website: str) -> str:
    if not website:
        return ""
    try:
        parsed = urlparse(website if "://" in website else "https://" + website)
        host = parsed.netloc or parsed.path
        host = re.sub(r"^www\.", "", host).split("/")[0].split("?")[0].lower().strip()
        return host
    except Exception:
        return ""


# ── Vibe API calls ────────────────────────────────────────────────────────────

def match_businesses(dealers: list[dict], api_key: str, rotator: KeyRotator) -> list[str | None]:
    """Batch match up to 50 dealers by name + domain. Returns list of business_ids."""
    payload = {
        "businesses_to_match": [
            {"name": d["dealer_name"], "domain": d["domain"]}
            for d in dealers
        ]
    }
    result = _post(BIZ_MATCH_EP, api_key, payload, rotator)
    matched = result.get("matched_businesses", [])
    ids = []
    for item in matched:
        bid = item.get("business_id")
        ids.append(bid if bid else None)
    return ids


def fetch_prospects_at_business(business_id: str, api_key: str, rotator: KeyRotator) -> list[dict]:
    """Fetch prospects at a business, pick owner/manager level by title."""
    payload = {
        "mode":      "preview",
        "filters":   {"business_id": {"values": [business_id]}},
        "page_size": 10,
    }
    result = _post(PROSP_EP, api_key, payload, rotator)
    return result.get("data", result.get("prospects", result.get("results", [])))


def match_prospect_by_name(name: str, company: str, api_key: str, rotator: KeyRotator) -> str | None:
    """Match a named prospect by name + company → prospect_id."""
    payload = {
        "prospects_to_match": [{"full_name": name, "company_name": company}]
    }
    result = _post(PROSP_MATCH, api_key, payload, rotator)
    matched = result.get("matched_prospects", [])
    if matched:
        return matched[0].get("prospect_id")
    return None


def enrich_prospect(prospect_id: str, api_key: str, rotator: KeyRotator) -> dict:
    """Get email + phone for a prospect. Costs 5 credits."""
    payload = {
        "prospect_id": prospect_id,
        "parameters":  {"contact_types": ["email", "phone"]},
    }
    result = _post(ENRICH_EP, api_key, payload, rotator)
    status = result.get("response_context", {}).get("request_status", "")
    if status == "miss":
        return {}

    data = result.get("data", {})
    if isinstance(data, list):
        data = data[0] if data else {}

    emails = data.get("emails", []) or []
    phones = data.get("phone_numbers", []) or []

    # Best email: work over personal
    email = data.get("professions_email", "") or ""
    if not email:
        for e in emails:
            val = e.get("email", "") if isinstance(e, dict) else str(e)
            if val and "@" in val:
                email = val
                if not any(p in val for p in ["gmail", "yahoo", "hotmail", "outlook", "icloud"]):
                    break

    # Best phone: mobile preferred
    phone = data.get("mobile_phone", "") or ""
    if not phone:
        for p in phones:
            val = p.get("phone_number", "") if isinstance(p, dict) else str(p)
            if val:
                phone = val
                break

    return {"email": email, "phone": phone}


def _pick_best_prospect(prospects: list[dict]) -> dict | None:
    """Pick the best match: prioritize owner > founder > CEO > GM > manager."""
    priority = ["owner", "co-owner", "founder", "proprietor", "president",
                "ceo", "chief executive", "general manager", "gm",
                "operations manager", "operations director", "director", "manager"]
    if not prospects:
        return None
    for kw in priority:
        for p in prospects:
            title = (p.get("title") or p.get("job_title") or "").lower()
            level = (p.get("job_level") or "").lower()
            if kw in title or kw in level:
                return p
    return prospects[0]


# ── Target collection ─────────────────────────────────────────────────────────

def collect_targets(dealers: list[dict], vibe_cache: dict,
                    exa_cache: dict, web_cache: dict) -> tuple[list, list]:
    """
    Returns:
      domain_targets  — dealers with a domain, not yet enriched (includes retry of
                        ones that matched biz but failed on prospect fetch)
      name_targets    — dealers where we already know the person's name
    """
    already_done = {d for d, v in vibe_cache.items()
                    if v.get("found") and (v.get("contact_email") or v.get("contact_phone"))}

    old_vibe = {}
    if Path(OLD_VIBE).exists():
        old_vibe = json.loads(Path(OLD_VIBE).read_text())
    already_done |= {d for d, v in old_vibe.items()
                     if v.get("found") and (v.get("contact_email") or v.get("contact_phone"))}

    domain_targets = []
    name_targets   = []
    seen           = set()

    for d in dealers:
        dealer = d["dealer_name"]
        if dealer in already_done:
            continue

        website = d.get("website", "").strip()
        domain  = extract_domain(website)

        exa_data = exa_cache.get(dealer, {})
        web_data = web_cache.get(dealer, {})
        known_name = (exa_data.get("contact_name") if exa_data.get("found") else "") or \
                     (web_data.get("contact_name") if web_data.get("found") else "")

        if known_name and known_name.lower() not in SKIP_NAMES:
            key = (dealer, known_name)
            if key not in seen:
                seen.add(key)
                name_targets.append({
                    "dealer":  dealer,
                    "name":    known_name,
                    "company": dealer,
                    "domain":  domain,
                    "title":   exa_data.get("contact_title") or web_data.get("contact_title") or "",
                })
        elif domain:
            if dealer not in seen:
                seen.add(dealer)
                cached = vibe_cache.get(dealer, {})
                # Re-use business_id if we already matched the business
                biz_id = cached.get("business_id")
                domain_targets.append({
                    "dealer":      dealer,
                    "dealer_name": dealer,
                    "domain":      domain,
                    "city":        d.get("location_city", ""),
                    "state":       d.get("location_state", ""),
                    "business_id": biz_id,  # skip re-match if already known
                })

    return domain_targets, name_targets


# ── Final contacts.csv merge ──────────────────────────────────────────────────

def write_contacts(dealers_map: dict, caches: dict):
    FIELDS = [
        "dealer_name", "dealer_city", "dealer_state", "dealer_website",
        "contact_name", "contact_title", "contact_email", "contact_phone",
        "contact_linkedin", "contact_source", "verified_level", "sources_agreed",
    ]

    rows = []
    for dealer, row in dealers_map.items():
        vd  = caches["vibe_domain"].get(dealer, {})
        vo  = caches["vibe_old"].get(dealer, {})
        rr  = caches["rocketreach"].get(dealer, {})
        exa = caches["exa"].get(dealer, {}) if caches["exa"].get(dealer, {}).get("found") else {}
        web = caches["web"].get(dealer, {}) if caches["web"].get(dealer, {}).get("found") else {}

        # Merge in priority order: domain-enriched > old vibe > rocketreach > exa > web
        vibe = vd if vd.get("found") and (vd.get("contact_email") or vd.get("contact_name")) else \
               vo if vo.get("found") and (vo.get("contact_email") or vo.get("contact_name")) else {}

        name  = (vibe.get("contact_name") or rr.get("contact_name") or
                 exa.get("contact_name") or web.get("contact_name") or "")
        title = (vibe.get("contact_title") or rr.get("contact_title") or
                 exa.get("contact_title") or web.get("contact_title") or "")
        email = (vibe.get("contact_email") or rr.get("contact_email") or
                 web.get("contact_email") or "")
        phone = (vibe.get("contact_phone") or rr.get("contact_phone") or
                 web.get("contact_phone") or exa.get("contact_phone") or "")
        linkedin = vibe.get("contact_linkedin") or rr.get("contact_linkedin") or ""

        sources = []
        if vibe.get("contact_email"):  sources.append("vibe")
        if rr.get("contact_email"):    sources.append("rocketreach")
        if exa.get("contact_name"):    sources.append("exa")
        if web.get("contact_name") or web.get("contact_email") or web.get("contact_phone"):
            sources.append("web")
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


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    keys = _load_keys()
    if not keys:
        print("ERROR: No VIBE_API_KEY_1..N found in .env")
        return

    print(f"Loaded {len(keys)} Vibe API keys  (~{len(keys)*100} credits, ~{len(keys)*20} enrichments)")

    dealers     = list(csv.DictReader(open(ENRICHED)))
    dealers_map = {d["dealer_name"]: d for d in dealers}
    vibe_cache  = json.loads(Path(VIBE_CACHE).read_text()) if Path(VIBE_CACHE).exists() else {}
    old_vibe    = json.loads(Path(OLD_VIBE).read_text()) if Path(OLD_VIBE).exists() else {}
    exa_cache   = json.loads(Path(EXA_CACHE).read_text()) if Path(EXA_CACHE).exists() else {}
    web_cache   = json.loads(Path(WEB_CACHE).read_text()) if Path(WEB_CACHE).exists() else {}
    rr_cache    = json.loads(Path("cache/rocketreach_cache.json").read_text()) \
                  if Path("cache/rocketreach_cache.json").exists() else {}

    domain_targets, name_targets = collect_targets(dealers, vibe_cache, exa_cache, web_cache)
    print(f"Named contacts to enrich  : {len(name_targets)}")
    print(f"Domain-only targets       : {len(domain_targets)}")
    print(f"Already enriched          : {sum(1 for v in vibe_cache.values() if v.get('found') and v.get('contact_email'))}")
    print()

    rotator = KeyRotator(keys)
    enriched_total = 0

    # ── Phase A: Named contacts (most accurate — match by name+company) ───────
    if name_targets:
        print(f"{'='*60}")
        print(f"Phase A — Enriching {len(name_targets)} named contacts by name+company")
        print(f"{'='*60}")

        BATCH = 50
        for bs in range(0, len(name_targets), BATCH):
            batch = name_targets[bs:bs + BATCH]
            key   = rotator.current()
            if not key:
                print("All keys exhausted.")
                break

            print(f"\nMatching batch {bs//BATCH+1}/{(len(name_targets)+BATCH-1)//BATCH}: {len(batch)} names...")
            try:
                payload = {"prospects_to_match": [
                    {"full_name": t["name"], "company_name": t["company"]}
                    for t in batch
                ]}
                result = _post(PROSP_MATCH, key, payload, rotator)
            except RuntimeError as e:
                if "credits" in str(e):
                    rotator.exhaust_current()
                    key = rotator.current()
                    if not key: break
                    result = _post(PROSP_MATCH, key, payload, rotator)
                else:
                    break

            matched = result.get("matched_prospects", [])
            for i, (target, item) in enumerate(zip(batch, matched)):
                dealer = target["dealer"]
                name   = target["name"]
                pid    = item.get("prospect_id") if isinstance(item, dict) else None
                idx    = bs + i + 1
                print(f"  [{idx:3d}/{len(name_targets)}] {name:22s} @ {dealer[:35]:35s}", end="  ", flush=True)

                if not pid:
                    print("— no match")
                    vibe_cache[dealer] = {"found": False, "reason": "no_name_match"}
                    continue

                key = rotator.current()
                if not key:
                    print("\nAll keys exhausted.")
                    Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                    break

                try:
                    contact = enrich_prospect(pid, key, rotator)
                except RuntimeError:
                    rotator.exhaust_current()
                    key = rotator.current()
                    if not key:
                        Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                        break
                    contact = enrich_prospect(pid, key, rotator)

                em = contact.get("email", "") or ""
                ph = contact.get("phone", "") or ""

                if em or ph:
                    lvl = "high" if em else "medium"
                    print(f"✓  {em or '(no email)':36s}  {ph}")
                    vibe_cache[dealer] = {
                        "found": True, "contact_name": name,
                        "contact_title": target.get("title", ""),
                        "contact_email": em, "contact_phone": ph,
                        "contact_source": "vibe", "verified_level": lvl,
                        "prospect_id": pid,
                    }
                    enriched_total += 1
                else:
                    print("— matched, no contacts")
                    vibe_cache[dealer] = {"found": False, "reason": "no_contacts", "prospect_id": pid}

                Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                rotator.rotate()
                time.sleep(0.3)

    # ── Phase B: Domain-based — match business → fetch prospects → enrich ─────
    if domain_targets and rotator.current():
        print(f"\n{'='*60}")
        print(f"Phase B — Domain enrichment for {len(domain_targets)} dealers")
        print(f"{'='*60}")

        BATCH = 50
        for bs in range(0, len(domain_targets), BATCH):
            batch = domain_targets[bs:bs + BATCH]
            key   = rotator.current()
            if not key:
                print("All keys exhausted.")
                break

            # Split batch into needs-matching vs already-matched
            needs_match = [t for t in batch if not t.get("business_id")]
            has_match   = [t for t in batch if t.get("business_id")]

            biz_id_map = {t["dealer"]: t["business_id"] for t in has_match}

            if needs_match:
                print(f"\nMatching businesses batch {bs//BATCH+1}/{(len(domain_targets)+BATCH-1)//BATCH}: {len(needs_match)} new domains ({len(has_match)} cached)...")
                try:
                    fresh_ids = match_businesses(needs_match, key, rotator)
                except RuntimeError:
                    rotator.exhaust_current()
                    key = rotator.current()
                    if not key: break
                    fresh_ids = match_businesses(needs_match, key, rotator)
                for t, bid in zip(needs_match, fresh_ids):
                    biz_id_map[t["dealer"]] = bid
                matched_count = sum(1 for b in fresh_ids if b)
                print(f"  New business matches: {matched_count}/{len(needs_match)}")
            else:
                print(f"\nBatch {bs//BATCH+1}: {len(has_match)} dealers with cached business_id")

            biz_ids = [biz_id_map.get(t["dealer"]) for t in batch]

            for i, (target, biz_id) in enumerate(zip(batch, biz_ids)):
                dealer = target["dealer"]
                domain = target["domain"]
                idx    = bs + i + 1
                print(f"  [{idx:3d}/{len(domain_targets)}] {domain:35s}  {dealer[:35]:35s}", end="  ", flush=True)

                if not biz_id:
                    print("— no biz match")
                    vibe_cache[dealer] = {"found": False, "reason": "no_biz_match", "domain": domain}
                    continue

                key = rotator.current()
                if not key:
                    print("\nAll keys exhausted.")
                    Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                    break

                # Fetch prospects at this business
                prospects = fetch_prospects_at_business(biz_id, key, rotator)
                best = _pick_best_prospect(prospects)

                if not best:
                    print("— biz found, no owner/mgr")
                    vibe_cache[dealer] = {"found": False, "reason": "no_prospects", "business_id": biz_id}
                    Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                    continue

                pid   = best.get("prospect_id") or best.get("id")
                pname = best.get("full_name") or best.get("name") or ""
                ptitle = best.get("title") or best.get("job_title") or ""

                if not pid:
                    print(f"— found {pname!r} but no prospect_id")
                    vibe_cache[dealer] = {"found": False, "reason": "no_pid"}
                    Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                    continue

                # Validate name — reject non-person names
                if not pname or len(pname.split()) < 2 or pname.lower() in SKIP_NAMES:
                    print(f"— suspect name: {pname!r}")
                    vibe_cache[dealer] = {"found": False, "reason": "suspect_name"}
                    Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                    continue

                key = rotator.current()
                if not key:
                    Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                    break

                try:
                    contact = enrich_prospect(pid, key, rotator)
                except RuntimeError:
                    rotator.exhaust_current()
                    key = rotator.current()
                    if not key:
                        Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                        break
                    contact = enrich_prospect(pid, key, rotator)

                em = contact.get("email", "") or ""
                ph = contact.get("phone", "") or ""

                if em or ph:
                    lvl = "high" if em else "medium"
                    print(f"✓ {pname:20s} | {ptitle:20s} | {em or '':33s} | {ph}")
                    vibe_cache[dealer] = {
                        "found": True, "contact_name": pname,
                        "contact_title": ptitle,
                        "contact_email": em, "contact_phone": ph,
                        "contact_source": "vibe_domain",
                        "verified_level": lvl,
                        "business_id": biz_id,
                        "prospect_id": pid,
                        "domain": domain,
                    }
                    enriched_total += 1
                else:
                    print(f"— {pname} ({ptitle}) — no contacts")
                    vibe_cache[dealer] = {"found": False, "reason": "no_contacts",
                                          "business_id": biz_id, "prospect_id": pid,
                                          "contact_name_tried": pname}

                Path(VIBE_CACHE).write_text(json.dumps(vibe_cache, indent=2))
                rotator.rotate()
                time.sleep(0.3)

    # ── Write merged contacts.csv ─────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"Writing contacts.csv...")
    rows = write_contacts(dealers_map, {
        "vibe_domain": vibe_cache,
        "vibe_old":    old_vibe,
        "rocketreach": rr_cache,
        "exa":         exa_cache,
        "web":         web_cache,
    })

    named   = sum(1 for r in rows if r["contact_name"])
    emailed = sum(1 for r in rows if r["contact_email"])
    phoned  = sum(1 for r in rows if r["contact_phone"])
    high    = sum(1 for r in rows if r["verified_level"] == "high")
    medium  = sum(1 for r in rows if r["verified_level"] == "medium")
    low     = sum(1 for r in rows if r["verified_level"] == "low")

    print(f"\ncontacts.csv  ─  {len(rows)} contacts")
    print(f"  Named          : {named}")
    print(f"  Have email     : {emailed}")
    print(f"  Have phone     : {phoned}")
    print(f"  High (name+email): {high}")
    print(f"  Medium (name)    : {medium}")
    print(f"  Low (no name)    : {low}")
    print(f"\nThis run enriched: {enriched_total} new contacts")
    print(f"Keys remaining: {len(rotator.active)}/{len(keys)}")
    print(f"Vibe cache → {VIBE_CACHE}")


if __name__ == "__main__":
    main()
