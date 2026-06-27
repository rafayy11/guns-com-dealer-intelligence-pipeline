#!/usr/bin/env python3
"""
Phase 1 — Contact Enrichment: Scrape dealer websites for owner/manager contacts.

Goal: Find the PERSON who feels operational pain — owner, ops manager, director.
We want THEIR email (personal or company) and THEIR direct phone, not the store's
generic inbox or front-desk number.

Verification rule:
  - Email/phone are only saved if they are found within 300 chars of the person's
    name+title on the page. Generic store emails (info@, sales@) without a named
    owner attached are marked low-confidence and passed to Apollo for enrichment.

Verified levels:
  high   — name + title + email all found near each other on page
  medium — name + title found, no email (name passed to Vibe for email lookup)
  low    — only generic store contact found, no owner identified

Cache:   cache/contacts_web_cache.json  (fully resumable)
Output:  contacts.csv

Run: python3 scrape_contacts.py
"""

import csv
import json
import os
import re
import time
import random
import urllib.request
import urllib.error
from pathlib import Path
from urllib.parse import urlparse
from dotenv import load_dotenv

load_dotenv()

ENRICHED     = "dealers_enriched.csv"
CONTACTS_OUT = "contacts.csv"
CACHE        = "cache/contacts_web_cache.json"
EXA_CONTENTS = "https://api.exa.ai/contents"

CONTACT_PATHS = [
    "/contact-us", "/contact",
    "/about-us", "/about",
    "/our-team", "/team", "/staff",
    "/meet-the-team", "/meet-us", "/who-we-are",
    "",   # homepage — most noise, last resort
]

# Priority order matters — index 0 = most valuable (owner)
OWNER_TITLES   = ["owner", "co-owner", "co owner", "proprietor",
                   "founder", "co-founder", "co founder",
                   "president", "ceo", "chief executive officer"]
MANAGER_TITLES = ["general manager", "gm", "operations manager",
                   "director of operations", "operations director",
                   "store manager", "shop manager", "manager", "director"]
ALL_TITLES     = OWNER_TITLES + MANAGER_TITLES

# Generic email prefixes — acceptable ONLY if we also have an owner name nearby
GENERIC_PREFIXES = {
    "info", "contact", "sales", "admin", "office", "hello", "support",
    "mail", "email", "store", "shop", "help", "service", "general",
    "inquiry", "enquiry", "team", "staff",
}

NOT_NAMES = {
    "the", "our", "your", "this", "that", "with", "from", "about",
    "meet", "contact", "welcome", "store", "shop", "gun", "pawn",
    "email", "phone", "fax", "call", "send", "visit", "click",
    "home", "new", "used", "buy", "sell", "trade", "please",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "open", "closed", "hours", "time",
    "north", "south", "east", "west", "street", "avenue", "road",
    "suite", "floor", "office", "location", "address", "city",
    "just", "also", "more", "most", "some", "have", "will",
    "been", "they", "them", "their", "what", "when", "where",
    "which", "while", "after", "before", "between", "through",
    "during", "each", "both", "only", "then", "than", "over",
    "right", "left", "best", "good", "great", "high", "low",
    "very", "well", "back", "next", "first", "last", "long",
    # CSS / typography / web-design terms that can look like "First Last"
    "font", "sans", "serif", "bold", "italic", "light", "medium",
    "regular", "black", "condensed", "extended", "narrow", "wide",
    "reserved", "heading", "display", "body", "weight", "style",
    "color", "size", "family", "face", "type", "text", "line",
    "web", "mono", "neue", "pro", "thin", "semibold", "heavy",
    "ultra", "extra", "variable", "rounded", "oblique", "alternate",
    "block", "inline", "flex", "grid", "row", "col", "span",
    "nav", "bar", "hero", "banner", "footer", "header", "sidebar",
    "modal", "popup", "dropdown", "menu", "link", "item", "list",
    "logo", "icon", "image", "photo", "video", "slider", "carousel",
    "lorem", "ipsum", "dolor", "amet", "read", "more", "learn",
    "scroll", "load", "submit", "reset", "cancel", "close", "open",
    "view", "show", "hide", "toggle", "edit", "save", "delete",
    # Firearms/retail generic words
    "ammo", "ammunition", "firearm", "firearms", "rifle", "pistol",
    "shotgun", "handgun", "shooting", "range", "hunting", "tactical",
    "price", "sale", "deal", "special", "offer", "discount", "free",
    "shipping", "return", "policy", "warranty", "service", "repair",
    # Layout/UI
    "page", "section", "content", "main", "area", "zone", "region",
    "wrapper", "container", "inner", "outer", "top", "bottom",
    "center", "middle", "side", "base", "full", "half", "wide",
    # Numbers (word form)
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "second", "third", "fourth",
    "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    # Month names
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    # Action/status words
    "updated", "posted", "published", "written", "filed", "created",
    "against", "because", "since", "within", "without", "around",
    "under", "above", "inside", "outside",
    # Career/job-posting phrases
    "career", "careers", "hiring", "apply", "lead", "leads",
    "crew", "position", "role", "candidate",
    # Constitutional / legal (common in gun shop names)
    "amendment", "constitutional", "rights", "liberty", "freedom",
    "patriot", "patriotic", "republic", "militia", "defense", "defensive",
    # Questions / abstract words
    "questions", "question", "answers", "answer", "problems",
    # Business/company name words that look like "First Last"
    "sports", "arms", "outdoors", "outfitters", "outdoor", "supply",
    "supplies", "enterprises", "solutions", "systems", "services",
    "group", "company", "industries", "holdings", "ventures",
    "mega", "super", "plus", "premier", "elite",
    "national", "regional", "local", "global", "american", "united",
    "federal", "state", "county", "valley", "mountain",
    "river", "lake", "creek", "ridge", "hills", "plains", "coast",
    "indoor", "outlet", "market", "exchange", "trading",
    "pawnbrokers", "jewelry", "jewelers", "auction",
}

