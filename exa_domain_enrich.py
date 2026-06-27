#!/usr/bin/env python3
"""
Exa neural-search domain enrichment — Phase 4g.

For dealers with no contact yet, searches Exa for pages mentioning the dealer's
domain alongside owner/manager keywords. Extracts name + title from results.

No credits charged per search — uses included Exa API quota.
Cheaper than B2B databases; finds "About Us" pages, local news, LinkedIn
snippets, press releases that B2B DBs miss.

Run: python3 exa_domain_enrich.py
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
CONTACTS_OUT = "contacts.csv"
EXA_DOM_CACHE = "cache/exa_domain_cache.json"

EXA_SEARCH_URL = "https://api.exa.ai/search"
EXA_CONTENTS_URL = "https://api.exa.ai/contents"

PLATFORM_DOMAINS = {
    "fandom.com","yelp.com","facebook.com","google.com",
    "yellowpages.com","bbb.org","manta.com","tripadvisor.com",
    "hub.biz","bizapedia.com","gunbroker.com","angieslist.com",
}

# Titles we consider valid decision-makers
GOOD_TITLES = [
    "owner","co-owner","founder","proprietor","president","ceo",
    "chief executive","general manager","operations manager","operations director",
    "director of operations","store manager","shop manager","manager","director",
    "vice president","vp","partner","principal",
]

TITLE_KW = (
    r"owner|co-owner|founder|co-founder|proprietor|president|ceo|"
    r"chief executive|general manager|gm|operations manager|operations director|"
    r"store manager|shop manager|manager|director|vice president|vp|partner|principal"
)

# Specific sentence patterns that reliably map a name to a role.
# Group 1 = name, Group 2 = title  (or reversed, noted per pattern)
PATTERNS = [
    # "John Smith, Owner"  /  "John Smith — Owner"
    re.compile(
        r'\b([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{0,20}){1,3})\s*[,\-–—]\s*('+ TITLE_KW + r')\b',
        re.IGNORECASE),
    # "Owner John Smith" / "President Jane Doe"
    re.compile(
        r'\b(' + TITLE_KW + r')\s*[,:\-–—]?\s*([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{0,20}){1,3})\b',
        re.IGNORECASE),
    # "owned/founded/operated by John Smith"
    re.compile(
        r'\b(?:owned|founded|operated|run|managed)\s+by\s+([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{0,20}){1,3})\b',
        re.IGNORECASE),
    # "John Smith is the owner/president/manager"
    re.compile(
        r'\b([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{0,20}){1,3})\s+is\s+(?:the\s+|our\s+)?(' + TITLE_KW + r')\b',
        re.IGNORECASE),
    # "Meet John Smith, our owner/manager"
    re.compile(
        r'\bMeet\s+([A-Z][a-z]{1,20}(?:\s+[A-Z][a-z]{0,20}){1,3})\b',
        re.IGNORECASE),
]

NON_NAME_WORDS = {
    "united","states","america","county","city","town","state",
    "street","avenue","road","drive","suite","floor",
    "north","south","east","west","central","new","old",
    "contact","address","phone","email","info","store","shop",
    "gun","pawn","firearms","ammo","outdoor","sports","tactical","range",
    "monday","tuesday","wednesday","thursday","friday","saturday","sunday",
    "january","february","march","april","june","july","august",
    "september","october","november","december",
    "llc","inc","corp","ltd","about","meet","team","staff","our","the",
    "click","here","read","more","learn","visit","view","back",
    "service","customer","product","item","price","checkout","buy",
    "shop","online","sale","deal","free","new","gun","rifle","pistol",
    # Job title words — a person's name cannot contain these
    "owner","founder","president","manager","director","operator",
    "chief","executive","officer","general","operations","principal",
    "partner","proprietor","vice","ceo","gm","vp",
    # Company/organization words that appear in LinkedIn snippets
    "connections","enterprises","solutions","services","group","holdings",
    "company","companies","industries","waste","energy","media",
    # Food/venue words (show up in local business search results)
    "buffet","diner","restaurant","cafe","grill","bar","pizza","kitchen",
    "hotel","motel","inn","lodge","plaza","center","mall","market",
    # Common false-positive fragments
    "of","place","business","location","number","type","page","site",
    "account","profile","listing","category","section","area","region",
    # Abbreviations and suffixes that appear after names
    "co","jr","sr","ii","iii","iv","esq","phd","md","llc","inc",
    # Financial / business concept words
    "cash","flow","revenue","profit","equity","capital","funds","asset",
    "credit","debit","payment","invoice","deposit","balance","interest",
    # Generic descriptors often seen in LinkedIn / directory snippets
    "local","family","owned","operated","veteran","licensed","certified",
    "registered","authorized","official","premier","leading","top",
    # Department / function words (corporate jargon)
    "corporate","development","technology","marketing","finance","legal",
    "procurement","compliance","logistics","distribution","manufacturing",
    # Org-type words — company/firm names are not person names
    "strategic","national","international","professional","advanced",
    "innovative","consulting","management","systems","technologies",
    "investigations","associates","alliance","coalition","federation",
    "foundation","institute","authority","bureau","agency","division",
    "network","platform","infrastructure","global","regional","united",
}


def is_person_name(name: str) -> bool:
    parts = name.strip().split()
    if len(parts) < 2 or len(parts) > 4:
        return False
    for part in parts:
        if part.lower() in NON_NAME_WORDS:
            return False
        if not re.match(r'^[A-Z][a-z]{1,19}$', part):
            return False
    return True


def _load_key() -> str:
    return os.getenv("EXA_API_KEY", "")


def _post(url: str, api_key: str, payload: dict) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        url, data=data,
        headers={"x-api-key": api_key, "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")[:200]
        if e.code == 402:
            raise RuntimeError("Exa credits exhausted")
        print(f"    [Exa HTTP {e.code}] {body}")
        return {}
    except Exception as e:
        print(f"    [Exa error] {e}")
        return {}


def extract_domain(website: str) -> str:
    if not website:
        return ""
    try:
        parsed = urlparse(website if "://" in website else "https://" + website)
        host = parsed.netloc or parsed.path
        return re.sub(r"^www\.", "", host).split("/")[0].lower().strip()
    except Exception:
        return ""


def valid_domain(domain: str) -> bool:
    if not domain:
        return False
    for p in PLATFORM_DOMAINS:
        if domain == p or domain.endswith("." + p):
            return False
    return not domain.endswith(".gov")


EMAIL_RE = re.compile(r'[\w.+-]+@[\w-]+\.[\w.]+')
PHONE_RE = re.compile(r'(?:\+?1[-.\s]?)?(?:\(?\d{3}\)?[-.\s]?)?\d{3}[-.\s]?\d{4}')
PHONE_NOISE = re.compile(r'^\d{4}$|^\d{7}$')  # reject pure extensions or short strings


def search_exa(dealer_name: str, domain: str, api_key: str) -> list[dict]:
    """Three queries, generous content window, paid API."""
    results = []
    for query in [
        f'"{dealer_name}" owner OR president OR "general manager" OR founder OR operator',
        f'site:{domain} "meet" OR "about us" OR "our team" OR "our staff" OR "contact"',
        f'site:{domain} email OR phone OR contact',
    ]:
        resp = _post(EXA_SEARCH_URL, api_key, {
            "query":         query,
            "type":          "neural",
            "numResults":    10,
            "useAutoprompt": False,
            "contents": {
                "text": {"maxCharacters": 5000},
            },
        })
        for r in resp.get("results", []):
            results.append(r)
    return results


def extract_email_phone(results: list[dict], domain: str) -> tuple[str, str]:
    """Extract email and phone. Only keep email if it matches the dealer's domain."""
    best_email = ""
    best_phone = ""

    # Build domain fingerprint: significant words from the dealer's domain
    # e.g. "theoldsoldier.com" → {"theoldsoldier"}
    # e.g. "practicalandtacticalgear.com" → {"practicalandtacticalgear"}
    domain_root = domain.split(".")[0] if domain else ""

    # Personal / tool email domains to reject entirely
    noise_domains = {
        "gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com",
        "aol.com","msn.com","live.com","me.com","mac.com",
        "wiza.co","hunter.io","apollo.io","rocketreach.co","clearbit.com",
        "skrapp.io","snov.io","findemail.io","findthat.email",
    }

    for r in results:
        text = " ".join(filter(None, [r.get("text",""), r.get("title","")]))
        for em in EMAIL_RE.findall(text):
            em = em.lower().strip(".,;:")
            em_domain = em.split("@")[-1] if "@" in em else ""
            if not em_domain or em_domain in noise_domains:
                continue
            # Only accept if the email domain matches the dealer's domain
            if domain_root and domain_root in em_domain:
                best_email = em
                break
        for ph in PHONE_RE.findall(text):
            ph = re.sub(r'[^\d+]', '', ph)
            if len(ph) >= 10 and not PHONE_NOISE.match(ph):
                best_phone = ph
                break
        if best_phone and (best_email or not domain_root):
            break

    return best_email, best_phone


