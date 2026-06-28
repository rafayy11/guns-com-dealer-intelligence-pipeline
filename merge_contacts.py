#!/usr/bin/env python3
"""
Merge all enrichment caches into a quality-filtered contacts.csv.

Sources (priority order):
  1. apollo_contacts_cache — Apollo people/contact enrichment
  2. exa_contacts_cache    — Exa owner/manager web search
  3. contacts_web_cache    — direct dealer website scraping
  4. serper_cache           — optional search enrichment
  5. tavily_cache           — optional search enrichment

Filters applied:
  - Foreign email domains (.com.au, .co.uk, .ca, etc.) that don't match dealer website
  - Known false positive contacts (association VPs, university staff)
  - Flags low-level roles (social media, accounts payable, sales associate)

Run: python3 merge_contacts.py
"""

import csv
import json
from pathlib import Path

ENRICHED     = "dealers_enriched.csv"
CONTACTS_OUT = "contacts.csv"

FIELDS = [
    "dealer_name", "dealer_city", "dealer_state", "dealer_website",
    "total_listings", "new_listings", "used_listings",
    "contact_name", "contact_title", "contact_email", "contact_phone",
    "contact_linkedin", "contact_source", "verified_level", "sources_agreed",
    "quality_flag",
]

FOREIGN_TLD = [".com.au", ".co.uk", ".co.nz", ".co.za", ".com.sg",
               ".org.au", ".net.au", ".ca", ".co.in"]

LOW_LEVEL_TITLES = [
    "social media", "accounts payable", "receptionist", "secretary",
    "sales associate", "cashier", "customer service", "teller",
    "warehouse employee", "warehouse worker", "shipping employee",
]

FALSE_POSITIVE_NAMES = {
    "puerto rico", "gold expands", "bobby bones", "many ways",
    "in cart", "add to cart", "view all", "read more", "learn more",
    "need assistance", "online manager", "contact us", "sign in",
    "get started", "submit", "click here", "tap here", "loading",
    "twitter google", "it training", "facebook instagram", "google maps",
    "email phone", "call us", "visit us", "find us",
    "hi john", "assistant technical ballistician", "contacts daniel",
    "texas facility", "sandy chisholm", "hi there",
}

FALSE_POSITIVE_CONTACTS = [
    ("lorraine thomas", "hcf.com.au", "Australian company"),
    ("lisa little", "nationalpawnbrokers.org", "Matched association HQ not dealer"),
    ("monsica collins", None, "American military university — wrong company"),
    ("joshua hunt", None, "City council president — wrong company"),
    ("online manager", None, "Generic web form label"),
    ("robert (rob) kamphausen", "fandom.com", "Fandom.com corporate — matched wiki page"),
    ("brett schriock", "madison-co.com", "County government, not a gun dealer"),
    ("robert lopez", "abilenevisitors.com", "Visitors bureau, not a gun dealer"),
    ("chelsea benson", "cityofchelsea.com", "City government, not a gun dealer"),
    ("patrick young", "statesboroherald.com", "Newspaper reporter, not a gun dealer"),
]

# Title phrases that indicate a wrong-company business match
FALSE_POSITIVE_TITLE_PHRASES = [
    "city council", "american military university", "university",
    "az concealed carry weapons inst", "church pastor", "nonprofit",
    "visitors bureau", "convention center",
]

# Email domains that are clearly platform/directory sites (not real dealer websites)
PLATFORM_DOMAINS = ["fandom.com", "yelp.com", "facebook.com", "google.com",
                    "yellowpages.com", "bbb.org", "manta.com",
                    "statesboroherald.com", "cityofchelsea.com",
                    "madison-co.com", "abilenevisitors.com"]

GENERIC_PREFIXES = ["info@", "sales@", "contact@", "support@", "admin@",
                    "mail@", "hello@", "store@", "shop@", "service@", "office@",
                    "customerservice@", "contactus@", "orders@", "events@",
                    "questions@", "online@", "feedback@", "webmaster@",
                    "noreply@", "no-reply@", "donotreply@"]


