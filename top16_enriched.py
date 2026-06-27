#!/usr/bin/env python3
"""
Enrich the first 16 rows of marketplace_presence.csv (through Plink Firearms).
Runs Serper on the 5 dealers not yet in any cache.
Output defaults to top16_enriched.csv in the repo root.
"""

import csv, json, os, re, time, requests
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

BASE        = Path(__file__).resolve().parent
MP_INPUT    = os.getenv("MARKETPLACE_PRESENCE_CSV", str(BASE / "marketplace_presence.csv"))
OUTPUT      = os.getenv("TOP16_ENRICHED_CSV", str(BASE / "top16_enriched.csv"))
MP_SERPER   = BASE / "cache/marketplace_serper_cache.json"
SERPER_URL  = "https://google.serper.dev/search"

EMAIL_RE    = re.compile(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}')
PHONE_RE    = re.compile(r'(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}')
PHONE_NOISE = re.compile(r'^(?:0{7,}|1{7,}|\d{5,6}$)')
NOISE_DOMAINS = {"gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com",
                 "example.com","yourdomain.com","domain.com","wiza.co","hunter.io","apollo.io"}
GENERIC_PREFIXES = ["info@","sales@","contact@","support@","admin@","mail@","hello@",
    "store@","shop@","service@","office@","customerservice@","contactus@",
    "orders@","events@","questions@","online@","feedback@","webmaster@",
    "noreply@","no-reply@","donotreply@"]

# Key rotation
_serper_keys = []; _serper_idx = 0

def _load_keys():
    global _serper_keys
    _serper_keys = [v for k in ["SERPER_API_KEY","SERPER_API_KEY_2","SERPER_API_KEY_3"]
                    if (v := os.getenv(k,"").strip())]
    print(f"Serper keys: {len(_serper_keys)}")

def _serper_key():
    return _serper_keys[_serper_idx]

def _rotate_serper():
    global _serper_idx
    _serper_idx += 1
    if _serper_idx >= len(_serper_keys):
        raise RuntimeError("All Serper keys exhausted")

def extract_domain(url):
    if not url: return ""
    url = re.sub(r'^https?://', '', url).strip('/')
    url = re.sub(r'^www\.', '', url)
    return url.split('/')[0].lower().strip()

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
            time.sleep(3 * (attempt + 1))
    return {}

def extract_from_serper(data, domain):
    texts = []
    for r in data.get("organic", []):
        texts += [r.get("snippet",""), r.get("title","")]
        for s in r.get("sitelinks",[]): texts.append(s.get("snippet",""))
    for r in data.get("peopleAlsoAsk",[]): texts.append(r.get("snippet",""))
    texts.append(data.get("knowledgeGraph",{}).get("description",""))
    return extract_contacts(" ".join(filter(None, texts)), domain)

def enrich_serper(name, domain):
    queries = [
        f"site:{domain} email contact",
        f"{name} email contact owner",
        f"{domain} contact email phone",
    ]
    email = ""; phone = ""
    for q in queries:
        data = serper_search(q)
        e, p = extract_from_serper(data, domain)
        if e and not email: email = e
        if p and not phone: phone = p
        if email and phone: break
        time.sleep(0.2)
    return email, phone

def load_all_caches():
    caches = {}
    paths = {
        "vibe":       BASE / "cache/vibe_domain_cache.json",
        "exa_domain": BASE / "cache/exa_domain_cache.json",
        "serper":     BASE / "cache/serper_cache.json",
        "tavily":     BASE / "cache/tavily_cache.json",
        "exa_rerun":  BASE / "cache/exa_rerun_cache.json",
        "exa_wf":     BASE / "cache/exa_web_fetch_cache.json",
        "web":        BASE / "cache/contacts_web_cache.json",
        "apollo":     BASE / "cache/apollo_contacts_cache.json",
        "mp_serper":  MP_SERPER,
    }
    for k, p in paths.items():
        caches[k] = json.loads(p.read_text()) if p.exists() else {}
    return caches

GENERIC_PREFIXES_SET = set(GENERIC_PREFIXES)

def is_generic(email):
    return any(email.lower().startswith(p) for p in GENERIC_PREFIXES) if email else False

def is_valid_email(email):
    if not email or "@" not in email: return False
    dom = email.split("@")[-1]
    return "." in dom and dom.lower() not in NOISE_DOMAINS

def is_valid_phone(phone):
    return len(re.sub(r"[^\d]", "", phone or "")) >= 10

EMAIL_ORDER = ["vibe","exa_domain","tavily","serper","exa_wf","web","mp_serper"]
PHONE_ORDER = ["serper","tavily","exa_domain","exa_rerun","exa_wf","web","vibe","mp_serper"]
NAME_ORDER  = ["vibe","apollo","exa_domain","exa_wf","web"]

def pick_email(caches, name):
    found = {}
    for src in EMAIL_ORDER:
        e = caches.get(src,{}).get(name,{}).get("contact_email","").strip()
        if e and is_valid_email(e): found[src] = e
    if not found: return "", "", False
    from collections import Counter
    agreement = Counter(found.values())
    for email, count in agreement.most_common():
        if count >= 2 and not is_generic(email):
            srcs = [s for s,e in found.items() if e == email]
            return email, "+".join(srcs), False
    for src in EMAIL_ORDER:
        e = found.get(src,"")
        if e and not is_generic(e): return e, src, False
    for email, count in agreement.most_common():
        if count >= 2:
            srcs = [s for s,e in found.items() if e == email]
            return email, "+".join(srcs), True
    for src in EMAIL_ORDER:
        e = found.get(src,"")
        if e: return e, src, True
    return "", "", False