def extract_contact(results: list[dict], domain: str) -> dict:
    """
    Apply specific sentence patterns to find (name, title) pairs.
    Returns the highest-priority match or empty dict.
    """
    domain_words = set(re.sub(r'[^a-z ]', ' ', domain).split())
    best = {}

    for r in results:
        text = " ".join(filter(None, [r.get("text"), r.get("title"), r.get("snippet")]))
        # Clean whitespace
        text = re.sub(r'\s+', ' ', text)

        for pat in PATTERNS:
            for m in pat.finditer(text):
                groups = m.groups()
                # Figure out which group is the name (longer, more title-case)
                if len(groups) == 1:
                    name_raw = groups[0]
                    title_raw = "owner"   # "owned by" pattern
                elif re.search(TITLE_KW, groups[0], re.IGNORECASE):
                    name_raw, title_raw = groups[1], groups[0]
                else:
                    name_raw, title_raw = groups[0], groups[1]

                name_raw = name_raw.strip()
                # Deduplicate repeated name parts (e.g. "Brian Forrest Brian Forrest")
                parts = name_raw.split()
                half  = len(parts) // 2
                if half >= 1 and parts[:half] == parts[half:]:
                    name_raw = " ".join(parts[:half])
                if not is_person_name(name_raw):
                    continue
                if any(w in domain_words for w in name_raw.lower().split()):
                    continue

                title_raw = title_raw.strip().lower()
                pri = next((i for i, t in enumerate(GOOD_TITLES) if t in title_raw), 99)
                cur_pri = next((i for i, t in enumerate(GOOD_TITLES) if t in best.get("title","").lower()), 99)

                if not best or pri < cur_pri:
                    best = {"name": name_raw, "title": title_raw, "url": r.get("url","")}

    return best


