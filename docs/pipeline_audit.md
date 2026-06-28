# Pipeline Audit

This repo was reduced to the coherent guns.com dealer pipeline after a local review.

## Kept

- `main.py` and `scraper/`: required for the guns.com dealer scrape.
- `deduplicate.py` and `clean_and_merge.py`: required cleanup utilities for duplicate and chain-store handling.
- `enrich_websites.py`, `postprocess_websites.py`, `verify_websites.py`, `apollo_enrich.py`, `exa_fill_gaps.py`, `google_enrich.py`, `verify_cloudflare.py`: website discovery and verification workflow.
- `scrape_contacts.py`, `apollo_contacts.py`, `exa_contacts.py`, `merge_contacts.py`, `run_pipeline.sh`: contact enrichment and final merge workflow.
- `optional_search_enrich.py`: optional consolidated Serper/Tavily search enrichment with key rotation.
- `.env.example`, `.gitignore`, `requirements.txt`, `README.md`: repo setup and safety files.

## Removed

Removed from the public repo because they were experiments, duplicate provider-specific reruns, marketplace-comparison tools, or outbound cleanup scripts that were not required by the main runnable pipeline:

- `apollo_domain_enrich.py`
- `atf_ffl_enrich.py`
- `build_outbound.py`
- `enrich_marketplace.py`
- `exa_domain_enrich.py`
- `exa_rerun_enrich.py`
- `exa_web_fetch.py`
- `marketplace_enrich.py`
- `rocketreach_domain_enrich.py`
- `rocketreach_enrich.py`
- `serper_enrich.py`
- `split_contacts.py`
- `tavily_enrich.py`
- `top16_enriched.py`
- `vibe_domain_enrich.py`
- `vibe_enrich.py`
- old internal task notes under `docs/plan.md` and `docs/todo.md`

## Validation Run

The cleaned repo was checked with:

```bash
python3 main.py --help
bash -n run_pipeline.sh
python3 -m compileall -q .
```

The live scrape was not executed during cleanup because it requires a US IP/VPN, live guns.com access, interactive Cloudflare/CAPTCHA handling, and external API credentials.
