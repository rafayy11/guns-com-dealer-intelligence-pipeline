#!/usr/bin/env python3
"""
Enrich unmatched marketplace_presence.csv dealers via Serper + Tavily.
Only targets dealers not already in the main enrichment caches.
Saves to: cache/marketplace_serper_cache.json
          cache/marketplace_tavily_cache.json
"""

import csv, json, os, re, time, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE       = Path(__file__).resolve().parent
MP_INPUT   = os.getenv("MARKETPLACE_PRESENCE_CSV", str(BASE / "marketplace_presence.csv"))
SERPER_CACHE = BASE / "cache/marketplace_serper_cache.json"
TAVILY_CACHE = BASE / "cache/marketplace_tavily_cache.json"
SERPER_URL   = "https://google.serper.dev/search"
TAVILY_URL   = "https://api.tavily.com/search"

EMAIL_RE    = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_RE    = re.compile(r'(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}')
PHONE_NOISE = re.compile(r'^(?:0{7,}|1{7,}|\d{5,6}$)')
NOISE_DOMAINS = {"gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com",
                 "example.com","yourdomain.com","domain.com","wiza.co","hunter.io","apollo.io"}
GENERIC_PREFIXES = ["info@","sales@","contact@","support@","admin@","mail@","hello@",
    "store@","shop@","service@","office@","customerservice@","contactus@",
    "orders@","events@","questions@","online@","feedback@","webmaster@",
    "noreply@","no-reply@","donotreply@"]

# ── Key rotation ──────────────────────────────────────────────────────────────
_serper_keys = []; _serper_idx = 0
_tavily_keys = []; _tavily_idx = 0

def _load_keys():
    global _serper_keys, _tavily_keys
    _serper_keys = [v for k in ["SERPER_API_KEY","SERPER_API_KEY_2","SERPER_API_KEY_3"]
                    if (v := os.getenv(k,"").strip())]
    _tavily_keys = [v for k in ["TAVILY_API_KEY","TAVILY_API_KEY_2","TAVILY_API_KEY_3"]
                    if (v := os.getenv(k,"").strip())]
    print(f"Serper keys: {len(_serper_keys)}  Tavily keys: {len(_tavily_keys)}")

def _serper_key():
    return _serper_keys[_serper_idx]

def _rotate_serper():
    global _serper_idx
    _serper_idx += 1
    if _serper_idx >= len(_serper_keys):
        raise RuntimeError("All Serper keys exhausted")
    print(f"  [Serper key rotated → {_serper_idx+1}]")

def _tavily_key():
    return _tavily_keys[_tavily_idx]

def _rotate_tavily():
    global _tavily_idx
    _tavily_idx += 1
    if _tavily_idx >= len(_tavily_keys):
        raise RuntimeError("All Tavily keys exhausted")
    print(f"  [Tavily key rotated → {_tavily_idx+1}]")

# ── Domain helpers ─────────────────────────────────────────────────────────────
def extract_domain(url):
    if not url: return ""
    url = re.sub(r'^https?://', '', url).strip('/')
    url = re.sub(r'^www\.', '', url)
    return url.split('/')[0].lower().strip()

# ── Email/phone extractors ─────────────────────────────────────────────────────
def extract_contacts(text, domain):
    domain_root = domain.split(".")[0] if domain else ""
    best_email = ""; best_phone = ""; generic_email = ""

    for em in EMAIL_RE.findall(text):
        em = em.lower().strip(".,;:()")
        em_dom = em.split("@")[-1] if "@" in em else ""
        if not em_dom or em_dom in NOISE_DOMAINS: continue
        if domain_root and domain_root not in em_dom: continue
        if any(em.startswith(p) for p in GENERIC_PREFIXES):
            if not generic_email: generic_email = em
            continue
        best_email = em; break

    if not best_email and generic_email:
        best_email = generic_email

    for ph in PHONE_RE.findall(text):
        ph = re.sub(r'[^\d+]', '', ph)
        if len(ph) >= 10 and not PHONE_NOISE.match(ph):
            best_phone = ph; break

    return best_email, best_phone

