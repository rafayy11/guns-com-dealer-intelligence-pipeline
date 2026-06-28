#!/usr/bin/env python3
"""
Optional contact enrichment via Serper.dev and Tavily search APIs.

This is not required for the core pipeline. Use it after Apollo, Exa, and
website scraping if contacts.csv still has dealers missing email or phone.

Outputs:
  cache/serper_cache.json
  cache/tavily_cache.json

Run:
  python3 optional_search_enrich.py --provider both
  python3 optional_search_enrich.py --provider serper --limit 50
"""

import argparse
import csv
import json
import os
import re
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

load_dotenv()

ENRICHED = "dealers_enriched.csv"
CONTACTS = "contacts.csv"
CACHE_DIR = Path("cache")

SERPER_URL = "https://google.serper.dev/search"
TAVILY_URL = "https://api.tavily.com/search"

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
PHONE_RE = re.compile(r"(?:\+1[\s\-.]?)?\(?\d{3}\)?[\s\-.]?\d{3}[\s\-.]?\d{4}")
PHONE_NOISE = re.compile(r"^(?:0{7,}|1{7,}|\d{5,6}$)")

NOISE_EMAIL_DOMAINS = {
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com", "icloud.com",
    "aol.com", "msn.com", "live.com", "me.com", "mac.com",
    "wiza.co", "hunter.io", "apollo.io", "rocketreach.co", "clearbit.com",
    "example.com", "yourdomain.com", "domain.com", "email.com",
}

GENERIC_PREFIXES = (
    "info@", "sales@", "contact@", "support@", "admin@", "mail@", "hello@",
    "store@", "shop@", "service@", "office@", "customerservice@", "contactus@",
    "orders@", "events@", "questions@", "online@", "feedback@", "webmaster@",
    "noreply@", "no-reply@", "donotreply@",
)

PLATFORM_DOMAINS = {
    "fandom.com", "yelp.com", "facebook.com", "google.com", "yellowpages.com",
    "bbb.org", "manta.com", "tripadvisor.com", "hub.biz", "bizapedia.com",
    "gunbroker.com", "angieslist.com", "wheree.com", "chamberofcommerce.com",
}


class KeyPool:
    def __init__(self, prefix: str):
        self.prefix = prefix
        self.keys = self._load_keys(prefix)
        self.index = 0

    @staticmethod
    def _load_keys(prefix: str) -> list[str]:
        keys = []
        first = os.getenv(prefix, "").strip()
        if first:
            keys.append(first)
        for i in range(2, 11):
            key = os.getenv(f"{prefix}_{i}", "").strip()
            if key:
                keys.append(key)
        return keys

    def has_keys(self) -> bool:
        return bool(self.keys)

    def current(self) -> str:
        if not self.keys:
            raise RuntimeError(f"{self.prefix} is not set in .env")
        return self.keys[self.index]

    def rotate(self, reason: str) -> None:
        self.index += 1
        if self.index >= len(self.keys):
            raise RuntimeError(f"All {self.prefix} keys exhausted after {reason}")
        print(f"  [{self.prefix} rotated to key {self.index + 1}/{len(self.keys)}: {reason}]")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Optional Serper/Tavily contact enrichment")
    parser.add_argument("--provider", choices=["serper", "tavily", "both"], default="both")
    parser.add_argument("--limit", type=int, default=0, help="Maximum dealers per provider; 0 = all")
    return parser.parse_args()


def load_json(path: Path) -> dict:
    return json.loads(path.read_text()) if path.exists() else {}


