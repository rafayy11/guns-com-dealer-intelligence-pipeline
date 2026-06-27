"""
Discover all guns.com dealers/sellers via the internal catalog search API.

Strategy:
  1. Navigate to guns.com/dealers (already in browser from primer).
  2. Call the guns.com internal API directly via XHR from the page context
     (same-origin so Cloudflare never sees a direct hit):
       GET /catalog/search/listing?facets=...&facetGroup={"dealer":""}...
  3. The response includes a "dealer" facet with {name: listing_count} for
     every dealer that has active listings.  Run once for "used" and once for
     "new" (they are separate collections on guns.com).
  4. Merge both facet dicts — result is every unique dealer + their exact counts.

The facet cap for new listings is 1000; used is uncapped (829 as of latest).
Total reachable: ~1,825+ unique dealers.
"""

import json
import time
import urllib.parse
from pathlib import Path

from .browser import safe_get, random_delay

CHECKPOINT_FILENAME = "dealers_discovered.json"
DEALERS_URL = "https://www.guns.com/dealers"

# Dealers that guns.com itself excludes from dealer facets (wholesalers / internal)
_EXCLUDE_DEALERS = {"GUNS COM", "Sports South", "RSR Group"}

_BASE_FILTERS = [
    {"name": "dealer", "value": "GUNS COM", "operator": "NOT"},
    {"name": "dealer", "value": "Sports South", "operator": "NOT"},
    {"name": "dealer", "value": "RSR Group", "operator": "NOT"},
]


def _catalog_api(sb, condition: str | None, dealer: str = "") -> dict:
    """
    Call guns.com's internal catalog search API via same-origin XHR.
    Returns the parsed JSON response dict, or {} on failure.
    """
    facets_param = json.dumps({
        "outlet": None,
        "condition": condition,
        "compliance": "",
        "outletOnly": None,
    })
    facet_group = json.dumps({
        "dealer": dealer,
        "product.category": "HANDGUNS,RIFLES,SHOTGUNS",
        "product.collections": "",
        "product.subCategory": "",
        "product.manufacturer": "",
    })
    filters_param = json.dumps(_BASE_FILTERS)

    params = urllib.parse.urlencode({
        "facets": facets_param,
        "facetGroup": facet_group,
        "filters": filters_param,
        "sortBy": "random",
        "size": 1,   # One hit so we get a usable listing link per dealer
    })

    result = sb.execute_script(f"""
        try {{
            var xhr = new XMLHttpRequest();
            xhr.open('GET', '/catalog/search/listing?' + {json.dumps(params)}, false);
            xhr.setRequestHeader('Accept', 'application/json');
            xhr.send();
            return {{status: xhr.status, body: xhr.responseText}};
        }} catch(e) {{
            return {{status: 0, body: '', error: e.toString()}};
        }}
    """)

    if not result or result.get("status") != 200:
        print(f"  [API] Error status={result.get('status')} err={result.get('error','')}")
        return {}

    try:
        return json.loads(result["body"])
    except Exception as e:
        print(f"  [API] JSON parse error: {e}")
        return {}


def _extract_dealer_facet(data: dict) -> dict:
    """
    Pull the dealer facet from an API response.
    Returns {dealer_name: count} or {}.
    """
    for facet in data.get("facets", []):
        if isinstance(facet, dict):
            if facet.get("filter", {}).get("facet") == "dealer":
                return facet.get("properties", {})
    return {}


def _get_sample_listing(data: dict) -> dict | None:
    """Return the first listing object from an API response, or None."""
    firearms = data.get("firearms", [])
    return firearms[0] if firearms else None


def _listing_to_profile_url(dealer_name: str) -> str:
    return f"https://www.guns.com/dealers?dealer={urllib.parse.quote(dealer_name)}"


def discover_dealers(sb, output_dir: str, force: bool = False) -> list[dict]:
    """
    Return a list of dealer dicts.
    Uses cached dealers_discovered.json unless force=True.
    """
    checkpoint = Path(output_dir) / CHECKPOINT_FILENAME
    Path(output_dir).mkdir(parents=True, exist_ok=True)

    if checkpoint.exists() and not force:
        print(f"  Loading cached dealer list from {checkpoint}")
        data = json.loads(checkpoint.read_text())
        print(f"  Loaded {len(data)} dealers.")
        return data

    # Make sure we're on a guns.com page so XHR works (same-origin)
    print("  [Discovery] Navigating to guns.com/dealers...")
    safe_get(sb, DEALERS_URL)
    time.sleep(4)

    print("  [Discovery] Fetching USED dealer facets via catalog API...")
    used_data = _catalog_api(sb, condition="used")
    used_dealers = _extract_dealer_facet(used_data)
    used_total = used_data.get("totalResultsCount", 0)
    print(f"  [Discovery] Used: {used_total:,} total items, {len(used_dealers)} dealers in facet")

    print("  [Discovery] Fetching NEW dealer facets via catalog API...")
    new_data = _catalog_api(sb, condition="new")
    new_dealers = _extract_dealer_facet(new_data)
    new_total = new_data.get("totalResultsCount", 0)
    print(f"  [Discovery] New: {new_total:,} total items, {len(new_dealers)} dealers in facet")

    # Merge: all unique dealers, correct used+new counts
    all_names = set(used_dealers) | set(new_dealers)
    print(f"  [Discovery] Total unique dealers: {len(all_names)}")

    dealers: list[dict] = []
    for name in sorted(all_names):
        used_cnt = used_dealers.get(name, 0)
        new_cnt = new_dealers.get(name, 0)
        dealers.append({
            "dealer_name": name,
            "profile_url": _listing_to_profile_url(name),
            "city": "",
            "state": "",
            # Pre-computed listing counts — no browser scraping needed for these
            "_used_count": used_cnt,
            "_new_count": new_cnt,
            "_total_count": used_cnt + new_cnt,
        })

    # Sort by total listings descending
    dealers.sort(key=lambda d: -d["_total_count"])

    checkpoint.write_text(json.dumps(dealers, indent=2))
    print(f"  [Discovery] Saved {len(dealers)} dealers to {checkpoint}")
    print(f"\n  Discovery complete: {len(dealers)} dealers found.")
    return dealers
