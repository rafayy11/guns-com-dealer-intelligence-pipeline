# User Guide

This project is a semi-automated pipeline. The scraper can resume long runs, but
guns.com may show Cloudflare or CAPTCHA checks. A real user must pass those
checks in the Chrome window when they appear.

## 1. Install

Use Python 3 and install the dependencies:

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
seleniumbase install chrome
```

Create a local environment file:

```bash
cp .env.example .env
```

Never commit `.env`, cache files, browser profiles, or generated CSVs.

## 2. First guns.com Run

Use a visible browser for the first run. Do not use `--headless` until the VPN,
age gate, and Cloudflare session are already working.

```bash
python3 main.py --output dealers.csv --limit 5 --output-dir out/test
```

When Chrome opens:

1. Install or enable a VPN extension.
2. Set the VPN location to the United States.
3. Open guns.com in that Chrome session if needed.
4. Accept the age gate.
5. Solve Cloudflare/CAPTCHA if it appears.
6. Return to the terminal and press Enter when the script asks.

The browser profile is saved in `chrome_profile/`, so the VPN extension and
session cookies can be reused. If Cloudflare appears again during a later run,
solve it in the same visible browser and press Enter.

If the run is interrupted:

```bash
python3 main.py --resume --output dealers.csv --output-dir out/test
```

## 3. Cleaning and Website Enrichment

The cleanup scripts expect the base scrape to be named `dealers.csv`.

```bash
python3 deduplicate.py
python3 clean_and_merge.py
cp dealers_clean.csv dealers_final.csv
python3 enrich_websites.py
python3 postprocess_websites.py
python3 verify_websites.py
```

Then run the API/browser fallbacks as needed:

```bash
python3 apollo_enrich.py
python3 exa_fill_gaps.py
python3 google_enrich.py
python3 verify_cloudflare.py
```

`exa_fill_gaps.py` can rotate through multiple Exa keys from `.env`. If the
next key is not configured, it asks the user to paste the next key manually.

## 4. Contact Enrichment

After `dealers_enriched.csv` exists:

```bash
bash run_pipeline.sh
```

That runs:

1. Apollo people/contact enrichment.
2. Exa owner/manager search.
3. Direct dealer website scraping.
4. Final merge.

Optional Serper/Tavily search enrichment can run after the core contact passes:

```bash
python3 optional_search_enrich.py --provider both
python3 merge_contacts.py
```

Use a limit for testing:

```bash
python3 optional_search_enrich.py --provider serper --limit 25
python3 optional_search_enrich.py --provider tavily --limit 25
```

## 5. API Keys

Add keys to `.env`.

```bash
APOLLO_API_KEY=your_apollo_key

EXA_API_KEY=exa_account_1_key
EXA_API_KEY_2=exa_account_2_key
EXA_API_KEY_3=exa_account_3_key

SERPER_API_KEY=serper_account_1_key
SERPER_API_KEY_2=serper_account_2_key
SERPER_API_KEY_3=serper_account_3_key

TAVILY_API_KEY=tavily_account_1_key
TAVILY_API_KEY_2=tavily_account_2_key
TAVILY_API_KEY_3=tavily_account_3_key
```

How to get keys:

- Apollo: log in to Apollo, open settings/developer/API keys, create or copy an API key.
- Exa: open `https://dashboard.exa.ai/api-keys`, log in, create/copy an API key.
- Serper.dev: log in to `https://serper.dev`, open the API key/dashboard page, copy the key.
- Tavily: log in to Tavily, open the API key dashboard, create/copy the key.

For multiple Exa, Serper, or Tavily accounts, log in to each account in your
normal browser, copy that account's key, and paste it into the next numbered
variable in `.env`.

## 6. Rotation Behavior

Exa:

- `exa_contacts.py` rotates through `EXA_API_KEY`, `EXA_API_KEY_2`, etc. when
  Exa returns quota/rate-limit responses.
- `exa_fill_gaps.py` uses the same numbered keys first. If there is no next
  key in `.env`, it prompts the user to paste the next account key.

Serper and Tavily:

- `optional_search_enrich.py` rotates through `SERPER_API_KEY`, `SERPER_API_KEY_2`, etc.
- It rotates through `TAVILY_API_KEY`, `TAVILY_API_KEY_2`, etc.
- Each provider writes its own cache under `cache/`.
- `merge_contacts.py` reads those optional caches if they exist.

Apollo:

- Apollo is configured as a single key: `APOLLO_API_KEY`.
- If Apollo quota is exhausted, replace the key in `.env` or resume after quota resets.

## 7. Outputs

Main generated files:

- `dealers.csv`
- `dealers_clean.csv`
- `dealers_enriched.csv`
- `contacts.csv`
- `contacts_pending_enrichment.csv`
- `contacts_excluded_false_positives.csv`

All generated files are ignored by Git.

## 8. Troubleshooting

If Selenium cannot bind a port, run outside a restricted sandbox or terminal
policy. Selenium needs to start a local WebDriver service.

If the scraper loops on Cloudflare, stop the headless run and rerun without
`--headless` so a user can solve the challenge.

If the VPN setup prompt was marked complete too early, remove the marker file:

```bash
rm chrome_profile/.vpn_setup_done
```

Then rerun `main.py` without `--headless` and complete the VPN setup.