def save_json(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


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
    if not domain or "." not in domain:
        return False
    return not any(domain == p or domain.endswith("." + p) for p in PLATFORM_DOMAINS)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or PHONE_NOISE.match(digits):
        return ""
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def extract_contacts(texts: list[str], domain: str) -> tuple[str, str]:
    domain_root = domain.split(".")[0] if domain else ""
    text = " ".join(t for t in texts if t)
    best_email = ""
    generic_email = ""

    for email in EMAIL_RE.findall(text):
        email = email.lower().strip(".,;:()[]{}")
        email_domain = email.split("@")[-1] if "@" in email else ""
        if not email_domain or email_domain in NOISE_EMAIL_DOMAINS:
            continue
        if domain_root and domain_root not in email_domain:
            continue
        if any(email.startswith(prefix) for prefix in GENERIC_PREFIXES):
            generic_email = generic_email or email
            continue
        best_email = email
        break

    if not best_email:
        best_email = generic_email

    best_phone = ""
    for match in PHONE_RE.findall(text):
        best_phone = normalize_phone(match)
        if best_phone:
            break

    return best_email, best_phone


def load_contacts_map() -> dict:
    if not Path(CONTACTS).exists():
        return {}
    with open(CONTACTS, newline="", encoding="utf-8") as f:
        return {row["dealer_name"]: row for row in csv.DictReader(f)}


def collect_targets(cache: dict, contacts_map: dict, limit: int) -> list[dict]:
    if not Path(ENRICHED).exists():
        raise RuntimeError(f"{ENRICHED} not found. Run website enrichment first.")

    with open(ENRICHED, newline="", encoding="utf-8") as f:
        dealers = list(csv.DictReader(f))

    targets = []
    for dealer in dealers:
        name = dealer["dealer_name"]
        if name in cache:
            continue
        contact = contacts_map.get(name, {})
        if contact.get("contact_email") and contact.get("contact_phone"):
            continue
        domain = extract_domain(dealer.get("website", ""))
        if not valid_domain(domain):
            continue
        targets.append({
            "dealer": name,
            "domain": domain,
            "listings": int(dealer.get("total_listings", "0") or 0),
        })

    targets.sort(key=lambda row: row["listings"], reverse=True)
    return targets[:limit] if limit else targets


def serper_request(query: str, pool: KeyPool) -> dict:
    while True:
        response = requests.post(
            SERPER_URL,
            json={"q": query, "num": 10},
            headers={"X-API-KEY": pool.current(), "Content-Type": "application/json"},
            timeout=(5, 20),
        )
        if response.status_code in (401, 402, 403, 429):
            pool.rotate(f"Serper HTTP {response.status_code}")
            continue
        if response.status_code != 200:
            print(f"    [Serper HTTP {response.status_code}] {response.text[:100]}")
            return {}
        return response.json()


def tavily_request(query: str, pool: KeyPool) -> dict:
    while True:
        response = requests.post(
            TAVILY_URL,
            json={
                "query": query,
                "search_depth": "basic",
                "max_results": 5,
                "include_answer": True,
                "include_raw_content": True,
            },
            headers={"Authorization": f"Bearer {pool.current()}", "Content-Type": "application/json"},
            timeout=(5, 25),
        )
        if response.status_code in (401, 402, 403, 429):
            pool.rotate(f"Tavily HTTP {response.status_code}")
            continue
        if response.status_code != 200:
            print(f"    [Tavily HTTP {response.status_code}] {response.text[:100]}")
            return {}
        return response.json()


def search_with_serper(dealer: str, domain: str, pool: KeyPool) -> tuple[str, str]:
    queries = [
        f"site:{domain} email contact",
        f'"{dealer}" owner email phone contact',
        f"{domain} contact email phone",
    ]
    texts = []
    for query in queries:
        data = serper_request(query, pool)
        for row in data.get("organic", []):
            texts.extend([row.get("title", ""), row.get("snippet", "")])
            for sitelink in row.get("sitelinks", []):
                texts.extend([sitelink.get("title", ""), sitelink.get("snippet", "")])
        for row in data.get("peopleAlsoAsk", []):
            texts.append(row.get("snippet", ""))
        texts.append((data.get("knowledgeGraph", {}) or {}).get("description", ""))
        email, phone = extract_contacts(texts, domain)
        if email and phone:
            return email, phone
        time.sleep(0.25)
    return extract_contacts(texts, domain)


def search_with_tavily(dealer: str, domain: str, pool: KeyPool) -> tuple[str, str]:
    queries = [
        f"email contact {domain}",
        f'"{dealer}" owner email phone contact information',
        f"{domain} contact us email address",
    ]
    texts = []
    for query in queries:
        data = tavily_request(query, pool)
        texts.append(data.get("answer", ""))
        for row in data.get("results", []):
            texts.extend([row.get("title", ""), row.get("content", ""), row.get("raw_content", "")])
        email, phone = extract_contacts(texts, domain)
        if email and phone:
            return email, phone
        time.sleep(0.35)
    return extract_contacts(texts, domain)


def run_provider(provider: str, limit: int) -> None:
    prefix = "SERPER_API_KEY" if provider == "serper" else "TAVILY_API_KEY"
    pool = KeyPool(prefix)
    if not pool.has_keys():
        print(f"Skipping {provider}: {prefix} is not set in .env")
        return

    cache_path = CACHE_DIR / f"{provider}_cache.json"
    cache = load_json(cache_path)
    contacts_map = load_contacts_map()
    targets = collect_targets(cache, contacts_map, limit)

    print(f"\n{provider.title()} targets: {len(targets)} | cached: {len(cache)} | keys: {len(pool.keys)}")
    if not targets:
        return

    found = 0
    for index, target in enumerate(targets, 1):
        dealer = target["dealer"]
        domain = target["domain"]
        print(f"[{index}/{len(targets)}] {domain:35s} {dealer[:35]}", end="  ", flush=True)
        try:
            if provider == "serper":
                email, phone = search_with_serper(dealer, domain, pool)
            else:
                email, phone = search_with_tavily(dealer, domain, pool)
        except RuntimeError as exc:
            print(f"\n{provider.title()} stopped: {exc}")
            save_json(cache_path, cache)
            return

        if email or phone:
            print(f"found {email or '-'} {phone or '-'}")
            cache[dealer] = {
                "found": True,
                "contact_email": email,
                "contact_phone": phone,
                "contact_source": provider,
                "domain": domain,
            }
            found += 1
        else:
            print("not found")
            cache[dealer] = {"found": False, "domain": domain}

        save_json(cache_path, cache)
        time.sleep(0.5)

    print(f"{provider.title()} complete: found contact data for {found}/{len(targets)} dealers")


def main() -> None:
    args = parse_args()
    providers = ["serper", "tavily"] if args.provider == "both" else [args.provider]
    for provider in providers:
        run_provider(provider, args.limit)
    print("\nNext: python3 merge_contacts.py")


if __name__ == "__main__":
    main()
