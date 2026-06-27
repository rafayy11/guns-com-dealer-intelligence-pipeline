# Plan: Email & Phone Enrichment for 356 Dealers

## Current State

### contacts.csv (294 rows — all have email OR phone)
| Status | Count |
|--------|-------|
| Email + Phone | 65 |
| Email only | 11 |
| Phone only | 218 |
| Neither | 0 |

**contacts.csv has at least one contact method for every row — but 218 rows have phone only, no email.**

### Phone number quality problem
Most phones are **store numbers, not personal** — attributed to a person only when Vibe matched them:

| Source | Count | Attribution | Reliable? |
|--------|-------|-------------|-----------|
| exa_domain | 222 | Extracted from website text — store line | Unverified |
| enriched | 32 | Guns.com profile page — store line | No person |
| vibe_domain | 21 | Vibe matched to named person | Yes |
| web | 8 | Scraped from dealer site — store line | No person |

**90 of 294 phone entries have no person name at all.**
**Of the 193 with a name, most names came from Exa text extraction — not phone-verified.**

### dealers_enriched.csv (356 rows)
| Need | Count |
|------|-------|
| Have phone, need email | 161 |
| Have nothing at all | 134 |
| **Total needing email** | **295** |

---

## Architecture Decisions

- **Hunter.io domain search** — best tool for finding professional emails from a domain. Returns all emails found for a domain, guesses pattern (first.last@, f.last@, etc.), and confidence score. Free tier: 25 searches/month. Paid: 500+/month. Best for the 295 dealers missing email.
- **Clearbit Connect / Prospector** — similar to Hunter, returns people + emails at a domain. More B2B-focused, weak for small retail.
- **Vibe Prospecting (already integrated)** — already ran domain match. Re-running won't find new people. Skip.
- **Apollo people search by domain** — already confirmed 0 hits for gun shops. Skip.
- **Phone verification via NumVerify / Twilio Lookup** — validates phone format, carrier, line type (mobile vs landline vs VoIP). Tells you it's a real number but NOT who owns it.
- **Reverse phone lookup (BeenVerified, Whitepages API)** — can return name associated with a number, but paid and noisy for business lines.
- **Hunter.io email verification** — verifies if an email address is deliverable (MX check + SMTP ping). Already have email_verifier tool for this.
- **Direct website scraping (contact page)** — for the 134 with nothing, scrape `/contact`, `/about`, `/staff` pages for email patterns and phone.

**Chosen approach:**
1. Hunter.io domain search for the 295 missing-email dealers → finds emails + person names
2. Phone verification with NumVerify for the 212 phone numbers we have → confirms real numbers, flags VoIP/invalid
3. Reverse phone lookup for the 90 anonymous phone entries → attempt to attach a name
4. Website contact page scrape for the 134 with absolutely nothing → last resort, gets store email at minimum

---

## Dependency Graph

```
dealers_enriched.csv (356 rows)
        │
        ├── Phase 1: Hunter.io email discovery  ← 295 domains without email
        │       │  outputs: hunter_cache.json (email + person name per domain)
        │       ↓
        ├── Phase 2: Phone verification         ← 212 phone numbers
        │       │  outputs: phone_verified_cache.json (valid/invalid, line type)
        │       ↓
        ├── Phase 3: Reverse phone lookup       ← 90 anonymous phones
        │       │  outputs: reverse_phone_cache.json (name per number)
        │       ↓
        ├── Phase 4: Contact page scrape        ← 134 with nothing
        │       │  outputs: adds to contacts_web_cache.json
        │       ↓
        └── Phase 5: merge_contacts.py re-run  ← combines everything
                outputs: dealers_enriched.csv (updated), contacts.csv (updated)
```

---

## Task List

### Phase 1: Hunter.io Email Discovery

#### Task 1: `hunter_enrich.py` — find emails by domain
**Description:** For each dealer in `dealers_enriched.csv` that has a website but no email, call Hunter.io's Domain Search API (`GET /v2/domain-search?domain=X&limit=10`). Pick the best result: prefer owner/founder/president/manager titles, fall back to any result. Store name, title, email, and confidence score per dealer.