_PHONE_RE = re.compile(
    r'\b(?:\+1[-.\s]?)?\(?(\d{3})\)?[-.\s](\d{3})[-.\s](\d{4})\b'
)
_EMAIL_RE = re.compile(
    r'\b([a-zA-Z0-9._%+\-]+)@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b'
)
_NAME_RE = re.compile(r'\b([A-Z][a-z]{1,20})\s+([A-Z][a-z]{1,20})\b')


def _domain(url: str) -> str:
    try:
        return urlparse(url).netloc.lstrip("www.").lower()
    except Exception:
        return ""


def _fmt_phone(m) -> str:
    return f"({m.group(1)}) {m.group(2)}-{m.group(3)}"


def _is_generic_email(email: str) -> bool:
    prefix = email.split("@")[0].lower()
    return prefix in GENERIC_PREFIXES


def extract_jsonld_person(html: str) -> dict:
    """Extract Person data from schema.org JSON-LD blocks — most reliable source."""
    for block in re.findall(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE
    ):
        try:
            data = json.loads(block)
            # Handle @graph wrapper
            items = data.get("@graph", [data]) if isinstance(data, dict) else []
            for item in items:
                if not isinstance(item, dict):
                    continue
                t = item.get("@type", "")
                if "Person" in t or "Employee" in t:
                    name  = item.get("name", "").strip()
                    title = item.get("jobTitle", "").strip()
                    email = item.get("email", "").lower().replace("mailto:", "").strip()
                    phone = item.get("telephone", "").strip()
                    if name and len(name.split()) >= 2:
                        return {"name": name, "title": title,
                                "email": email, "phone": phone}
        except Exception:
            pass
    return {}


def find_person_contact(text: str) -> dict | None:
    """
    Scan page text for a name+title pair, then look for email/phone
    within 300 characters of that person mention.

    Returns the best contact dict or None.
    """
    lower = text.lower()
    best  = None

    for priority, title in enumerate(ALL_TITLES):
        for m in re.finditer(re.escape(title), lower):
            pos     = m.start()
            # Window around the title mention
            w_start = max(0, pos - 250)
            w_end   = min(len(text), pos + 250)
            window  = text[w_start:w_end]

            # Look for a person name in this window
            for nm in _NAME_RE.finditer(window):
                first = nm.group(1).lower()
                last  = nm.group(2).lower()
                if first in NOT_NAMES or last in NOT_NAMES:
                    continue
                if len(first) < 2 or len(last) < 2:
                    continue

                name = f"{nm.group(1)} {nm.group(2)}"

                # Now search a wider window (500 chars around name position
                # in the original text) for email + phone
                name_pos_in_text = text.find(nm.group(0), max(0, w_start - 50))
                if name_pos_in_text == -1:
                    name_pos_in_text = pos

                c_start = max(0, name_pos_in_text - 300)
                c_end   = min(len(text), name_pos_in_text + 300)
                context = text[c_start:c_end]

                emails = [
                    f"{em.group(1)}@{em.group(2)}".lower()
                    for em in _EMAIL_RE.finditer(context)
                ]
                phones = [_fmt_phone(ph) for ph in _PHONE_RE.finditer(context)]

                # Prefer non-generic email, but accept generic if it's the only one
                owner_email = ""
                for e in emails:
                    if not _is_generic_email(e):
                        owner_email = e
                        break
                if not owner_email and emails:
                    owner_email = emails[0]   # generic accepted when name is known

                result = {
                    "contact_name":   name,
                    "contact_title":  title,
                    "contact_email":  owner_email,
                    "contact_phone":  phones[0] if phones else "",
                    "contact_source": "web_page",
                    "verified_level": "high" if owner_email else "medium",
                    "_priority":      priority,
                }

                if best is None or priority < best["_priority"]:
                    best = result

                # Stop early if we have high-confidence owner contact
                if priority == 0 and owner_email:
                    return best

    return best


