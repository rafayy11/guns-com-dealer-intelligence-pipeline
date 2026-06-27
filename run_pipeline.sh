#!/bin/bash
# Contact enrichment pipeline (reordered for speed):
#   Phase 2 (Apollo API, fast) → Phase 3 (Exa API, fast) → Phase 1 (website scrape, slow/overnight)
#   → cleanup all caches → merge_contacts.py

PIPELINE_LOG="${PIPELINE_LOG:-/tmp/pipeline_all_phases.log}"
DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

# ─── Shared cleanup logic (applied to both web and exa caches) ──────────────
cleanup_cache() {
    local cache_path="$1"
    python3 - "$cache_path" <<'EOF'
import json, re, sys
from pathlib import Path

CACHE = sys.argv[1]
if not Path(CACHE).exists():
    print(f"No cache at {CACHE} — skipping.")
    exit(0)

cache = json.loads(Path(CACHE).read_text())

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
    # Career/HR/job-posting phrases
    "career", "careers", "hiring", "apply", "applying", "resume",
    "lead", "leads", "crew", "staff", "team", "member", "members",
    "position", "role", "roles", "candidate", "candidates",
    # CSS / typography
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
    # Firearms/retail
    "ammo", "ammunition", "firearm", "firearms", "rifle", "pistol",
    "shotgun", "handgun", "shooting", "range", "hunting", "tactical",
    "price", "sale", "deal", "special", "offer", "discount", "free",
    "shipping", "service", "repair",
    # Layout/UI
    "page", "section", "content", "main", "area", "top", "bottom",
    "center", "middle", "side", "base", "full", "half",
    # Business/company name words
    "sports", "arms", "outdoors", "outfitters", "outdoor", "supply",
    "supplies", "enterprises", "solutions", "systems", "services",
    "group", "company", "industries", "holdings", "ventures",
    "mega", "super", "plus", "premier", "elite",
    "national", "regional", "local", "global", "american", "united",
    "federal", "state", "county", "valley", "mountain",
    "river", "lake", "creek", "ridge", "hills", "plains", "coast",
    "indoor", "outlet", "market", "exchange", "trading",
    "pawnbrokers", "jewelry", "jewelers", "auction",
    # Constitutional / legal (common in gun shop names)
    "amendment", "constitutional", "rights", "liberty", "freedom",
    "patriot", "patriotic", "republic", "militia", "defense", "defensive",
    # Questions / abstract words
    "questions", "question", "answers", "answer", "solutions", "problems",
}

def is_business_echo(candidate, dealer):
    c_words = set(re.sub(r"[^a-z ]", "", candidate.lower()).split())
    d_words = set(re.sub(r"[^a-z ]", "", dealer.lower()).split())
    return bool(c_words) and c_words.issubset(d_words)

fixed = 0
for dealer, data in cache.items():
    name = data.get("contact_name", "")
    if not name:
        continue
    parts = name.lower().split()
    bad = False
    if len(parts) >= 2:
        first, last = parts[0], parts[-1]
        if first in NOT_NAMES or last in NOT_NAMES:
            bad = True
        elif is_business_echo(name, dealer):
            bad = True
    if bad:
        print(f"  FIXED: {dealer[:50]} -> '{name}'")
        if data.get("contact_email") or data.get("contact_phone"):
            data["contact_name"] = ""
            data["contact_title"] = ""
            data["contact_source"] = data.get("contact_source", "").replace("exa","web_store_page").replace("web_page","web_store_page") or "web_store_page"
            data["verified_level"] = "low"
        else:
            data["found"] = False
            for k in ("contact_name","contact_title","contact_email","contact_phone","contact_source","verified_level"):
                data.pop(k, None)
        fixed += 1

Path(CACHE).write_text(json.dumps(cache, indent=2))
found  = sum(1 for v in cache.values() if v.get("found"))
high   = sum(1 for v in cache.values() if v.get("verified_level") == "high")
medium = sum(1 for v in cache.values() if v.get("verified_level") == "medium")
low    = sum(1 for v in cache.values() if v.get("verified_level") == "low")
print(f"{CACHE}: fixed {fixed} bad names. Found={found} high={high} medium={medium} low={low}")
EOF
}

log() { echo "$1" | tee -a "$PIPELINE_LOG"; }

log "======================================================"
log "PIPELINE START: $(date)"
log "Order: Phase 2 (Apollo) → Phase 3 (Exa) → Phase 1 (Web) → Merge"
log "======================================================"

# ─── Phase 2: Apollo ──────────────────────────────────────
log ""
log "Starting Phase 2 — Apollo contacts: $(date)"
python3 apollo_contacts.py 2>&1 | tee -a "$PIPELINE_LOG"
log "Phase 2 done: $(date)"

# ─── Phase 3: Exa ────────────────────────────────────────
log ""
log "======================================================"
log "Starting Phase 3 — Exa contacts: $(date)"
python3 exa_contacts.py 2>&1 | tee -a "$PIPELINE_LOG"
log "Phase 3 done: $(date)"

# ─── Cleanup Exa cache ───────────────────────────────────
log ""
log "Cleaning Exa cache false positives..."
cleanup_cache "cache/exa_contacts_cache.json" 2>&1 | tee -a "$PIPELINE_LOG"

# ─── Phase 1: Website scraping (overnight) ───────────────
log ""
log "======================================================"
log "Starting Phase 1 — Website scraping (slow, Exa CF fallback): $(date)"
python3 scrape_contacts.py 2>&1 | tee -a "$PIPELINE_LOG"
log "Phase 1 done: $(date)"

# ─── Cleanup web cache false positives ───────────────────
log ""
log "Cleaning web cache false positives..."
cleanup_cache "cache/contacts_web_cache.json" 2>&1 | tee -a "$PIPELINE_LOG"

# ─── Merge all three phases ──────────────────────────────
log ""
log "======================================================"
log "Running merge_contacts.py: $(date)"
python3 merge_contacts.py 2>&1 | tee -a "$PIPELINE_LOG"

log ""
log "======================================================"
log "ALL PHASES COMPLETE: $(date)"
log "contacts.csv is ready."
log "NOTIFY USER: Ready for Phase 4 — Vibe Prospecting API keys needed."
log "======================================================"
