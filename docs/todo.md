# Todo: Email & Phone Enrichment

## Phase 1 — Hunter.io Email Discovery
- [ ] Add HUNTER_API_KEY to .env
- [ ] Task 1: Build `hunter_enrich.py` (295 dealers missing email)
- [ ] Task 2: Update `merge_contacts.py` to include hunter_cache

## Checkpoint 1
- [ ] hunter_cache.json populated, merge re-run, email count checked

## Phase 2 — Phone Verification
- [ ] Task 3: Build `verify_phones.py` (212 existing phones → valid/invalid/line type)
- [ ] Task 4: Build `reverse_phone_lookup.py` (90 anonymous phones → attach name)

## Checkpoint 2
- [ ] All phones verified, anonymous phones have names where possible

## Phase 3 — Contact Page Scrape
- [ ] Task 5: Build `scrape_contact_pages.py` (134 dealers with zero contact info)

## Checkpoint 3
- [ ] Blank dealer set reduced, merge re-run

## Phase 4 — Final Output
- [ ] Task 6: Final merge_contacts.py + rebuild dealers_enriched.csv
- [ ] Final counts: email / phone / both