**API:** `https://api.hunter.io/v2/domain-search?domain={domain}&api_key={key}&limit=10`
**Key:** Add `HUNTER_API_KEY` to `.env`

**Priority order for picking best result:**
1. Title contains: owner, founder, president, ceo, director, gm, general manager, manager
2. Highest confidence score among remaining

**Acceptance criteria:**
- [ ] Reads `dealers_enriched.csv`, targets rows where `contact_email` is blank and `website` is present
- [ ] Calls Hunter domain search per dealer domain (strip `https://www.`)
- [ ] Picks best result using title priority, falls back to highest confidence
- [ ] Only saves email if confidence >= 70
- [ ] Skips generic prefixes (info@, sales@, contact@, etc.) unless nothing better found
- [ ] Saves to `cache/hunter_cache.json` keyed by dealer_name, writes after each entry
- [ ] Prints progress: `[N/295] domain → name (title) email [confidence%]`

**Verification:**
- [ ] Run on 5 dealers manually, compare Hunter result to what you'd find on their website
- [ ] Confirm no API key appears in the cache file

**Files:** `hunter_enrich.py`, `cache/hunter_cache.json`, `.env` (add HUNTER_API_KEY)
**Scope:** Small

---

#### Task 2: Update `merge_contacts.py` to include Hunter cache
**Description:** Add `hunter_cache.json` to the merge pipeline. Priority slot: after Vibe domain, before RocketReach (since Hunter finds domain-verified emails). Add `"hunter"` to sources list.

**Acceptance criteria:**
- [ ] `load_cache("cache/hunter_cache.json")` added
- [ ] Hunter email/name/title slotted into merge priority: vibe > hunter > rocketreach > exa_domain
- [ ] `sources_agreed` includes "hunter" when Hunter contributed

**Files:** `merge_contacts.py`
**Scope:** XS

---

### Checkpoint: Phase 1
- [ ] `hunter_cache.json` has entries for most of the 295 target dealers
- [ ] Re-run `merge_contacts.py` — email count should increase noticeably
- [ ] Spot-check 5 Hunter emails by visiting the dealer website — do names match?

---

### Phase 2: Phone Verification

#### Task 3: `verify_phones.py` — validate existing phone numbers
**Description:** For every phone number in `contacts.csv`, call NumVerify (or Twilio Lookup) to confirm the number is real, active, and classify the line type (mobile, landline, VoIP, toll-free). Flag invalid or VoIP numbers for review. Store result in `cache/phone_verified_cache.json`.

**API options:**
- NumVerify: `http://apilayer.net/api/validate?number={phone}&country_code=US` — free tier 100/month, paid 500+
- Twilio Lookup: `https://lookups.twilio.com/v2/PhoneNumbers/{number}` — $0.005/lookup, ~$1 for 200 numbers

**Acceptance criteria:**
- [ ] Normalises phone to E.164 (+1XXXXXXXXXX) before lookup
- [ ] Stores: `valid`, `line_type` (mobile/landline/voip/toll_free), `carrier`, `formatted`
- [ ] Skips numbers already in cache
- [ ] Flags `line_type == "voip"` or `valid == false` with `phone_flag` column in output

**Verification:**
- [ ] Test with 3 known good numbers and 1 fake number — confirm correct classification

**Files:** `verify_phones.py`, `cache/phone_verified_cache.json`
**Scope:** Small

---

#### Task 4: Reverse phone lookup for anonymous numbers
**Description:** For the 90 phone entries that have no person name, attempt a reverse lookup to attach a name. Use NumVerify's carrier/location response or a reverse lookup API (BeenVerified API, or free-tier Whitepages). Business lines will often return the business name, not an individual — that's still useful to confirm the number belongs to this dealer.

**Acceptance criteria:**
- [ ] Targets only the 90 phone-only, name-missing contacts
- [ ] Stores reverse lookup result: `reverse_name`, `reverse_type` (business/personal/unknown)
- [ ] If `reverse_type == business` and name matches dealer → marks phone as `dealer_verified`
- [ ] Does NOT overwrite an existing contact_name — only fills blanks