def find_store_contact_fallback(text: str, site_domain: str) -> dict | None:
    """
    If no owner name found, extract any business email + phone as low-confidence
    fallback (used by Apollo/Exa phases to enrich further).
    """
    emails = [
        f"{m.group(1)}@{m.group(2)}".lower()
        for m in _EMAIL_RE.finditer(text)
    ]
    phones = [_fmt_phone(m) for m in _PHONE_RE.finditer(text)]

    # Filter to domain-matching emails first
    domain_emails = [e for e in emails if site_domain and e.split("@")[1] == site_domain]
    chosen_email  = domain_emails[0] if domain_emails else (emails[0] if emails else "")

    if chosen_email or phones:
        return {
            "contact_name":   "",
            "contact_title":  "",
            "contact_email":  chosen_email,
            "contact_phone":  phones[0] if phones else "",
            "contact_source": "web_store_page",
            "verified_level": "low",
            "_priority":      999,
        }
    return None


def _is_cloudflare_block(html: str) -> bool:
    """Detect Cloudflare challenge / access-denied pages."""
    if not html:
        return True
    lower = html[:3000].lower()
    return any(s in lower for s in [
        "checking your browser", "cloudflare", "enable javascript",
        "cf-browser-verification", "just a moment", "ddos protection",
        "access denied", "403 forbidden", "verifying you are human",
        "challenge-form", "cf_clearance",
    ])


def _get_exa_key() -> str:
    return os.getenv("EXA_API_KEY", "")


