#!/usr/bin/env python3
"""
Split 356 guns.com dealers into:
  - personal_emails.csv   : dealers with a personal (non-generic) email
  - generic_emails.csv    : dealers with only a generic email
  - Appends 4 no-contact dealers to Mannual Review Dealers.csv
"""

import csv, json, re
from pathlib import Path
from collections import Counter

BASE = Path(__file__).resolve().parent

GENERIC_PREFIXES = [
    "info@","sales@","contact@","support@","admin@","mail@","hello@","store@","shop@",
    "service@","office@","customerservice@","contactus@","orders@","events@",
    "questions@","online@","feedback@","webmaster@","noreply@","no-reply@","donotreply@",
    # Gun-industry role addresses
    "gunsmith@","ffl@","transfers@","transfer@","firearms@","guns@","ammo@","range@",
    "shooting@","billing@","accounting@","hr@","jobs@","careers@","press@","media@",
    "help@","inquiry@","inquiries@","quote@","returns@","warranty@","shipping@",
    "wholesale@","dealer@","dispatch@","fulfillment@","team@","staff@","crew@",
]
NOISE_DOMAINS = {"gmail.com","yahoo.com","hotmail.com","outlook.com","icloud.com",
                 "example.com","yourdomain.com","domain.com","wiza.co","hunter.io","apollo.io"}

EMAIL_ORDER = ["vibe","exa_domain","tavily","serper","exa_wf","web"]
PHONE_ORDER = ["serper","tavily","exa_domain","exa_rerun","exa_wf","web","vibe"]
NAME_ORDER  = ["vibe","apollo","exa_domain","exa_wf","web"]

def is_generic(e):
    return any(e.lower().startswith(p) for p in GENERIC_PREFIXES) if e else False

def is_valid_email(e):
    if not e or "@" not in e: return False
    dom = e.split("@")[-1]
    return "." in dom and dom.lower() not in NOISE_DOMAINS

def is_valid_phone(p):
    return len(re.sub(r"[^\d]", "", p or "")) >= 10

def pick_email(caches, name):
    found = {}
    for src in EMAIL_ORDER:
        e = caches.get(src, {}).get(name, {}).get("contact_email", "").strip()
        if e and is_valid_email(e):
            found[src] = e
    if not found:
        return "", "", False
    agreement = Counter(found.values())
    for email, count in agreement.most_common():
        if count >= 2 and not is_generic(email):
            srcs = [s for s, e in found.items() if e == email]
            return email, "+".join(srcs), False
    for src in EMAIL_ORDER:
        e = found.get(src, "")
        if e and not is_generic(e):
            return e, src, False
    for email, count in agreement.most_common():
        if count >= 2:
            srcs = [s for s, e in found.items() if e == email]
            return email, "+".join(srcs), True
    for src in EMAIL_ORDER:
        e = found.get(src, "")
        if e:
            return e, src, True
    return "", "", False

def pick_phone(caches, name):
    for src in PHONE_ORDER:
        p = caches.get(src, {}).get(name, {}).get("contact_phone", "").strip()
        if p and is_valid_phone(p):
            return re.sub(r"[^\d+]", "", p), src
    return "", ""

def pick_name(caches, name):
    for src in NAME_ORDER:
        n = caches.get(src, {}).get(name, {}).get("contact_name", "").strip()
        if n and n.lower() not in ("none none", "none", ""):
            t = caches.get(src, {}).get(name, {}).get("contact_title", "").strip()
            return n, t, src
    return "", "", ""

def main():
    with open(BASE / "dealers_enriched.csv") as f:
        dealers = list(csv.DictReader(f))

    caches = {}
    for src, path in [
        ("vibe",       BASE / "cache/vibe_domain_cache.json"),
        ("exa_domain", BASE / "cache/exa_domain_cache.json"),
        ("serper",     BASE / "cache/serper_cache.json"),
        ("tavily",     BASE / "cache/tavily_cache.json"),
        ("exa_rerun",  BASE / "cache/exa_rerun_cache.json"),
        ("exa_wf",     BASE / "cache/exa_web_fetch_cache.json"),
        ("web",        BASE / "cache/contacts_web_cache.json"),
        ("apollo",     BASE / "cache/apollo_contacts_cache.json"),
    ]:
        caches[src] = json.loads(path.read_text()) if path.exists() else {}

    out_fields = [
        "dealer_name", "website", "total_listings", "new_listings", "used_listings",
        "contact_name", "contact_title", "contact_email", "contact_phone",
        "email_source", "phone_source", "name_source",
    ]

    personal_rows = []
    generic_rows  = []
    no_contact    = []

    for d in dealers:
        name = d["dealer_name"]
        email, email_src, generic = pick_email(caches, name)
        phone, phone_src          = pick_phone(caches, name)
        cname, title, name_src    = pick_name(caches, name)

        row = {
            "dealer_name":   name,
            "website":       d.get("website", ""),
            "total_listings": d.get("total_listings", ""),
            "new_listings":   d.get("new_listings", ""),
            "used_listings":  d.get("used_listings", ""),
            "contact_name":   cname,
            "contact_title":  title,
            "contact_email":  email,
            "contact_phone":  phone,
            "email_source":   email_src,
            "phone_source":   phone_src,
            "name_source":    name_src,
        }

        if email and not generic:
            personal_rows.append(row)
        elif email and generic:
            generic_rows.append(row)
        elif not email and not phone:
            no_contact.append(d)

    # Sort by listings descending
    personal_rows.sort(key=lambda r: int(r["total_listings"] or 0), reverse=True)
    generic_rows.sort(key=lambda r: int(r["total_listings"] or 0), reverse=True)

    # Write personal_emails.csv
    with open(BASE / "personal_emails.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(personal_rows)

    # Write generic_emails.csv
    with open(BASE / "generic_emails.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(generic_rows)

    # Append 4 no-contact dealers to Mannual Review Dealers.csv
    manual_path = BASE / "Mannual Review Dealers.csv"
    existing = list(csv.DictReader(open(manual_path, encoding="utf-8-sig")))
    existing_names = {r["dealer_name"] for r in existing}
    manual_fields = list(existing[0].keys())

    new_manual = []
    for d in no_contact:
        if d["dealer_name"] not in existing_names:
            new_manual.append({
                "dealer_name":        d["dealer_name"],
                "guns_com_profile_url": d.get("guns_com_profile_url", ""),
                "location_city":      d.get("location_city", ""),
                "location_state":     d.get("location_state", ""),
                "phone":              d.get("guns_com_phone", ""),
                "email":              d.get("guns_com_email", ""),
                "website":            d.get("website", ""),
                "total_listings":     d.get("total_listings", ""),
                "new_listings":       d.get("new_listings", ""),
                "used_listings":      d.get("used_listings", ""),
                "location_count":     d.get("location_count", ""),
                "merged_from":        "no_contact",
            })

    with open(manual_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=manual_fields)
        w.writerows(new_manual)

    print(f"personal_emails.csv : {len(personal_rows)} dealers")
    print(f"generic_emails.csv  : {len(generic_rows)} dealers")
    print(f"Added to manual review: {len(new_manual)} dealers")
    print()
    print("No-contact dealers added to manual review:")
    for d in no_contact:
        print(f"  {d['dealer_name']} ({d.get('total_listings','?')} listings)")

if __name__ == "__main__":
    main()
