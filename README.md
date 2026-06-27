# guns.com Dealer Intelligence Pipeline

Python scraping and enrichment system for building a structured lead dataset from guns.com dealer activity. The project discovers active dealers, captures listing volume, extracts visible profile/contact data, enriches missing websites and decision-maker contacts, verifies contact quality, and builds outbound-ready CSVs.

This repository intentionally excludes real scraped data, API keys, browser profiles, generated caches, checkpoints, logs, and lead CSV outputs.

## What It Does

The system turns guns.com marketplace activity into a cleaned dealer-intelligence dataset:

1. Opens a real Chrome/UC browser session with persistent profile support.
2. Handles VPN setup, age gates, Cloudflare challenges, and CAPTCHA pauses.
3. Calls guns.com's internal catalog API from the browser context to discover active dealers and exact listing counts.
4. Visits one sample product listing per dealer to extract visible seller profile details.
5. Writes resumable CSV output with dealer name, profile URL, location, contact fields, listing counts, FFL status, ratings, and scrape timestamp.
6. Cleans duplicate/chain dealers and clears invalid scraped phone artifacts.
7. Finds official dealer websites through search APIs, Apollo, Exa, DDG/Google-style search, and Cloudflare/browser fallback.
8. Enriches owner/manager contacts from websites, Apollo, Exa, Vibe/Explorium, RocketReach, Serper, Tavily, ATF FFL data, and Reoon verification.
9. Merges all contact sources with priority rules, false-positive filters, and confidence levels.
10. Splits final contacts into personal-email, generic-email, phone-only, manual-review, and outbound campaign files.

## Core Scraper

### `main.py`

Command-line entry point for the guns.com scrape. It supports:

- Full scrape.
- Discovery-only mode.
- Resume from checkpoint.
- State filtering.
- Test limits.
- Headless mode.
- Custom output directory.

Example:

```bash
python3 main.py --output dealers.csv --limit 10
python3 main.py --discover-only --output-dir ./out
python3 main.py --resume --output dealers.csv
```

### `scraper/browser.py`

Owns browser lifecycle. It uses undetected Chrome through Selenium/SeleniumBase support and wraps browser operations behind a small helper class.

Responsibilities:

- Creates a persistent `chrome_profile/`.
- Prompts for one-time VPN setup when needed.
- Uses Chrome for Testing and UC driver where available.
- Handles age-gate buttons.
- Detects Cloudflare/CAPTCHA pages.
- Pauses for manual CAPTCHA solving.
- Provides safe navigation and randomized delays.

### `scraper/discovery.py`

Discovers dealers through guns.com's internal catalog API. The module navigates to guns.com, then runs same-origin XHR calls from the browser context.

It pulls dealer facets separately for:

- Used listings.
- New listings.

The two dealer dictionaries are merged to create a unique dealer list with exact active listing counts.

Output in the working pipeline:

- `out/dealers_discovered.json`.

### `scraper/listing_count.py`

Fetches exact per-dealer listing counts and sample listing URLs through the catalog API. It re-checks new listing count per dealer because the discovery facet can be capped.

### `scraper/profile.py`

Visits one sample product page for a dealer and extracts any visible seller profile data:

- Display name.
- Address.
- City/state.
- Phone.
- Email.
- External website.
- Dealer rating.
- Review count.

### `scraper/storage.py`

Writes rows to CSV with immediate flush and JSON checkpointing. This allows long scrapes to resume safely after interruptions.

## Cleaning and Deduplication

### `deduplicate.py`

Detects possible duplicate dealers using prefix/name-variant analysis. It can optionally verify ambiguous pairs by opening product pages and comparing addresses.

Signals:

- Name/suffix classification.
- Browser-based address comparison.
- Manual review for unresolved duplicates.

### `clean_and_merge.py`

Handles known chain-store groups, folds branch variants into canonical rows where appropriate, sums listing counts, preserves location counts, and flags risky groups for manual review.

## Website Enrichment

Website discovery and verification is handled through several scripts because no single source covers every dealer.

Key scripts:

