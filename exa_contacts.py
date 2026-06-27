#!/usr/bin/env python3
"""
Phase 3 — Contact Enrichment: Exa neural search for owner/manager names.

For dealers still missing a named contact after Phase 1 (website) + Phase 2 (Apollo),
searches Exa for "[dealer name] [city] owner" style queries.

Exa's neural search finds:
  - LinkedIn profiles  ("John Smith — Owner at XYZ Guns")
  - Local news quotes  ("John Smith, owner of XYZ Guns, said...")
  - BBB profiles       (often list owner name)
  - Chamber listings   (sometimes include owner)
  - Facebook About pages

Extracted names are saved with verified_level="medium" (name+title, no email yet)
so the final merge can keep them in contacts_pending_enrichment.csv when no
email or phone is available.

Cache:   cache/exa_contacts_cache.json
Output:  contacts.csv  (merged)

Run: python3 exa_contacts.py
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
from dotenv import load_dotenv

load_dotenv()

ENRICHED     = "dealers_enriched.csv"
CONTACTS_OUT = "contacts.csv"
CACHE        = "cache/exa_contacts_cache.json"
EXA_SEARCH   = "https://api.exa.ai/search"
EXA_CONTENTS = "https://api.exa.ai/contents"

TARGET_TITLES = [
    "owner", "co-owner", "proprietor", "founder", "co-founder",
    "president", "ceo", "chief executive",
    "general manager", "gm", "operations manager",
    "director of operations", "operations director",
    "store manager", "shop manager", "manager",
]

NOT_NAMES = {
    "the", "our", "your", "this", "that", "with", "from", "about",
    "meet", "contact", "welcome", "store", "shop", "gun", "pawn",
    "email", "phone", "fax", "call", "send", "visit", "click",
    "home", "new", "used", "buy", "sell", "trade", "please",
    "monday", "tuesday", "wednesday", "thursday", "friday",
    "saturday", "sunday", "open", "closed", "hours", "time",
    "north", "south", "east", "west", "street", "avenue", "road",
    "just", "also", "more", "most", "some", "have", "will",
    "been", "they", "them", "their", "what", "when", "where",
    # CSS / typography / web-design terms that can look like "First Last"
    "font", "sans", "serif", "bold", "italic", "light", "medium",
    "regular", "black", "condensed", "extended", "narrow", "wide",
    "reserved", "heading", "display", "body", "weight", "style",
    "color", "size", "family", "face", "type", "text", "line",
    "web", "mono", "neue", "pro", "thin", "semibold", "heavy",
    "ultra", "extra", "variable", "rounded", "oblique", "alternate",
    "block", "inline", "flex", "grid", "row", "col", "span",
    "nav", "bar", "hero", "banner", "footer", "header", "sidebar",
    "lorem", "ipsum", "dolor", "amet", "read", "more", "learn",
    "submit", "cancel", "close", "view", "show", "hide", "edit",
    # Firearms/retail generic words
    "ammo", "ammunition", "firearm", "firearms", "rifle", "pistol",
    "shotgun", "handgun", "shooting", "range", "hunting", "tactical",
    "price", "sale", "deal", "special", "offer", "discount", "free",
    "shipping", "service", "repair",
    # Layout/UI
    "page", "section", "content", "main", "area", "top", "bottom",
    "center", "middle", "side", "base", "full", "half",
    # Numbers (word form)
    "one", "two", "three", "four", "five", "six", "seven", "eight",
    "nine", "ten", "eleven", "twelve", "second", "third", "fourth",
    "fifth", "sixth", "seventh", "eighth", "ninth", "tenth",
    # Month names (appear in "Updated June", "Posted January", etc.)
    "january", "february", "march", "april", "june", "july", "august",
    "september", "october", "november", "december",
    # Action/status words that get confused for names
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
    "mega", "super", "ultra", "plus", "premier", "elite", "pro",
    "national", "regional", "local", "global", "american", "united",
    "federal", "state", "county", "city", "valley", "mountain",
    "river", "lake", "creek", "ridge", "hills", "plains", "coast",
    "indoor", "outlet", "center", "market", "exchange", "trading",
    "pawnbrokers", "jewelry", "jewelers", "auction",
}

_NAME_RE  = re.compile(r'\b([A-Z][a-z]{1,20})\s+([A-Z][a-z]{1,20})\b')
_EMAIL_RE = re.compile(r'\b([a-zA-Z0-9._%+\-]+)@([a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})\b')
_PHONE_RE = re.compile(r'\b(?:\+1[-.\s]?)?\(?(\d{3})\)?[-.\s](\d{3})[-.\s](\d{4})\b')

FIELDS = [
    "dealer_name", "dealer_city", "dealer_state", "dealer_website",
    "contact_name", "contact_title", "contact_email", "contact_phone",
    "contact_source", "verified_level",
]


def _get_exa_key() -> str:
    return os.getenv("EXA_API_KEY", "")


def _exa_request(endpoint: str, payload: dict, api_key: str) -> dict:
    data = json.dumps(payload).encode("utf-8")
    req  = urllib.request.Request(
        endpoint, data=data,
        headers={"Content-Type": "application/json", "x-api-key": api_key},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        if e.code in (402, 429):
            raise RuntimeError(f"Exa credits exhausted (HTTP {e.code})")
        body = e.read().decode("utf-8", errors="ignore")[:200]
        raise RuntimeError(f"Exa HTTP {e.code}: {body}")


def exa_search_owner(name: str, city: str, state: str, api_key: str) -> list:
    """Run two query variants and return list of result snippets + URLs."""
    loc      = f"{city} {state}".strip()
    queries  = [
        f'"{name}" {loc} owner',
        f'"{name}" {loc} founder manager',
    ]
    results  = []
    for query in queries:
        try:
            resp = _exa_request(
                EXA_SEARCH,
                {"query": query, "numResults": 5, "type": "neural",
                 "useAutoprompt": True},
                api_key,
            )
            for r in resp.get("results", []):
                results.append({
                    "url":     r.get("url", ""),
                    "title":   r.get("title", ""),
                    "snippet": r.get("snippet") or r.get("text", ""),
                })
        except RuntimeError:
            raise
        except Exception as e:
            print(f"    [search error] {e}")
        time.sleep(random.uniform(0.3, 0.7))
    return results


def exa_fetch_page(url: str, api_key: str) -> str:
    """Fetch a URL via Exa (bypasses Cloudflare). Returns page text."""
    try:
        resp = _exa_request(
            EXA_CONTENTS,
            {"ids": [url], "text": True},
            api_key,
        )
        results = resp.get("results", [])
        if results:
            title = results[0].get("title", "")
            text  = results[0].get("text", "")[:5000]
            return f"{title} {text}"
    except Exception as e:
        print(f"    [fetch error] {e}")
    return ""


def _is_business_name_echo(candidate: str, dealer_name: str) -> bool:
    """True if the candidate 'name' is just the dealer's business name repeated."""
    c_norm = re.sub(r"[^a-z ]", "", candidate.lower()).strip()
    d_norm = re.sub(r"[^a-z ]", "", dealer_name.lower()).strip()
    c_tokens = set(c_norm.split())
    d_tokens = set(d_norm.split())
    # Reject if every word in the candidate appears in the dealer name
    return bool(c_tokens) and c_tokens.issubset(d_tokens)


