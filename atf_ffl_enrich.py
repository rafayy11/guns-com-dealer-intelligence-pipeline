#!/usr/bin/env python3
"""
ATF FFL Database Enrichment — Phase 4c.

Downloads the public ATF Federal Firearms Licensee (FFL) database,
cross-references against our 487 dealer list by business name + state,
and extracts the licensee personal name + phone (the FFL holder who bears
personal legal liability — exactly who we want to pitch).

ATF publishes every active FFL with:
  - Business trade name
  - Licensee personal name (LNAME / FNAME columns)
  - Phone number
  - Address, city, state, zip, license type

Data is free, public, and updated monthly.
ATF page: https://www.atf.gov/firearms/listing-federal-firearms-licensees

Run: python3 atf_ffl_enrich.py
"""

import csv
import json
import os
import re
import time
import urllib.request
import urllib.error
import zipfile
import io
from pathlib import Path
from difflib import SequenceMatcher
from dotenv import load_dotenv

load_dotenv()

ENRICHED    = "dealers_enriched.csv"
FFL_CACHE   = "cache/atf_ffl_cache.json"
FFL_RAW     = "cache/atf_ffl_raw.csv"
CONTACTS_OUT = "contacts.csv"

# ATF FFL listing page — we scrape this to find the current download link
ATF_LISTING_URL = "https://www.atf.gov/firearms/listing-federal-firearms-licensees"

# Fallback: direct zip URL pattern (ATF updates monthly, try a few recent months)
ATF_ZIP_URLS = [
    "https://www.atf.gov/firearms/docs/undefined/0624-ffl-listexcel/download",
    "https://www.atf.gov/firearms/docs/undefined/0524-ffl-listexcel/download",
    "https://www.atf.gov/firearms/docs/undefined/0424-ffl-listexcel/download",
    "https://www.atf.gov/firearms/docs/undefined/0325-ffl-listexcel/download",
    "https://www.atf.gov/firearms/docs/undefined/0225-ffl-listexcel/download",
    "https://www.atf.gov/firearms/docs/undefined/0125-ffl-listexcel/download",
    "https://www.atf.gov/firearms/docs/undefined/1224-ffl-listexcel/download",
]

# License types we care about (01=dealer, 02=pawnbroker, 07=manufacturer, 08=importer-mfr)
FFL_DEALER_TYPES = {"01", "02", "07", "08", "09", "10", "11"}

FUZZY_THRESHOLD = 0.72   # SequenceMatcher ratio to count as a match


# ── Text normalization ────────────────────────────────────────────────────────

_STRIP_WORDS = re.compile(
    r"\b(llc|inc|corp|co|ltd|lp|dba|and|the|of|a|an|guns|arms|outdoors|"
    r"outdoor|sports|sporting|goods|supply|supplies|enterprises|group|"
    r"holdings|ventures|solutions|systems|services|trading|exchange|"
    r"store|shop|firearms|ammo|ammunition|pawn|broker|brokers)\b",
    re.I,
)

def _norm(s: str) -> str:
    s = s.lower()
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    s = _STRIP_WORDS.sub(" ", s)
    return re.sub(r"\s+", " ", s).strip()


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, _norm(a), _norm(b)).ratio()


# ── ATF download ──────────────────────────────────────────────────────────────

def _fetch_url(url: str, timeout: int = 30) -> bytes | None:
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception as e:
        print(f"    fetch {url[:60]}: {e}")
        return None


def _find_download_link(html_bytes: bytes) -> str | None:
    """Try to find an Excel/zip download URL in the ATF listing page."""
    html = html_bytes.decode("utf-8", errors="ignore")
    # Look for links pointing to Excel or zip files
    candidates = re.findall(r'href="([^"]+(?:excel|xlsx|\.zip)[^"]*)"', html, re.I)
    for c in candidates:
        if "ffl" in c.lower() or "licens" in c.lower():
            if c.startswith("/"):
                c = "https://www.atf.gov" + c
            return c
    return None