# ── API calls ──────────────────────────────────────────────────────────────────
def serper_search(query, num=10):
    for attempt in range(3):
        try:
            r = requests.post(SERPER_URL,
                json={"q": query, "num": num},
                headers={"X-API-KEY": _serper_key(), "Content-Type": "application/json"},
                timeout=(5, 15))
            if r.status_code == 429: time.sleep(5); continue
            if r.status_code in (401, 403): _rotate_serper(); return serper_search(query, num)
            if r.status_code != 200: return {}
            return r.json()
        except RuntimeError: raise
        except Exception as e:
            wait = 3 * (attempt + 1)
            print(f"    [Serper retry {attempt+1}/3 in {wait}s: {e}]")
            time.sleep(wait)
    return {}

def tavily_search(query, max_results=5):
    for attempt in range(3):
        try:
            r = requests.post(TAVILY_URL,
                json={"query": query, "search_depth": "basic", "max_results": max_results,
                      "include_raw_content": True, "include_answer": True},
                headers={"Authorization": f"Bearer {_tavily_key()}", "Content-Type": "application/json"},
                timeout=(5, 20))
            if r.status_code == 429: time.sleep(5); continue
            if r.status_code in (401, 403): raise RuntimeError(f"Tavily auth error {r.status_code}")
            if r.status_code == 402: _rotate_tavily(); return tavily_search(query, max_results)
            if r.status_code != 200: return {}
            return r.json()
        except RuntimeError: raise
        except Exception as e:
            wait = 3 * (attempt + 1)
            print(f"    [Tavily retry {attempt+1}/3 in {wait}s: {e}]")
            time.sleep(wait)
    return {}

def extract_from_serper(data, domain):
    texts = []
    for r in data.get("organic", []):
        texts += [r.get("snippet",""), r.get("title","")]
        for s in r.get("sitelinks",[]): texts.append(s.get("snippet",""))
    for r in data.get("peopleAlsoAsk",[]): texts.append(r.get("snippet",""))
    kg = data.get("knowledgeGraph",{})
    texts.append(kg.get("description",""))
    return extract_contacts(" ".join(filter(None, texts)), domain)

def extract_from_tavily(data, domain):
    texts = [data.get("answer","") or ""]
    for r in data.get("results",[]):
        texts += [r.get("content","") or "", r.get("raw_content","") or "", r.get("title","") or ""]
    return extract_contacts(" ".join(filter(None, texts)), domain)

# ── Per-dealer search ──────────────────────────────────────────────────────────
def enrich_serper(name, domain):
    queries = [
        f"site:{domain} email contact",
        f"{name} email contact owner",
        f"{domain} contact email phone",
    ]
    email = ""; phone = ""
    for q in queries:
        try: data = serper_search(q)
        except RuntimeError: raise
        e, p = extract_from_serper(data, domain)
        if e and not email: email = e
        if p and not phone: phone = p
        if email and phone: break
        time.sleep(0.2)
    return email, phone

def enrich_tavily(name, domain):
    queries = [
        f"email contact {domain}",
        f'"{name}" owner email phone contact',
        f"{domain} contact us email address",
    ]
    email = ""; phone = ""
    for q in queries:
        try: data = tavily_search(q)
        except RuntimeError: raise
        e, p = extract_from_tavily(data, domain)
        if e and not email: email = e
        if p and not phone: phone = p
        if email and phone: break
        time.sleep(0.3)
    return email, phone

# ── Main ───────────────────────────────────────────────────────────────────────
def load_existing_caches():
    all_keys = set()
    for src, path in [
        ('vibe', BASE/"cache/vibe_domain_cache.json"),
        ('serper', BASE/"cache/serper_cache.json"),
        ('tavily', BASE/"cache/tavily_cache.json"),
        ('exa_domain', BASE/"cache/exa_domain_cache.json"),
        ('exa_rerun', BASE/"cache/exa_rerun_cache.json"),
        ('exa_wf', BASE/"cache/exa_web_fetch_cache.json"),
    ]:
        if path.exists():
            all_keys.update(json.loads(path.read_text()).keys())
    return all_keys