def exa_fetch(url: str) -> str:
    """Fetch page text via Exa's server-side fetcher — bypasses Cloudflare."""
    key = _get_exa_key()
    if not key:
        return ""
    try:
        data = json.dumps({"ids": [url], "text": True}).encode("utf-8")
        req  = urllib.request.Request(
            EXA_CONTENTS, data=data,
            headers={"Content-Type": "application/json", "x-api-key": key},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            results = json.loads(resp.read()).get("results", [])
            if results:
                title = results[0].get("title", "")
                text  = results[0].get("text", "")[:18000]
                return f"<title>{title}</title> {text}"
    except Exception:
        pass
    return ""


def fetch_text(sb, url: str) -> str:
    try:
        sb.uc_open_with_reconnect(url, reconnect_time=3)
        time.sleep(random.uniform(1.2, 2.0))
        src = sb.get_page_source() or ""
        if src and not _is_cloudflare_block(src):
            return src[:20000]
        # Browser got blocked — try Exa as fallback
        exa_text = exa_fetch(url)
        if exa_text:
            return exa_text
        return ""
    except Exception:
        # Network/timeout — try Exa as fallback
        try:
            return exa_fetch(url)
        except Exception:
            return ""


def clean_text(html: str) -> str:
    html = re.sub(r'<(script|style|noscript)[^>]*>.*?</\1>', ' ',
                  html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', html)
    return re.sub(r'\s+', ' ', text)[:10000]


def main():
    rows    = list(csv.DictReader(open(ENRICHED)))
    targets = [r for r in rows if r.get("website")]
    print(f"Dealers with websites : {len(targets)}")

    cache = json.loads(Path(CACHE).read_text()) if Path(CACHE).exists() else {}
    todo  = [r for r in targets if r["dealer_name"] not in cache]
    print(f"Cache: {len(cache)} done | Remaining: {len(todo)}")

    if todo:
        try:
            from seleniumbase import SB
        except ImportError:
            print("ERROR: pip install seleniumbase")
            return

        print("\nOpening Chrome (UC headless)...\n")
        high = medium = low = not_found = 0

        with SB(uc=True, headless=True) as sb:
            for i, row in enumerate(todo, 1):
                name    = row["dealer_name"]
                website = row["website"].rstrip("/")
                domain  = _domain(website)

                print(f"[{i}/{len(todo)}] {name[:48]}", end="  ", flush=True)

                best_contact = None
                best_jsonld  = {}

                for path in CONTACT_PATHS:
                    url  = website + path
                    html = fetch_text(sb, url)
                    if not html:
                        continue

                    # JSON-LD is most trustworthy — check first
                    if not best_jsonld.get("name"):
                        best_jsonld = extract_jsonld_person(html)

                    text    = clean_text(html)
                    contact = find_person_contact(text)

                    if contact:
                        if best_contact is None or contact["_priority"] < best_contact["_priority"]:
                            best_contact = contact
                        # Owner + email = done, no need to check more pages
                        if contact["_priority"] == 0 and contact["contact_email"]:
                            break

                    # If we found owner via JSON-LD, stop
                    if best_jsonld.get("name") and any(
                        t in best_jsonld.get("title", "").lower() for t in ALL_TITLES
                    ):
                        break

                # JSON-LD overrides page-text if it found a matching person
                if best_jsonld.get("name") and any(
                    t in best_jsonld.get("title", "").lower() for t in ALL_TITLES
                ):
                    jl = best_jsonld
                    email = jl.get("email", "")
                    phone = jl.get("phone", "")
                    # If JSON-LD has no email/phone, pull from page-text contact
                    if best_contact and not email:
                        email = best_contact.get("contact_email", "")
                    if best_contact and not phone:
                        phone = best_contact.get("contact_phone", "")
                    best_contact = {
                        "contact_name":   jl["name"],
                        "contact_title":  jl.get("title", ""),
                        "contact_email":  email,
                        "contact_phone":  phone,
                        "contact_source": "web_jsonld",
                        "verified_level": "high" if email else "medium",
                        "_priority":      0,
                    }

                # Fallback: no named person found → store contact only
                if not best_contact:
                    # Re-fetch homepage for fallback
                    html  = fetch_text(sb, website)
                    text  = clean_text(html) if html else ""
                    if text:
                        best_contact = find_store_contact_fallback(text, domain)

                if best_contact:
                    lvl = best_contact["verified_level"]
                    nm  = best_contact["contact_name"] or "(no name)"
                    em  = best_contact["contact_email"] or "(no email)"
                    ph  = best_contact["contact_phone"] or "(no phone)"
                    symbol = "✓✓" if lvl == "high" else ("~" if lvl == "medium" else "?")
                    print(f"{symbol} {nm} | {em} | {ph}")
                    data = {"found": True, "dealer_name": name,
                            "website": website, **best_contact}
                    if lvl == "high":   high   += 1
                    elif lvl == "medium": medium += 1
                    else:               low    += 1
                else:
                    print("—")
                    data = {"found": False, "dealer_name": name, "website": website}
                    not_found += 1

                cache[name] = data
                Path(CACHE).write_text(json.dumps(cache, indent=2))
                time.sleep(random.uniform(0.8, 1.5))

        total = high + medium + low
        print(f"\nPhase 1 results ({len(todo)} processed):")
        print(f"  High   (name+title+email) : {high}")
        print(f"  Medium (name+title only)  : {medium}  → needs email via Apollo/Vibe")
        print(f"  Low    (store contact)    : {low}   → needs owner name via Apollo/Exa")
        print(f"  Not found                 : {not_found}")

    _write_contacts(cache, rows)


def _write_contacts(cache: dict, all_rows: list):
    FIELDS = [
        "dealer_name", "dealer_city", "dealer_state", "dealer_website",
        "contact_name", "contact_title", "contact_email", "contact_phone",
        "contact_source", "verified_level",
    ]
    row_map  = {r["dealer_name"]: r for r in all_rows}
    contacts = []

    for name, data in cache.items():
        if not data.get("found"):
            continue
        dealer = row_map.get(name, {})
        contacts.append({
            "dealer_name":    name,
            "dealer_city":    dealer.get("location_city", ""),
            "dealer_state":   dealer.get("location_state", ""),
            "dealer_website": data.get("website", ""),
            "contact_name":   data.get("contact_name", ""),
            "contact_title":  data.get("contact_title", ""),
            "contact_email":  data.get("contact_email", ""),
            "contact_phone":  data.get("contact_phone", ""),
            "contact_source": data.get("contact_source", ""),
            "verified_level": data.get("verified_level", ""),
        })

    with open(CONTACTS_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for c in contacts:
            w.writerow(c)

    with_name  = sum(1 for c in contacts if c["contact_name"])
    with_email = sum(1 for c in contacts if c["contact_email"])
    with_phone = sum(1 for c in contacts if c["contact_phone"])
    high       = sum(1 for c in contacts if c["verified_level"] == "high")
    medium     = sum(1 for c in contacts if c["verified_level"] == "medium")
    low        = sum(1 for c in contacts if c["verified_level"] == "low")

    print(f"\n{'='*50}")
    print(f"contacts.csv  —  {len(contacts)} contacts saved")
    print(f"  Named owner/manager : {with_name}  (high={high}, medium={medium})")
    print(f"  Have email          : {with_email}")
    print(f"  Have phone          : {with_phone}")
    print(f"  Store-only (low)    : {low}")
    print(f"\nNext: python3 apollo_contacts.py")


if __name__ == "__main__":
    main()
