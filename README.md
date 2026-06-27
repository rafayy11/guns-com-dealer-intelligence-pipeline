# guns.com Dealer Intelligence Pipeline

Python pipeline for collecting active guns.com dealer data, enriching dealer websites and decision-maker contacts, and producing a clean B2B contact dataset.

The repository is intentionally sanitized. It does not include API keys, `.env`, browser profiles, scraped lead CSVs, caches, checkpoints, logs, or generated outputs.

## Scope

This public version keeps the coherent project core:

1. Discover active guns.com dealers and listing counts.
2. Scrape visible dealer profile details from sample product pages.
3. Clean duplicate and chain-store dealer rows.
4. Find and verify official dealer websites.
5. Enrich owner/manager contacts from Apollo, Exa, and direct website scraping.
6. Merge contact sources into clean, confidence-ranked CSV outputs.

Experimental provider passes, marketplace-comparison scripts, and one-off outbound cleanup utilities were removed from the public repo to keep the project focused.

## Repository Structure

```text
.
├── main.py                         # CLI entry point for guns.com scrape
├── scraper/
│   ├── browser.py                  # Selenium/UC browser, VPN, age-gate, CAPTCHA handling
│   ├── discovery.py                # guns.com catalog API dealer discovery
│   ├── listing_count.py            # listing counts and sample product URLs
│   ├── profile.py                  # product-page dealer detail extraction
│   └── storage.py                  # CSV writer and resume checkpointing
├── deduplicate.py                  # duplicate detection and optional address verification
├── clean_and_merge.py              # known chain-store merge rules and cleanup
├── enrich_websites.py              # initial website/location discovery
├── postprocess_websites.py         # website false-positive cleanup
├── verify_websites.py              # requests-based website ownership verification
├── apollo_enrich.py                # Apollo company/domain enrichment
├── exa_fill_gaps.py                # Exa search/fetch fallback for websites
├── google_enrich.py                # final DDG-style website search fallback
├── verify_cloudflare.py            # browser verification for blocked websites
├── scrape_contacts.py              # direct website contact extraction
├── apollo_contacts.py              # Apollo people/contact enrichment
├── exa_contacts.py                 # Exa search for owner/manager names
├── merge_contacts.py               # final contact source merge and filtering
├── run_pipeline.sh                 # contact enrichment runner
├── requirements.txt
└── .env.example
```

## Core Scraper

`main.py` is the primary entry point.

```bash
python3 main.py --output dealers.csv --limit 10
python3 main.py --discover-only --output-dir ./out
python3 main.py --resume --output dealers.csv
```

It supports:

- discovery-only mode
- full dealer scrape
- state filtering
- test limits
- resumable CSV output
- headless mode
- custom output directory

### Dealer Discovery

`scraper/discovery.py` calls guns.com's internal catalog search API from the browser context. It pulls dealer facets separately for new and used listings, then merges them into one dealer list with exact active listing counts.

### Browser Handling

`scraper/browser.py` opens a persistent undetected Chrome profile and handles:

- one-time VPN setup prompt
- guns.com age gate
- Cloudflare/CAPTCHA detection
- manual CAPTCHA pause and resume
- randomized delays
- safe navigation retries

### Profile Scraping

`scraper/profile.py` fetches one real product listing for each dealer, opens the product page, and extracts any visible seller information:

- display name
- address
- city/state
- phone
- email
- website
- rating
- review count

`scraper/storage.py` writes each row immediately and stores a JSON checkpoint so long runs can resume.

## Cleaning

`deduplicate.py` detects duplicate dealer candidates through name/suffix analysis and can verify ambiguous matches by opening sample product pages and comparing physical addresses.

`clean_and_merge.py` applies known chain-store merge rules, sums listing counts, preserves location counts, and clears invalid scraped phone artifacts.

## Website Enrichment

Website discovery is separated from the base scraper because guns.com does not always expose external dealer websites.

The website workflow is:

1. `enrich_websites.py`: finds city/state and likely website from search results.
2. `postprocess_websites.py`: removes directories, marketplaces, dealer locators, and weak domain matches.
3. `verify_websites.py`: verifies ownership by matching dealer-name/location tokens in page content.
4. `apollo_enrich.py`: fills missing or unverified domains from Apollo company search.
5. `exa_fill_gaps.py`: uses Exa search/fetch for missing domains and blocked pages.
6. `google_enrich.py`: final targeted web-search fallback.
7. `verify_cloudflare.py`: browser-based verification for Cloudflare-blocked pages.

## Contact Enrichment

The contact workflow focuses on owner, founder, president, CEO, general manager, operations manager, and store manager profiles.

`run_pipeline.sh` runs the main contact enrichment sequence:

1. `apollo_contacts.py`: searches Apollo people data by dealer name/domain/title.
2. `exa_contacts.py`: searches the web for owner/manager names and nearby emails/phones.
3. `scrape_contacts.py`: crawls dealer home, contact, about, team, and staff pages.
4. `merge_contacts.py`: merges all sources and filters false positives.

`merge_contacts.py` writes:

- `contacts.csv`
- `contacts_pending_enrichment.csv`
- `contacts_excluded_false_positives.csv`

## Environment Variables

Copy `.env.example` to `.env` and add only the keys needed for the phases you run.

```bash
cp .env.example .env
```

Required for the base scrape:

- no API key required
- Chrome/SeleniumBase setup required
- US IP/VPN required

Required for enrichment:

- `APOLLO_API_KEY`
- `EXA_API_KEY`

## What Is Not Committed

The `.gitignore` excludes:

- `.env`
- real API keys
- browser profiles
- CSV lead outputs
- cache files
- checkpoint JSON files
- logs
- generated folders
- Python bytecode

## Validation

Local checks run before this cleanup:

```bash
python3 main.py --help
bash -n run_pipeline.sh
python3 -m compileall -q .
```

The live scrape/enrichment is not run as part of repository validation because it requires a US IP/VPN, interactive Cloudflare/CAPTCHA handling, live guns.com access, and external API keys.

## Tech Stack

- Python
- Selenium / undetected Chrome browser automation
- Requests / urllib
- BeautifulSoup
- DDGS search
- RapidFuzz
- Apollo API
- Exa API
- CSV/JSON file-based ETL

## CV Summary

Built a Python-based guns.com dealer intelligence pipeline that discovers active firearm dealers through internal catalog APIs, captures exact listing counts, scrapes profile data with Selenium/UC browser automation, enriches missing websites and decision-maker contacts through Apollo, Exa, and direct website crawling, and merges results into clean confidence-ranked B2B contact datasets.

