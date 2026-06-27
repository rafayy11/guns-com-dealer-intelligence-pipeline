#!/usr/bin/env python3
"""
guns.com Seller Data Scraper
Usage: python3 main.py --help
"""

import argparse
import sys
import time
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="guns_scraper",
        description="Scrape guns.com dealer/seller data: name, listing counts, contact info.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  python3 main.py --output dealers.csv --limit 10
  python3 main.py --output dealers.csv --state TX
  python3 main.py --output dealers.csv --resume
  python3 main.py --discover-only --output-dir ./out/
  python3 main.py --rediscover --output dealers.csv
        """,
    )
    p.add_argument("--output", "-o", default="guns_com_dealers.csv",
                   help="Output CSV file path (default: guns_com_dealers.csv)")
    p.add_argument("--limit", "-n", type=int, default=0,
                   help="Stop after scraping N dealers (0 = no limit). Use for testing.")
    p.add_argument("--state", "-s", default="",
                   help="Only scrape dealers in this US state (2-letter code, e.g. TX).")
    p.add_argument("--resume", "-r", action="store_true",
                   help="Resume a previous run from the last saved checkpoint.")
    p.add_argument("--discover-only", action="store_true",
                   help="Run discovery only (saves dealers_discovered.json). Skip profile scraping.")
    p.add_argument("--rediscover", action="store_true",
                   help="Force re-run of discovery even if dealers_discovered.json exists.")
    p.add_argument("--headless", action="store_true",
                   help="Run Chrome in headless mode. CAPTCHA solving will not work.")
    p.add_argument("--output-dir", default="./out",
                   help="Directory for intermediate files (checkpoint, discovered dealers). Default: ./out")
    return p


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    output_path = Path(args.output)
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    print("\n" + "=" * 60)
    print("  guns.com Dealer Scraper")
    print("=" * 60)
    print(f"  Output CSV   : {output_path}")
    print(f"  Limit        : {args.limit if args.limit else 'all dealers'}")
    print(f"  State filter : {args.state.upper() if args.state else 'none (all states)'}")
    print(f"  Resume       : {args.resume}")
    print(f"  Headless     : {args.headless}")
    print("=" * 60 + "\n")

    from scraper.browser import get_browser, close_browser
    from scraper.discovery import discover_dealers
    from scraper.profile import scrape_dealer_profile
    from scraper.listing_count import get_listing_count, get_dealer_new_count
    from scraper.storage import CSVWriter

    start_time = time.time()

    try:
        sb = get_browser(headless=args.headless)

        # ── Phase 1: Dealer Discovery ──────────────────────────────────────
        dealers = discover_dealers(sb, str(output_dir), force=args.rediscover)

        if not dealers:
            print("ERROR: No dealers found. Check the browser window for issues.")
            sys.exit(1)

        # State filter (only possible after discovery with contact info)
        if args.state:
            state_filter = args.state.upper().strip()
            # Pre-filter on state if already known from cache; we re-filter after profile scrape too
            dealers_filtered = [d for d in dealers if d.get("state", "").upper() == state_filter]
            if dealers_filtered:
                print(f"  After state filter ({state_filter}): {len(dealers_filtered)} dealers (pre-filtered from cache)")
                dealers = dealers_filtered
            else:
                print(f"  Note: state filter '{state_filter}' applied after profile scraping (state not in cache yet)")

        if args.discover_only:
            print(f"\n  --discover-only set. Discovery done.")
            print(f"  {len(dealers)} dealers saved to {output_dir}/dealers_discovered.json")
            return

        # ── Phase 2: Profile scraping + CSV output ─────────────────────────
        writer = CSVWriter(str(output_path), resume=args.resume)
        start_index = 0
        if args.resume:
            start_index = writer.load_checkpoint() + 1
            if start_index > 0:
                print(f"  Resuming from dealer #{start_index} (skipping {start_index} already done)")

        limit = args.limit or len(dealers)
        total_to_scrape = min(len(dealers) - start_index, limit)
        print(f"\n  Scraping {total_to_scrape} dealers...\n")

        scraped = 0
        total_listings_found = 0

        for i, dealer in enumerate(dealers):
            if i < start_index:
                continue
            if scraped >= limit:
                break

            name = dealer.get("dealer_name", "Unknown")
            profile_url = dealer.get("profile_url", "")
            used_count = dealer.get("_used_count", -1)
            # Discovery new-count comes from a facet capped at 1,000 alphabetical entries.
            # Re-query individually so dealers past that cap get correct counts.
            discovery_new = dealer.get("_new_count", 0)
            new_count = get_dealer_new_count(sb, name)
            if new_count == 0 and discovery_new > 0:
                new_count = discovery_new  # fall back to discovery if individual query returns 0
            total_count = (used_count if used_count >= 0 else 0) + new_count

            print(f"  [{i+1}/{len(dealers)}] {name}")

            # Scrape one product page for contact info
            profile_data = scrape_dealer_profile(sb, name, profile_url)

            # State filter (re-apply after we have location)
            if args.state:
                state_filter = args.state.upper().strip()
                dealer_state = (profile_data.get("state") or dealer.get("state", "")).upper()
                if dealer_state and dealer_state != state_filter:
                    print(f"         State mismatch ({dealer_state} ≠ {state_filter}) — skipping")
                    continue

            display_name = profile_data.get("display_name") or name

            row = {
                "dealer_name": display_name,
                "guns_com_profile_url": profile_url,
                "location_city": profile_data.get("city") or dealer.get("city", ""),
                "location_state": profile_data.get("state") or dealer.get("state", ""),
                "location_address": profile_data.get("address", ""),
                "phone": profile_data.get("phone", ""),
                "email": profile_data.get("email", ""),
                "website": profile_data.get("website", ""),
                "total_listings": total_count if total_count >= 0 else 0,
                "new_listings": new_count if new_count >= 0 else 0,
                "used_listings": used_count if used_count >= 0 else 0,
                "ffl_verified": "Yes",
                "dealer_rating": profile_data.get("dealer_rating", ""),
                "review_count": profile_data.get("review_count", ""),
            }

            writer.write_row(row, i)
            if total_count >= 0:
                total_listings_found += total_count
            scraped += 1

            print(
                f"         listings={row['total_listings']} (new={row['new_listings']}, "
                f"used={row['used_listings']}) | phone={row['phone'] or '—'} | website={row['website'] or '—'}"
            )

        writer.close()

        elapsed = time.time() - start_time
        mins, secs = divmod(int(elapsed), 60)

        print("\n" + "=" * 60)
        print(f"  Done.")
        print(f"  Dealers scraped  : {scraped}")
        print(f"  Total listings   : {total_listings_found:,}")
        print(f"  Time elapsed     : {mins}m {secs}s")
        print(f"  Output file      : {output_path}")
        print("=" * 60 + "\n")

    except KeyboardInterrupt:
        print("\n\n  Interrupted. Progress saved to checkpoint.")
        print(f"  Resume with: python3 main.py --resume --output {output_path}\n")
    finally:
        close_browser()


if __name__ == "__main__":
    main()