**Verification:**
- [ ] Test 5 numbers manually on whitepages.com — compare to API result

**Files:** `reverse_phone_lookup.py`, `cache/reverse_phone_cache.json`
**Scope:** Small–Medium

---

### Checkpoint: Phase 2
- [ ] `phone_verified_cache.json` covers all 212 numbers
- [ ] Invalid/VoIP phones flagged — count how many are bad
- [ ] At least some anonymous phones now have a name attached

---

### Phase 3: Contact Page Scrape for Blank Dealers

#### Task 5: `scrape_contact_pages.py` — targeted contact page scrape
**Description:** For the 134 dealers with a website but zero contact info (no email, no phone, no name), scrape their `/contact`, `/about-us`, `/staff`, and `/team` pages. Extract any email pattern, phone number, and person name visible on the page. This is the same approach as the existing `scrape_contacts.py` but targeted only at these 134.

**Acceptance criteria:**
- [ ] Reads `dealers_enriched.csv`, targets rows where `contact_email`, `contact_phone`, `contact_name` are all blank
- [ ] Tries URL paths: `/contact`, `/contact-us`, `/about`, `/about-us`, `/staff`, `/team`
- [ ] Uses `requests` + `BeautifulSoup` (no browser needed — contact pages are static)
- [ ] Extracts email, phone using regex; name using existing `is_person_name()` logic
- [ ] Appends results to `cache/contacts_web_cache.json`
- [ ] Respects 1s delay between requests

**Verification:**
- [ ] Test on 5 dealers manually — compare scraped result to what you see on the page

**Files:** `scrape_contact_pages.py`
**Scope:** Small–Medium

---

### Checkpoint: Phase 3
- [ ] 134-dealer blank set reduced — how many now have at least a phone or email?
- [ ] Re-run `merge_contacts.py` — contacts.csv row count should increase

---

### Phase 4: Final Merge & Output

#### Task 6: Re-run `merge_contacts.py` + rebuild `dealers_enriched.csv`
**Description:** After all three enrichment phases, re-run merge to produce clean final outputs. Then rebuild `dealers_enriched.csv` from the new contacts.csv.

**Acceptance criteria:**
- [ ] `contacts.csv` updated with Hunter emails, verified phones, new scraped contacts
- [ ] `dealers_enriched.csv` updated with new contact columns
- [ ] Final counts printed: total with email, total with phone, total with both

**Files:** `merge_contacts.py`, `dealers_enriched.csv`
**Scope:** XS

---

## Risks & Mitigations

| Risk | Impact | Mitigation |
|------|--------|------------|
| Hunter.io free tier only 25/month | High | Check credits first — paid plan needed for 295 lookups (~$49/month for 500) |
| Hunter finds generic store emails only (info@, sales@) | Medium | Skip generic prefixes unless confidence ≥ 70; still useful for outreach even if not personal |
| Phone numbers are store lines, not owner personal mobiles | High | This is expected for small dealers — store phone is still the right outreach channel; note the distinction in the CSV |
| NumVerify free tier only 100/month | Medium | 212 numbers fits in paid tier ($14.99 for 500); or use Twilio at $0.005 each (~$1.06 total) |
| Reverse phone returns business name, not person | Low | Still confirms number belongs to the right dealer |
| Contact page scrape blocked by Cloudflare | Medium | Use `requests-html` with delay; most small dealer sites aren't Cloudflare-protected |
| 134 dealers with no contact have bare-minimum web presence | Medium | Many will have at least a phone on the contact page even if no email |

---

## Summary of What to Build

| Script | Target | Expected yield |
|--------|--------|----------------|
| `hunter_enrich.py` | 295 dealers missing email | 50–120 new emails (Hunter coverage ~40–60% for small biz) |
| `verify_phones.py` | 212 existing phones | Flags bad numbers; confirms real ones |
| `reverse_phone_lookup.py` | 90 anonymous phone-only contacts | Attaches dealer name to ~50% |
| `scrape_contact_pages.py` | 134 dealers with nothing | Likely 40–70 more phones, 20–40 emails |