def extract_name_near_title(text: str, dealer_name: str = "") -> tuple[str, str]:
    """
    Scan text for a person name adjacent to an owner/manager title.
    Returns (name, title) or ("", "").
    """
    lower = text.lower()
    for title in TARGET_TITLES:
        for m in re.finditer(re.escape(title), lower):
            pos     = m.start()
            w_start = max(0, pos - 200)
            w_end   = min(len(text), pos + 200)
            window  = text[w_start:w_end]
            for nm in _NAME_RE.finditer(window):
                first = nm.group(1).lower()
                last  = nm.group(2).lower()
                if first in NOT_NAMES or last in NOT_NAMES:
                    continue
                if len(first) < 2 or len(last) < 2:
                    continue
                candidate = f"{nm.group(1)} {nm.group(2)}"
                if dealer_name and _is_business_name_echo(candidate, dealer_name):
                    continue
                return candidate, title
    return "", ""


def extract_email_phone_near_name(text: str, person_name: str) -> tuple[str, str]:
    """Look for email and phone within 400 chars of the person's name in text."""
    pos = text.lower().find(person_name.lower())
    if pos == -1:
        return "", ""
    c_start = max(0, pos - 300)
    c_end   = min(len(text), pos + 300)
    context = text[c_start:c_end]

    emails = [f"{m.group(1)}@{m.group(2)}".lower() for m in _EMAIL_RE.finditer(context)]
    phones = [f"({m.group(1)}) {m.group(2)}-{m.group(3)}" for m in _PHONE_RE.finditer(context)]

    return (emails[0] if emails else ""), (phones[0] if phones else "")


def _load_contacts() -> dict:
    if not Path(CONTACTS_OUT).exists():
        return {}
    return {r["dealer_name"]: r for r in csv.DictReader(open(CONTACTS_OUT))}


