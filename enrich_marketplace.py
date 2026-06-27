#!/usr/bin/env python3
"""
Enrich marketplace_presence.csv with contact data from all enrichment caches.

Priority (email):   personal > generic, multi-source agreement preferred
Priority (source):  vibe > exa_domain > tavily > serper > exa_wf > exa_rerun > web > apollo
Output defaults to enriched_contacts.csv in the repo root.
"""

import csv, json, os, re
from pathlib import Path
from collections import defaultdict

BASE = Path(__file__).resolve().parent
INPUT  = os.getenv("MARKETPLACE_PRESENCE_CSV", str(BASE / "marketplace_presence.csv"))
OUTPUT = os.getenv("ENRICHED_MARKETPLACE_CSV", str(BASE / "enriched_contacts.csv"))

GENERIC_PREFIXES = [
    "info@","sales@","contact@","support@","admin@","mail@","hello@",
    "store@","shop@","service@","office@","customerservice@","contactus@",
    "orders@","events@","questions@","online@","feedback@","webmaster@",
    "noreply@","no-reply@","donotreply@",
]

# Source priority for name/title
NAME_PRIORITY  = ["vibe","apollo","exa_domain","exa_wf","web"]
# Source priority for email: personal first pass, then generic fallback
EMAIL_PRIORITY = ["vibe","exa_domain","tavily","serper","exa_wf","web"]
PHONE_PRIORITY = ["serper","tavily","exa_domain","exa_rerun","exa_wf","web","vibe"]


def load_caches():
    base = BASE
    paths = {
        "vibe":      base / "cache/vibe_domain_cache.json",
        "exa_domain":base / "cache/exa_domain_cache.json",
        "serper":    base / "cache/serper_cache.json",
        "tavily":    base / "cache/tavily_cache.json",
        "exa_rerun": base / "cache/exa_rerun_cache.json",
        "exa_wf":    base / "cache/exa_web_fetch_cache.json",
        "web":       base / "cache/contacts_web_cache.json",
        "apollo":    base / "cache/apollo_contacts_cache.json",
    }
    return {k: json.loads(v.read_text()) if v.exists() else {} for k, v in paths.items()}


def is_generic(email):
    if not email: return False
    return any(email.lower().startswith(p) for p in GENERIC_PREFIXES)


def is_valid_email(email):
    if not email or "@" not in email: return False
    domain = email.split("@")[-1]
    if "." not in domain: return False
    noise = {"gmail.com","yahoo.com","hotmail.com","outlook.com","example.com",
             "yourdomain.com","domain.com","wiza.co","hunter.io","apollo.io"}
    return domain.lower() not in noise


def is_valid_phone(phone):
    digits = re.sub(r"[^\d]", "", phone or "")
    return len(digits) >= 10


def best_email(caches, name):
    """Return (email, source, is_generic) using priority + personal preference."""
    found = {}
    for src in EMAIL_PRIORITY:
        e = caches[src].get(name, {}).get("contact_email", "").strip()
        if e and is_valid_email(e):
            found[src] = e

    if not found:
        return "", "", False

    # Count how many sources agree on each email
    from collections import Counter
    agreement = Counter(found.values())

    # 1. Personal email agreed by 2+ sources
    for email, count in agreement.most_common():
        if count >= 2 and not is_generic(email):
            srcs = [s for s, e in found.items() if e == email]
            return email, "+".join(srcs), False

    # 2. Any personal email from priority order
    for src in EMAIL_PRIORITY:
        e = found.get(src, "")
        if e and not is_generic(e):
            return e, src, False

    # 3. Generic email agreed by 2+ sources
    for email, count in agreement.most_common():
        if count >= 2:
            srcs = [s for s, e in found.items() if e == email]
            return email, "+".join(srcs), True

    # 4. Fallback: any generic from priority order
    for src in EMAIL_PRIORITY:
        e = found.get(src, "")
        if e:
            return e, src, True

    return "", "", False


def best_phone(caches, name):
    for src in PHONE_PRIORITY:
        p = caches[src].get(name, {}).get("contact_phone", "").strip()
        if p and is_valid_phone(p):
            digits = re.sub(r"[^\d+]", "", p)
            return digits, src
    return "", ""


def best_name(caches, name):
    for src in NAME_PRIORITY:
        n = caches[src].get(name, {}).get("contact_name", "").strip()
        if n and n.lower() not in ("none none", "none", ""):
            t = caches[src].get(name, {}).get("contact_title", "").strip()
            return n, t, src
    return "", "", ""


def all_emails_str(caches, name):
    parts = []
    for src in EMAIL_PRIORITY:
        e = caches[src].get(name, {}).get("contact_email", "").strip()
        if e and is_valid_email(e):
            parts.append(f"{src}:{e}")
    return " | ".join(parts)


def main():
    caches = load_caches()
    dealers = list(csv.DictReader(open(INPUT, encoding="utf-8-sig")))
    print(f"Marketplace dealers : {len(dealers)}")

    out_fields = [
        "company_name", "best_domain",
        "total_listings", "guns_com", "gunbroker", "armslist",
        "marketplace_count", "estimated_hours",
        "contact_name", "title",
        "contact_email", "contact_phone",
        "email_source", "email_type",
        "phone_source", "name_source",
        "all_email_sources",
        "quality_flag",
    ]

    rows = []
    stats = {"email_personal": 0, "email_generic": 0, "phone_only": 0, "no_contact": 0}

    for d in dealers:
        name = d["company_name"].strip()

        email, email_src, generic = best_email(caches, name)
        phone, phone_src          = best_phone(caches, name)
        cname, title, name_src    = best_name(caches, name)
        all_srcs                  = all_emails_str(caches, name)

        if email and not generic:
            flag = "good"
            stats["email_personal"] += 1
        elif email and generic:
            flag = "generic_email"
            stats["email_generic"] += 1
        elif phone:
            flag = "phone_only"
            stats["phone_only"] += 1
        else:
            flag = "no_contact"
            stats["no_contact"] += 1

        rows.append({
            "company_name":      name,
            "best_domain":       d.get("best_domain", ""),
            "total_listings":    d.get("total_listings", ""),
            "guns_com":          d.get("guns_com", ""),
            "gunbroker":         d.get("gunbroker", ""),
            "armslist":          d.get("armslist", ""),
            "marketplace_count": d.get("marketplace_count", ""),
            "estimated_hours":   d.get("estimated_hours", ""),
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

    # Sort by total_listings descending
    rows.sort(key=lambda r: int(r["total_listings"] or 0), reverse=True)

    with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=out_fields)
        w.writeheader()
        w.writerows(rows)

    print(f"\nOutput: {OUTPUT}")
    print(f"\nBreakdown ({len(rows)} dealers):")
    print(f"  Personal email    : {stats['email_personal']}")
    print(f"  Generic email     : {stats['email_generic']}")
    print(f"  Phone only        : {stats['phone_only']}")
    print(f"  No contact        : {stats['no_contact']}")
    print(f"\nTotal reachable    : {stats['email_personal'] + stats['email_generic'] + stats['phone_only']}")

    # Show sample of best contacts
    good = [r for r in rows if r["quality_flag"] == "good"][:10]
    print(f"\nTop contacts (personal email, sorted by listings):")
    print(f"  {'Company':35s} {'Listings':8s} {'Email':38s} {'Phone'}")
    print(f"  {'-'*35} {'-'*8} {'-'*38} {'-'*14}")
    for r in good:
        print(f"  {r['company_name'][:35]:35s} {str(r['total_listings']):8s} {r['contact_email']:38s} {r['contact_phone']}")


if __name__ == "__main__":
    main()