def download_ffl_data() -> list[dict]:
    """Return list of FFL records as dicts. Downloads if not cached."""
    raw_path = Path(FFL_RAW)
    if raw_path.exists():
        print(f"Using cached ATF FFL data: {FFL_RAW}")
        return _parse_ffl_csv(raw_path.read_text(encoding="utf-8", errors="ignore"))

    print("Downloading ATF FFL database...")

    # 1. Try to scrape the listing page for a fresh link
    html = _fetch_url(ATF_LISTING_URL)
    download_url = None
    if html:
        download_url = _find_download_link(html)
        if download_url:
            print(f"  Found download link: {download_url}")

    # 2. Fall back to known URL patterns
    if not download_url:
        for url in ATF_ZIP_URLS:
            print(f"  Trying: {url}")
            data = _fetch_url(url, timeout=60)
            if data and len(data) > 10000:
                download_url = url
                break
            time.sleep(1)

    if not download_url:
        print("ERROR: Could not find ATF FFL download. Try manually downloading from:")
        print("  https://www.atf.gov/firearms/listing-federal-firearms-licensees")
        print("  Save the Excel file and convert to CSV, place at:", FFL_RAW)
        return []

    print(f"  Downloading: {download_url}")
    data = _fetch_url(download_url, timeout=120)
    if not data:
        print("ERROR: Download failed.")
        return []

    print(f"  Downloaded {len(data):,} bytes")

    # Determine format
    if download_url.lower().endswith(".zip") or data[:4] == b"PK\x03\x04":
        # It's a zip — extract and find CSV/Excel inside
        records = _extract_from_zip(data)
    elif data[:2] in (b"\xd0\xcf", b"PK"):
        # Excel binary or xlsx
        records = _parse_excel_bytes(data)
    else:
        # Assume CSV
        text = data.decode("utf-8", errors="ignore")
        raw_path.write_text(text, encoding="utf-8")
        records = _parse_ffl_csv(text)

    if records:
        print(f"  Parsed {len(records):,} FFL records")
        # Cache as CSV for future runs
        _write_ffl_csv(records)

    return records


def _extract_from_zip(data: bytes) -> list[dict]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            for name in zf.namelist():
                print(f"    ZIP entry: {name}")
                if name.lower().endswith((".csv", ".txt")):
                    text = zf.read(name).decode("utf-8", errors="ignore")
                    Path(FFL_RAW).write_text(text, encoding="utf-8")
                    return _parse_ffl_csv(text)
                if name.lower().endswith((".xlsx", ".xls")):
                    return _parse_excel_bytes(zf.read(name))
    except Exception as e:
        print(f"    ZIP error: {e}")
    return []


def _parse_excel_bytes(data: bytes) -> list[dict]:
    """Parse Excel bytes using openpyxl (xlsx) or xlrd (xls)."""
    try:
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        ws = wb.active
        rows = list(ws.iter_rows(values_only=True))
        if not rows:
            return []
        headers = [str(c).strip() if c else "" for c in rows[0]]
        records = []
        for row in rows[1:]:
            rec = {headers[i]: (str(row[i]).strip() if row[i] is not None else "") for i in range(len(headers))}
            records.append(rec)
        wb.close()
        _write_ffl_csv(records)
        return records
    except ImportError:
        pass

    try:
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        ws = wb.sheet_by_index(0)
        headers = [str(ws.cell_value(0, c)).strip() for c in range(ws.ncols)]
        records = []
        for r in range(1, ws.nrows):
            rec = {headers[c]: str(ws.cell_value(r, c)).strip() for c in range(ws.ncols)}
            records.append(rec)
        _write_ffl_csv(records)
        return records
    except ImportError:
        print("    No Excel library found. Install: pip install openpyxl")
        return []
    except Exception as e:
        print(f"    Excel parse error: {e}")
        return []