def main():
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--serper", action="store_true", default=True)
    parser.add_argument("--tavily", action="store_true", default=False)
    parser.add_argument("--no-serper", action="store_true")
    args = parser.parse_args()

    _load_keys()
    run_serper = args.serper and not args.no_serper
    run_tavily = args.tavily

    existing = load_existing_caches()
    existing_upper = {k.upper(): k for k in existing}

    mp = list(csv.DictReader(open(MP_INPUT, encoding="utf-8-sig")))

    serper_cache = json.loads(SERPER_CACHE.read_text()) if SERPER_CACHE.exists() else {}
    tavily_cache = json.loads(TAVILY_CACHE.read_text()) if TAVILY_CACHE.exists() else {}

    targets = []
    for r in mp:
        name = r["company_name"].strip()
        if name in existing or name.upper() in existing_upper:
            continue
        domain = extract_domain(r.get("best_domain",""))
        if not domain or "." not in domain:
            continue
        listings = int(r.get("total_listings",0) or 0)
        targets.append({"name": name, "domain": domain, "listings": listings})

    targets.sort(key=lambda x: x["listings"], reverse=True)

    if run_serper:
        remaining = [t for t in targets if t["name"] not in serper_cache]
        print(f"\nSerper targets: {len(remaining)} (already cached: {len(targets)-len(remaining)})")
        found = 0
        for i, t in enumerate(remaining, 1):
            print(f"[{i:3d}/{len(remaining)}] {t['listings']:5d}L  {t['domain']:35s} {t['name'][:25]}", end="  ", flush=True)
            try:
                email, phone = enrich_serper(t["name"], t["domain"])
            except RuntimeError as e:
                print(f"\nSerper error: {e}")
                SERPER_CACHE.write_text(json.dumps(serper_cache, indent=2))
                break
            if email or phone:
                parts = [x for x in [email, phone] if x]
                print(f"✓ [{', '.join(parts)}]")
                serper_cache[t["name"]] = {"found": True, "contact_email": email,
                    "contact_phone": phone, "contact_source": "serper", "domain": t["domain"]}
                found += 1
            else:
                print("— not found")
                serper_cache[t["name"]] = {"found": False, "domain": t["domain"]}
            SERPER_CACHE.write_text(json.dumps(serper_cache, indent=2))
            time.sleep(0.3)
        print(f"\nSerper: {found}/{len(remaining)} found")

    if run_tavily:
        remaining = [t for t in targets if t["name"] not in tavily_cache]
        print(f"\nTavily targets: {len(remaining)} (already cached: {len(targets)-len(remaining)})")
        found = 0
        for i, t in enumerate(remaining, 1):
            print(f"[{i:3d}/{len(remaining)}] {t['listings']:5d}L  {t['domain']:35s} {t['name'][:25]}", end="  ", flush=True)
            try:
                email, phone = enrich_tavily(t["name"], t["domain"])
            except RuntimeError as e:
                print(f"\nTavily error: {e}")
                TAVILY_CACHE.write_text(json.dumps(tavily_cache, indent=2))
                break
            if email or phone:
                parts = [x for x in [email, phone] if x]
                print(f"✓ [{', '.join(parts)}]")
                tavily_cache[t["name"]] = {"found": True, "contact_email": email,
                    "contact_phone": phone, "contact_source": "tavily", "domain": t["domain"]}
                found += 1
            else:
                print("— not found")
                tavily_cache[t["name"]] = {"found": False, "domain": t["domain"]}
            TAVILY_CACHE.write_text(json.dumps(tavily_cache, indent=2))
            time.sleep(0.3)
        print(f"\nTavily: {found}/{len(remaining)} found")

if __name__ == "__main__":
    main()
