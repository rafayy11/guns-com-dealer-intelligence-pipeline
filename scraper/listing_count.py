"""
Return listing counts for a dealer.

Since discovery.py already fetches exact counts from the guns.com catalog API,
this module just passes them through.  It also fetches the URL of one real
listing for this dealer — profile.py visits that listing page to extract
contact info.
"""

import json
import urllib.parse
from . import _state


def get_listing_count(sb, dealer_name: str, profile_url: str = "",
                      used_count: int = -1, new_count: int = -1) -> dict:
    """
    Return {total, new, used, sample_listing_url}.

    If used_count/new_count are passed in (from discovery), use them directly.
    Otherwise fall back to the internal API.
    """
    if used_count >= 0 and new_count >= 0:
        return {
            "total": used_count + new_count,
            "new": new_count,
            "used": used_count,
            "sample_listing_url": "",   # will be set by profile.py
        }

    # Fallback: query the catalog API for this specific dealer
    try:
        facets_p = json.dumps({"outlet": None, "condition": None, "compliance": "", "outletOnly": None})
        fg = json.dumps({
            "dealer": dealer_name,
            "product.category": "HANDGUNS,RIFLES,SHOTGUNS",
            "product.collections": "", "product.subCategory": "", "product.manufacturer": "",
        })
        filters = json.dumps([
            {"name": "dealer", "value": "GUNS COM", "operator": "NOT"},
            {"name": "dealer", "value": "Sports South", "operator": "NOT"},
            {"name": "dealer", "value": "RSR Group", "operator": "NOT"},
        ])

        params_used = urllib.parse.urlencode({
            "facets": json.dumps({"outlet": None, "condition": "used", "compliance": "", "outletOnly": None}),
            "facetGroup": fg, "filters": filters, "sortBy": "random", "size": 0,
        })
        params_new = urllib.parse.urlencode({
            "facets": json.dumps({"outlet": None, "condition": "new", "compliance": "", "outletOnly": None}),
            "facetGroup": fg, "filters": filters, "sortBy": "random", "size": 0,
        })

        used_r = sb.execute_script(f"""
            var x = new XMLHttpRequest();
            x.open('GET', '/catalog/search/listing?' + {json.dumps(params_used)}, false);
            x.setRequestHeader('Accept', 'application/json');
            x.send();
            return x.status === 200 ? JSON.parse(x.responseText).totalResultsCount : 0;
        """) or 0

        new_r = sb.execute_script(f"""
            var x = new XMLHttpRequest();
            x.open('GET', '/catalog/search/listing?' + {json.dumps(params_new)}, false);
            x.setRequestHeader('Accept', 'application/json');
            x.send();
            return x.status === 200 ? JSON.parse(x.responseText).totalResultsCount : 0;
        """) or 0

        return {"total": int(used_r) + int(new_r), "new": int(new_r), "used": int(used_r), "sample_listing_url": ""}

    except Exception as e:
        print(f"  [listing_count] fallback API error: {e}")
        return {"total": 0, "new": 0, "used": 0, "sample_listing_url": ""}


def get_dealer_new_count(sb, dealer_name: str) -> int:
    """
    Query the catalog API for this specific dealer's new-condition listing count.
    Used to correct the new-item count that discovery may have missed (facet capped
    at 1,000 alphabetical entries — dealers with names past the cap get 0 from discovery).
    """
    try:
        fg = json.dumps({
            "dealer": dealer_name,
            "product.category": "HANDGUNS,RIFLES,SHOTGUNS",
            "product.collections": "", "product.subCategory": "", "product.manufacturer": "",
        })
        facets_p = json.dumps({"outlet": None, "condition": "new", "compliance": "", "outletOnly": None})
        filters = json.dumps([
            {"name": "dealer", "value": "GUNS COM", "operator": "NOT"},
            {"name": "dealer", "value": "Sports South", "operator": "NOT"},
            {"name": "dealer", "value": "RSR Group", "operator": "NOT"},
        ])
        params = urllib.parse.urlencode({
            "facets": facets_p, "facetGroup": fg, "filters": filters,
            "sortBy": "random", "size": 0,
        })
        result = sb.execute_script(f"""
            var x = new XMLHttpRequest();
            x.open('GET', '/catalog/search/listing?' + {json.dumps(params)}, false);
            x.setRequestHeader('Accept', 'application/json');
            x.send();
            return x.status === 200 ? JSON.parse(x.responseText).totalResultsCount : 0;
        """)
        return int(result or 0)
    except Exception:
        return 0


def get_sample_listing_url(sb, dealer_name: str, condition: str = "used") -> str:
    """
    Fetch one listing for this dealer and return the product page URL.
    Used by profile.py to visit and extract contact info.
    """
    try:
        fg = json.dumps({
            "dealer": dealer_name,
            "product.category": "HANDGUNS,RIFLES,SHOTGUNS",
            "product.collections": "", "product.subCategory": "", "product.manufacturer": "",
        })
        facets_p = json.dumps({"outlet": None, "condition": condition, "compliance": "", "outletOnly": None})
        filters = json.dumps([
            {"name": "dealer", "value": "GUNS COM", "operator": "NOT"},
            {"name": "dealer", "value": "Sports South", "operator": "NOT"},
            {"name": "dealer", "value": "RSR Group", "operator": "NOT"},
        ])
        params = urllib.parse.urlencode({
            "facets": facets_p, "facetGroup": fg, "filters": filters,
            "sortBy": "random", "size": 1,
        })
        result = sb.execute_script(f"""
            var x = new XMLHttpRequest();
            x.open('GET', '/catalog/search/listing?' + {json.dumps(params)}, false);
            x.setRequestHeader('Accept', 'application/json');
            x.send();
            if (x.status !== 200) return '';
            var d = JSON.parse(x.responseText);
            var f = d.firearms || [];
            return f.length > 0 ? (f[0].link || '') : '';
        """)
        if result:
            return "https://www.guns.com" + result
    except Exception:
        pass

    # Try new if used found nothing
    if condition == "used":
        return get_sample_listing_url(sb, dealer_name, condition="new")
    return ""