def _parse_ffl_csv(text: str) -> list[dict]:
    rows = list(csv.DictReader(io.StringIO(text)))
    return rows


def _write_ffl_csv(records: list[dict]):
    if not records:
        return
    with open(FFL_RAW, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        w.writeheader()
        w.writerows(records)


# ── Column discovery (ATF changes column names sometimes) ────────────────────

def _find_col(headers: list[str], candidates: list[str]) -> str | None:
    lh = [h.lower().strip() for h in headers]
    for c in candidates:
        cl = c.lower()
        for i, h in enumerate(lh):
            if cl in h or h in cl:
                return headers[i]
    return None


def _extract_record_fields(rec: dict) -> dict | None:
    """Normalize an FFL record to our schema."""
    keys = list(rec.keys())

    biz_col   = _find_col(keys, ["trade name", "business name", "tradename", "dba"])
    lname_col = _find_col(keys, ["lname", "last name", "last_name", "lic regn", "licensee name"])
    fname_col = _find_col(keys, ["fname", "first name", "first_name"])
    phone_col = _find_col(keys, ["phone", "telephone", "tel", "phone number"])
    state_col = _find_col(keys, ["state", "st", "lic state"])
    city_col  = _find_col(keys, ["city", "lic city"])
    type_col  = _find_col(keys, ["lic type", "type", "license type"])

    if not biz_col:
        return None

    biz   = rec.get(biz_col, "").strip()
    lname = rec.get(lname_col, "").strip() if lname_col else ""
    fname = rec.get(fname_col, "").strip() if fname_col else ""
    phone = rec.get(phone_col, "").strip() if phone_col else ""
    state = rec.get(state_col, "").strip().upper() if state_col else ""
    city  = rec.get(city_col, "").strip().title() if city_col else ""
    lic_type = rec.get(type_col, "").strip() if type_col else ""

    if not biz:
        return None

    # Skip non-dealer license types if we can determine type
    if lic_type and lic_type not in FFL_DEALER_TYPES and lic_type not in {"", "None"}:
        return None

    name = f"{fname} {lname}".strip() if fname or lname else ""

    # Clean phone
    phone = re.sub(r"[^\d]", "", phone)
    if len(phone) == 10:
        phone = f"({phone[:3]}) {phone[3:6]}-{phone[6:]}"
    elif len(phone) == 11 and phone[0] == "1":
        phone = f"({phone[1:4]}) {phone[4:7]}-{phone[7:]}"

    return {
        "biz_name": biz,
        "licensee_name": name,
        "phone": phone,
        "state": state,
        "city": city,
    }


# ── Cross-reference ───────────────────────────────────────────────────────────

def match_dealers(dealers: list[dict], ffl_records: list[dict]) -> dict:
    """
    For each dealer in our list, find the best-matching FFL record.
    Returns {dealer_name: {found, licensee_name, phone, match_score, ffl_biz_name}}
    """
    print(f"\nCross-referencing {len(dealers)} dealers against {len(ffl_records):,} FFL records...")

    # Pre-extract and normalize FFL records
    ffl_clean = []
    for r in ffl_records:
        ex = _extract_record_fields(r)
        if ex:
            ffl_clean.append(ex)

    print(f"  Valid FFL dealer records: {len(ffl_clean):,}")

    # Group FFL by state for faster search
    ffl_by_state: dict[str, list] = {}
    for r in ffl_clean:
        ffl_by_state.setdefault(r["state"], []).append(r)

    results = {}
    matched = 0

    for dealer in dealers:
        dname  = dealer["dealer_name"]
        dstate = dealer.get("location_state", "").strip().upper()
        dcity  = dealer.get("location_city", "").strip().title()

        # Search same state first, then national if no state match
        candidates = ffl_by_state.get(dstate, []) if dstate else ffl_clean
        if not candidates:
            candidates = ffl_clean

        best_score = 0.0
        best_match = None

        for ffl in candidates:
            score = _similarity(dname, ffl["biz_name"])
            # Boost if city also matches
            if dcity and ffl["city"] and _similarity(dcity, ffl["city"]) > 0.8:
                score = min(1.0, score + 0.1)
            if score > best_score:
                best_score = score
                best_match = ffl

        if best_score >= FUZZY_THRESHOLD and best_match:
            results[dname] = {
                "found":          True,
                "licensee_name":  best_match["licensee_name"],
                "phone":          best_match["phone"],
                "ffl_biz_name":   best_match["biz_name"],
                "match_score":    round(best_score, 3),
                "state":          best_match["state"],
                "city":           best_match["city"],
            }
            matched += 1
            print(f"  ✓ [{best_score:.2f}] {dname[:45]:45s} → {best_match['biz_name'][:35]:35s}  {best_match['licensee_name']:25s}  {best_match['phone']}")
        else:
            results[dname] = {"found": False}

    print(f"\nMatched {matched}/{len(dealers)} dealers in ATF FFL database.")
    return results


# ── Merge into contacts.csv ───────────────────────────────────────────────────

def _merge_ffl_into_contacts(ffl_results: dict):
    """Update contacts.csv — fill in ATF phone/name where missing."""
    contacts_path = Path(CONTACTS_OUT)
    if not contacts_path.exists():
        print("contacts.csv not found — run merge_contacts.py first.")
        return

    rows = list(csv.DictReader(open(CONTACTS_OUT, encoding="utf-8")))
    updated = 0

    for row in rows:
        dealer = row.get("dealer_name", "")
        ffl = ffl_results.get(dealer, {})
        if not ffl.get("found"):
            continue

        # Only fill in MISSING data — never overwrite verified data
        if not row.get("contact_name") and ffl.get("licensee_name"):
            row["contact_name"] = ffl["licensee_name"]
            row["contact_source"] = (row.get("contact_source", "") + "+atf_ffl").lstrip("+")
            row["verified_level"] = "medium"
            updated += 1

        if not row.get("contact_phone") and ffl.get("phone"):
            row["contact_phone"] = ffl["phone"]
            if "atf_ffl" not in row.get("contact_source", ""):
                row["contact_source"] = (row.get("contact_source", "") + "+atf_ffl").lstrip("+")

    # Write back
    fieldnames = rows[0].keys() if rows else []
    with open(CONTACTS_OUT, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fieldnames)
        w.writeheader()
        w.writerows(rows)

    print(f"Updated {updated} contacts with ATF FFL data.")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    dealers = list(csv.DictReader(open(ENRICHED, encoding="utf-8")))
    print(f"Loaded {len(dealers)} dealers from {ENRICHED}")

    # Load or download ATF FFL data
    ffl_records = download_ffl_data()
    if not ffl_records:
        print("\nNo FFL data available. Exiting.")
        print("\nManual option: Download the Excel from ATF, convert to CSV,")
        print(f"save at: {FFL_RAW}")
        print("Then re-run this script.")
        return

    # Cross-reference
    ffl_cache_path = Path(FFL_CACHE)
    ffl_results = match_dealers(dealers, ffl_records)

    # Save cache
    ffl_cache_path.write_text(json.dumps(ffl_results, indent=2))
    print(f"\nATF FFL cache saved → {FFL_CACHE}")

    # Stats
    found   = sum(1 for v in ffl_results.values() if v.get("found"))
    w_phone = sum(1 for v in ffl_results.values() if v.get("found") and v.get("phone"))
    w_name  = sum(1 for v in ffl_results.values() if v.get("found") and v.get("licensee_name"))

    print(f"\nATF FFL results:")
    print(f"  Matched dealers   : {found}/{len(dealers)}")
    print(f"  With licensee name: {w_name}")
    print(f"  With phone        : {w_phone}")

    # Merge into contacts.csv if it exists
    _merge_ffl_into_contacts(ffl_results)


if __name__ == "__main__":
    main()