def collect_targets(dealers: list[dict], exa_dom_cache: dict,
                    contacts_csv: str, top_n: int = 150) -> list[dict]:
    covered = set()
    if Path(contacts_csv).exists():
        for r in csv.DictReader(open(contacts_csv)):
            if r.get("contact_email") or r.get("contact_phone"):
                covered.add(r["dealer_name"])

    candidates = []
    for d in dealers:
        dealer   = d["dealer_name"]
        website  = d.get("website","").strip()
        domain   = extract_domain(website)
        listings = int(d.get("total_listings","0") or 0)

        if dealer in exa_dom_cache:
            continue
        if dealer in covered:
            continue
        if not valid_domain(domain):
            continue

        candidates.append({
            "dealer":         dealer,
            "domain":         domain,
            "website":        website,
            "total_listings": listings,
        })

    candidates.sort(key=lambda x: x["total_listings"], reverse=True)
    return candidates[:top_n]


def main():
    api_key = _load_key()
    if not api_key:
        print("ERROR: EXA_API_KEY not set in .env")
        return

    dealers      = list(csv.DictReader(open(ENRICHED)))
    exa_dom_cache = json.loads(Path(EXA_DOM_CACHE).read_text()) if Path(EXA_DOM_CACHE).exists() else {}

    targets = collect_targets(dealers, exa_dom_cache, CONTACTS_OUT, top_n=500)
    print(f"Exa domain search targets: {len(targets)} (all dealers with valid domains)")
    print(f"Already cached: {len(exa_dom_cache)}")
    print()

    found = not_found = 0

    for i, t in enumerate(targets, 1):
        dealer = t["dealer"]
        domain = t["domain"]
        listings = t["total_listings"]
        print(f"[{i:3d}/{len(targets)}] {listings:4d}L  {domain:35s} {dealer[:32]}", end="  ", flush=True)

        try:
            results = search_exa(dealer, domain, api_key)
        except RuntimeError as e:
            print(f"\nExa error: {e}")
            Path(EXA_DOM_CACHE).write_text(json.dumps(exa_dom_cache, indent=2))
            break

        contact = extract_contact(results, domain)

        email, phone = extract_email_phone(results, domain)

        if contact:
            name  = contact["name"]
            title = contact["title"]
            url   = contact["url"]
            lvl   = "high" if (name and email) else ("medium" if name else "low")
            extras = []
            if email: extras.append(email)
            if phone: extras.append(phone)
            print(f"✓ {name} ({title})" + (f"  [{', '.join(extras)}]" if extras else ""))
            exa_dom_cache[dealer] = {
                "found":          True,
                "contact_name":   name,
                "contact_title":  title,
                "contact_email":  email,
                "contact_phone":  phone,
                "contact_source": "exa_domain",
                "verified_level": lvl,
                "source_url":     url,
                "domain":         domain,
            }
            found += 1
        elif email or phone:
            extras = []
            if email: extras.append(email)
            if phone: extras.append(phone)
            print(f"— name not found  [{', '.join(extras)}]")
            exa_dom_cache[dealer] = {
                "found":          True,
                "contact_name":   "",
                "contact_title":  "",
                "contact_email":  email,
                "contact_phone":  phone,
                "contact_source": "exa_domain",
                "verified_level": "low",
                "domain":         domain,
            }
            found += 1
        else:
            print("— no contact found")
            exa_dom_cache[dealer] = {"found": False, "domain": domain}
            not_found += 1

        Path(EXA_DOM_CACHE).write_text(json.dumps(exa_dom_cache, indent=2))
        time.sleep(0.2)

    print(f"\nExa domain results ({len(targets)} searched):")
    print(f"  Found    : {found}")
    print(f"  Not found: {not_found}")
    print(f"\nRun merge_contacts.py to include these in contacts.csv")


if __name__ == "__main__":
    main()