def _write_contacts(contacts: dict, row_map: dict):
    with open(CONTACTS_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader()
        for name, c in contacts.items():
            dealer = row_map.get(name, {})
            w.writerow({
                "dealer_name":    name,
                "dealer_city":    c.get("dealer_city") or dealer.get("location_city", ""),
                "dealer_state":   c.get("dealer_state") or dealer.get("location_state", ""),
                "dealer_website": c.get("dealer_website") or dealer.get("website", ""),
                "contact_name":   c.get("contact_name", ""),
                "contact_title":  c.get("contact_title", ""),
                "contact_email":  c.get("contact_email", ""),
                "contact_phone":  c.get("contact_phone", ""),
                "contact_source": c.get("contact_source", ""),
                "verified_level": c.get("verified_level", ""),
            })


def main():
    dealers  = list(csv.DictReader(open(ENRICHED)))
    row_map  = {r["dealer_name"]: r for r in dealers}
    contacts = _load_contacts()

    # Run on ALL dealers — Exa independently verifies or fills gaps from Phases 1+2.
    targets = dealers
    print(f"Dealers needing Exa enrichment: {len(targets)}")

    cache = json.loads(Path(CACHE).read_text()) if Path(CACHE).exists() else {}
    todo  = [r for r in targets if r["dealer_name"] not in cache]
    print(f"Cache: {len(cache)} done | Remaining: {len(todo)}")

    if not todo:
        print("Nothing to do — applying cache to contacts.csv")
    else:
        api_key = _get_exa_key()
        if not api_key:
            print("No Exa key — exiting")
            return

        print(f"\nRunning Exa search for {len(todo)} dealers...\n")
        found = not_found = 0

        for i, row in enumerate(todo, 1):
            name  = row["dealer_name"]
            city  = row.get("location_city", "")
            state = row.get("location_state", "")

            print(f"[{i}/{len(todo)}] {name[:50]}", end="  ", flush=True)

            try:
                results = exa_search_owner(name, city, state, api_key)
            except RuntimeError as e:
                print(f"\n\nExa credits exhausted: {e}")
                print("Save progress and exit.")
                Path(CACHE).write_text(json.dumps(cache, indent=2))
                break

            found_name = found_title = found_email = found_phone = ""

            for r in results:
                # First try the snippet (fast, no extra credit cost)
                text = (r["title"] + " " + r["snippet"]).strip()
                n, t = extract_name_near_title(text, name)
                if n:
                    found_name, found_title = n, t
                    em, ph = extract_email_phone_near_name(text, n)
                    found_email, found_phone = em, ph
                    # If we got a name from snippet, fetch the full page for email/phone
                    if not found_email and not found_phone and r["url"]:
                        full_text = exa_fetch_page(r["url"], api_key)
                        if full_text:
                            em, ph = extract_email_phone_near_name(full_text, n)
                            found_email = em or found_email
                            found_phone = ph or found_phone
                    break

            if found_name:
                lvl = "high" if found_email else "medium"
                print(f"✓ [{lvl}] {found_name} | {found_email or '(no email)'} | {found_phone or '(no phone)'}")
                data = {
                    "contact_name":   found_name,
                    "contact_title":  found_title,
                    "contact_email":  found_email,
                    "contact_phone":  found_phone,
                    "contact_source": "exa",
                    "verified_level": lvl,
                }
                cache[name] = {"found": True, **data}
                contacts[name] = {
                    **contacts.get(name, {}),
                    "dealer_city":  city,
                    "dealer_state": state,
                    "dealer_website": row.get("website", ""),
                    **data,
                }
                found += 1
            else:
                print("—")
                cache[name] = {"found": False}
                not_found += 1

            Path(CACHE).write_text(json.dumps(cache, indent=2))
            time.sleep(random.uniform(0.8, 1.5))

        print(f"\nExa results ({len(todo)} processed):")
        print(f"  Found  : {found}")
        print(f"  Not found : {not_found}")

    # Apply any cached results not yet in contacts
    for name, data in cache.items():
        if data.get("found") and name not in contacts:
            dealer = row_map.get(name, {})
            contacts[name] = {
                "dealer_city":    dealer.get("location_city", ""),
                "dealer_state":   dealer.get("location_state", ""),
                "dealer_website": dealer.get("website", ""),
                "contact_name":   data.get("contact_name", ""),
                "contact_title":  data.get("contact_title", ""),
                "contact_email":  data.get("contact_email", ""),
                "contact_phone":  data.get("contact_phone", ""),
                "contact_source": data.get("contact_source", ""),
                "verified_level": data.get("verified_level", ""),
            }

    _write_contacts(contacts, row_map)

    # Final summary
    named  = sum(1 for c in contacts.values() if c.get("contact_name"))
    emailed = sum(1 for c in contacts.values() if c.get("contact_email"))
    phoned  = sum(1 for c in contacts.values() if c.get("contact_phone"))
    high    = sum(1 for c in contacts.values() if c.get("verified_level") == "high")
    medium  = sum(1 for c in contacts.values() if c.get("verified_level") == "medium")
    low     = sum(1 for c in contacts.values() if c.get("verified_level") == "low")

    print(f"\n{'='*55}")
    print(f"contacts.csv after Phases 1–3  —  {len(contacts)} contacts")
    print(f"  Named owner/manager : {named}/{len(dealers)}")
    print(f"  Have email          : {emailed}/{len(dealers)}")
    print(f"  Have phone          : {phoned}/{len(dealers)}")
    print(f"  High confidence     : {high}")
    print(f"  Medium (name only)  : {medium}  ← pending email/phone enrichment")
    print(f"  Low (store contact) : {low}")
    print(f"\nContact enrichment complete. Next: python3 merge_contacts.py")


if __name__ == "__main__":
    main()