def load_cache(path: str) -> dict:
    p = Path(path)
    return json.loads(p.read_text()) if p.exists() else {}


def is_foreign_email(email: str, dealer_website: str) -> bool:
    if not email or "@" not in email:
        return False
    domain = email.split("@")[-1].lower()
    for tld in FOREIGN_TLD:
        if domain.endswith(tld):
            if dealer_website and domain in dealer_website.lower():
                return False
            return True
    return False


def quality_flag(name: str, title: str, email: str, dealer_website: str) -> str:
    name_l  = (name or "").lower().strip()
    title_l = (title or "").lower()
    email_l = (email or "").lower()

    if name_l in FALSE_POSITIVE_NAMES:
        return "false_positive: non-person name"

    for fp_name, fp_domain, reason in FALSE_POSITIVE_CONTACTS:
        if fp_name in name_l:
            if fp_domain is None or (fp_domain and fp_domain in email_l):
                return f"false_positive: {reason}"

    if is_foreign_email(email, dealer_website):
        return f"false_positive: foreign email ({email.split('@')[-1]})"

    if email and any(email_l.startswith(p) for p in GENERIC_PREFIXES):
        return "low_level: generic store email"

    for low in LOW_LEVEL_TITLES:
        if low in title_l:
            return f"low_level: {title}"

    for fp_title in FALSE_POSITIVE_TITLE_PHRASES:
        if fp_title in title_l:
            return f"false_positive: wrong company ({title})"

    # Email from a known platform/directory domain
    if email:
        ed = email.split("@")[-1].lower()
        for pd in PLATFORM_DOMAINS:
            if ed == pd or ed.endswith("." + pd):
                return f"false_positive: platform domain ({ed})"

    return "good"