def pick_phone(caches, name):
    for src in PHONE_ORDER:
        p = caches.get(src,{}).get(name,{}).get("contact_phone","").strip()
        if p and is_valid_phone(p):
            return re.sub(r"[^\d+]","",p), src
    return "", ""

def pick_name(caches, name):
    for src in NAME_ORDER:
        n = caches.get(src,{}).get(name,{}).get("contact_name","").strip()
        if n and n.lower() not in ("none none","none",""):
            t = caches.get(src,{}).get(name,{}).get("contact_title","").strip()
            return n, t, src
    return "", "", ""

def all_email_sources(caches, name):
    parts = []
    for src in EMAIL_ORDER:
        e = caches.get(src,{}).get(name,{}).get("contact_email","").strip()
        if e and is_valid_email(e): parts.append(f"{src}:{e}")
    return " | ".join(parts)

def main():
    _load_keys()

    all_rows = list(csv.DictReader(open(MP_INPUT, encoding="utf-8-sig")))
    top16 = all_rows[:16]

    caches = load_all_caches()
    mp_serper = caches["mp_serper"]

    existing_keys = set()
    for src in ["vibe","exa_domain","serper","tavily","exa_rerun","exa_wf","web","apollo"]:
        existing_keys.update(caches[src].keys())
    existing_upper = {k.upper(): k for k in existing_keys}

    # Find which of the 16 need Serper enrichment
    to_enrich = []
    for r in top16:
        name = r["company_name"].strip()
        if name in existing_keys or name.upper() in existing_upper: continue
        if name in mp_serper: continue
        domain = extract_domain(r.get("best_domain",""))
        if not domain or "." not in domain: continue
        to_enrich.append({"name": name, "domain": domain})

    if to_enrich:
        print(f"\nRunning Serper on {len(to_enrich)} unmatched dealers:")
        for t in to_enrich:
            print(f"  {t['name']} ({t['domain']})", end="  ", flush=True)
            try:
                email, phone = enrich_serper(t["name"], t["domain"])
            except RuntimeError as e:
                print(f"\nSerper error: {e}")
                MP_SERPER.write_text(json.dumps(mp_serper, indent=2))
                break
            if email or phone:
                print(f"✓ {email or '-'} / {phone or '-'}")
                mp_serper[t["name"]] = {"found": True, "contact_email": email,
                    "contact_phone": phone, "contact_source": "serper", "domain": t["domain"]}
            else:
                print("— not found")
                mp_serper[t["name"]] = {"found": False, "domain": t["domain"]}
            MP_SERPER.write_text(json.dumps(mp_serper, indent=2))
            time.sleep(0.3)
    else:
        print("All 16 dealers already in cache — skipping Serper.")

    # Reload after enrichment
    caches["mp_serper"] = mp_serper

    out_fields = [
        "company_name","best_domain","total_listings","guns_com","gunbroker","armslist",
        "marketplace_count","estimated_hours",
        "contact_name","title","contact_email","contact_phone",
        "email_source","email_type","phone_source","name_source",
        "all_email_sources","quality_flag",
    ]

    rows = []
    for r in top16:
        name = r["company_name"].strip()
        email, email_src, generic = pick_email(caches, name)
        phone, phone_src          = pick_phone(caches, name)
        cname, title, name_src    = pick_name(caches, name)
        all_srcs                  = all_email_sources(caches, name)

        if email and not generic:   flag = "good"
        elif email and generic:     flag = "generic_email"
        elif phone:                 flag = "phone_only"
        else:                       flag = "no_contact"

        rows.append({
            "company_name":      name,
            "best_domain":       r.get("best_domain",""),
            "total_listings":    r.get("total_listings",""),
            "guns_com":          r.get("guns_com",""),
            "gunbroker":         r.get("gunbroker",""),
            "armslist":          r.get("armslist",""),
            "marketplace_count": r.get("marketplace_count",""),
            "estimated_hours":   r.get("estimated_hours",""),
            "contact_name":      cname,
            "title":             title,
            "contact_email":     email,
            "contact_phone":     phone,
            "email_source":      email_src,
            "email_type":        "generic" if generic else ("personal" if email else ""),
            "phone_source":      phone_src,
            "name_source":       name_src,
            "all_email_sources": all_srcs,
            "quality_flag":      flag,
        })

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(rows)

    print(f"\nOutput: {OUTPUT}")
    print(f"\n{'Company':38s} {'Listings':8s} {'Email':40s} {'Phone':14s} {'Flag'}")
    print(f"  {'-'*38} {'-'*8} {'-'*40} {'-'*14} {'-'*12}")
    for r in rows:
        print(f"  {r['company_name'][:38]:38s} {str(r['total_listings']):8s} "
              f"{(r['contact_email'] or '-')[:40]:40s} {(r['contact_phone'] or '-'):14s} {r['quality_flag']}")

if __name__ == "__main__":
    main()