- `enrich_websites.py`: initial DDG/masterFFL-based city/state and website enrichment.
- `postprocess_websites.py`: clears directory/locator/platform false positives.
- `verify_websites.py`: verifies that page content matches dealer name or location.
- `apollo_enrich.py`: uses Apollo company search to fill missing domains.
- `exa_fill_gaps.py`: uses Exa search/fetch to find websites or verify Cloudflare-blocked sites.
- `google_enrich.py`: final targeted search fallback for missing websites.
- `verify_cloudflare.py`: uses browser rendering for pages blocked to normal HTTP requests.

Verification logic includes:

- Dealer-name token matches.
- Location matches.
- Domain token scoring.
- Directory and marketplace exclusion.
- Cloudflare/fetch-failure handling.
- Source tagging and confidence fields.

## Contact Enrichment

The project uses a layered contact enrichment strategy. Each provider writes to a cache, and `merge_contacts.py` later selects the best result.

Core sources:

- `scrape_contacts.py`: crawls dealer websites and contact/about/team pages for owner/manager names, titles, emails, and phones.
- `apollo_contacts.py`: searches Apollo people data by company/domain/title.
- `exa_contacts.py`: uses Exa neural search to find owner/manager names from snippets, LinkedIn, local news, BBB pages, and web pages.
- `exa_domain_enrich.py`: searches by dealer domain and owner/manager keywords.
- `exa_web_fetch.py`: fetches contact/about pages through Exa to bypass blocked sites.
- `vibe_enrich.py`: enriches known named contacts through Vibe/Explorium.
- `vibe_domain_enrich.py`: matches businesses by domain, fetches prospects, and enriches owner/manager contacts.
- `rocketreach_enrich.py`: enriches already-known names through RocketReach.
- `rocketreach_domain_enrich.py`: discovers and enriches prospects from RocketReach by company/domain.
- `serper_enrich.py`: extracts indexed emails/phones through Serper Google search.
- `tavily_enrich.py`: uses Tavily search as an alternate index.
- `atf_ffl_enrich.py`: downloads the public ATF FFL database and matches licensee names/phones to dealers.

## Merge and Output

### `merge_contacts.py`

Merges all enrichment caches into a clean contact CSV. It applies source priority, confidence levels, and false-positive filters.

Priority includes:

- Apollo.
- Vibe domain/name matches.
- RocketReach.
- Exa domain/name searches.
- Apollo domain.
- Serper.
- Tavily.
- Exa rerun.
- Exa web fetch.
- Direct website scraping.

It writes:

- `contacts.csv`.
- `contacts_pending_enrichment.csv`.
- `contacts_excluded_false_positives.csv`.

### `split_contacts.py`

Splits final contacts into personal email, generic email, and manual-review groups.

### `build_outbound.py`

Builds outbound campaign CSVs. It:

- Classifies email addresses as personal or generic.
- Verifies generic emails with Reoon.
- Re-enriches invalid personal emails through Serper.
- Produces personal and generic outbound lists.

## Supporting Marketplace Scripts

Some scripts support a separate marketplace-comparison workflow:

- `marketplace_enrich.py`
- `enrich_marketplace.py`
- `top16_enriched.py`

They reuse enrichment caches from the main guns.com pipeline to fill contacts for marketplace comparison outputs.

## Tech Stack

- Python
- Selenium / SeleniumBase UC browser automation
- Requests / urllib
- BeautifulSoup
- DDGS search
- RapidFuzz
- Apollo API
- Exa API
- Vibe/Explorium API
- RocketReach API
- Serper API
- Tavily API
- Reoon email verification
- ATF public FFL data
- CSV/JSON file-based ETL

## GitHub Safety Notes

Do not commit:

- `.env`
- API keys
- real scraped leads
- `cache/`
- `chrome_profile/`
- `out/`
- `snapshots/`
- `downloaded_files/`
- CSV exports
- checkpoint JSON files
- logs

Use `.env.example` as the safe configuration template.

## CV Summary

Built a Python-based dealer intelligence pipeline that scrapes guns.com dealer activity, captures exact listing counts through internal catalog APIs, extracts seller profile data with browser automation, enriches missing websites and decision-maker contacts through multiple external providers, verifies lead quality, removes false positives, and produces outbound-ready B2B contact lists.