def main():
    if not Path(ENRICHED).exists():
        print(f"ERROR: {ENRICHED} not found. Run the scraper and website enrichment first.")
        return

    with open(ENRICHED, newline="", encoding="utf-8") as f:
        dealers_map = {r["dealer_name"]: r for r in csv.DictReader(f)}

    apollo = load_cache("cache/apollo_contacts_cache.json")
    exa    = load_cache("cache/exa_contacts_cache.json")
    web    = load_cache("cache/contacts_web_cache.json")
    serper = load_cache("cache/serper_cache.json")
    tavily = load_cache("cache/tavily_cache.json")

    rows = []
    stats = {"high": 0, "medium": 0, "low": 0,
             "false_positive": 0, "low_level": 0, "good": 0}

    def active(data):
        if not data or not data.get("found"):
            return {}
        has_contact = (
            data.get("contact_name") or
            data.get("contact_email") or
            data.get("contact_phone")
        )
        return data if has_contact else {}

    def first_value(candidates, key):
        for _, data in candidates:
            value = (data.get(key) or "").strip()
            if value:
                return value
        return ""

    for dealer, row in dealers_map.items():
        website = row.get("website", "")

        candidates = [
            ("apollo", active(apollo.get(dealer, {}))),
            ("exa", active(exa.get(dealer, {}))),
            ("web", active(web.get(dealer, {}))),
            ("serper", active(serper.get(dealer, {}))),
            ("tavily", active(tavily.get(dealer, {}))),
        ]

        name     = first_value(candidates, "contact_name")
        title    = first_value(candidates, "contact_title")
        email    = first_value(candidates, "contact_email")
        phone    = first_value(candidates, "contact_phone")
        linkedin = first_value(candidates, "contact_linkedin")

        if not name and not email and not phone:
            continue

        sources = []
        for label, data in candidates:
            if data:
                sources.append(data.get("contact_source") or label)
        source = "+".join(sources) or "enriched"

        agreement_values = []
        if email:
            agreement_values = [
                label for label, data in candidates
                if (data.get("contact_email") or "").lower().strip() == email.lower().strip()
            ]
        elif name:
            agreement_values = [
                label for label, data in candidates
                if (data.get("contact_name") or "").lower().strip() == name.lower().strip()
            ]

        flag = quality_flag(name, title, email, website)

        if name and email:   lvl = "high"
        elif name:           lvl = "medium"
        elif email or phone: lvl = "low"
        else:                continue

        rows.append({
            "dealer_name":    dealer,
            "dealer_city":    row.get("location_city", ""),
            "dealer_state":   row.get("location_state", ""),
            "dealer_website": website,
            "total_listings": row.get("total_listings", ""),
            "new_listings":   row.get("new_listings", ""),
            "used_listings":  row.get("used_listings", ""),
            "contact_name":   name,
            "contact_title":  title,
            "contact_email":  email,
            "contact_phone":  phone,
            "contact_linkedin": linkedin,
            "contact_source": source,
            "verified_level": lvl,
            "sources_agreed": str(len(agreement_values)) if agreement_values else "",
            "quality_flag":   flag,
        })

        stats[lvl] += 1
        if "false_positive" in flag:
            stats["false_positive"] += 1
        elif "low_level" in flag:
            stats["low_level"] += 1
        else:
            stats["good"] += 1

    # Remove false positives — write those to a separate audit file
    false_pos   = [r for r in rows if "false_positive" in r.get("quality_flag","")]
    clean       = [r for r in rows if "false_positive" not in r.get("quality_flag","")]

    # Split clean rows: has email/phone → contacts.csv  |  name-only → pending
    has_contact_raw = [r for r in clean if r.get("contact_email") or r.get("contact_phone")]
    pending         = [r for r in clean if not r.get("contact_email") and not r.get("contact_phone")]

    # Deduplicate by email: same person at multiple locations → keep row with phone,
    # otherwise keep first occurrence. People with no email are not deduplicated.
    seen_emails = {}
    has_contact = []
    for r in has_contact_raw:
        email = r.get("contact_email","").lower().strip()
        if not email:
            has_contact.append(r)  # no email — always keep (phone-only rows)
            continue
        if email not in seen_emails:
            seen_emails[email] = r
        else:
            # Keep the record with a phone number if current doesn't have one
            existing = seen_emails[email]
            if r.get("contact_phone") and not existing.get("contact_phone"):
                seen_emails[email] = r
    has_contact += list(seen_emails.values())

    with open(CONTACTS_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(has_contact)

    with open("contacts_pending_enrichment.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(pending)

    with open("contacts_excluded_false_positives.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDS)
        w.writeheader(); w.writerows(false_pos)

    good   = [r for r in has_contact if r["quality_flag"] == "good"]
    ll     = [r for r in has_contact if "low_level" in r["quality_flag"]]

    print(f"contacts.csv                         → {len(has_contact)} (clean, with email or phone)")
    print(f"contacts_pending_enrichment.csv      → {len(pending)} (name only, no contact info)")
    print(f"contacts_excluded_false_positives.csv→ {len(false_pos)} (wrong-company matches, excluded)")
    print()
    print(f"contacts.csv breakdown:")
    print(f"  High confidence (name+email)  : {sum(1 for r in has_contact if r['verified_level']=='high')}")
    print(f"  Medium (name+phone, no email) : {sum(1 for r in has_contact if r['verified_level']=='medium')}")
    print(f"  Low (phone/email, no name)    : {sum(1 for r in has_contact if r['verified_level']=='low')}")
    print(f"  Good decision-makers          : {len(good)}")
    print(f"  Low-level roles (flagged)     : {len(ll)}")
    print()
    hi = [r for r in good if r["verified_level"] == "high"]
    print(f"Decision-makers with verified email ({len(hi)}):")
    for r in hi:
        print(f"  {r['contact_name']:25s} | {r['contact_title'][:28]:28s} | {r['contact_email']:38s} | {r['contact_phone']}")


if __name__ == "__main__":
    main()
