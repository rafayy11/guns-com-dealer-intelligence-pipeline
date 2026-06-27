"""
Extract dealer contact info and location from a guns.com product detail page.

guns.com doesn't have public dealer profile pages — contact info (address,
phone, rating) appears on the product detail page for each listing.  We
visit ONE listing per dealer and scrape whatever contact info is shown.
"""

import re
import time

from .browser import safe_get
from .listing_count import get_sample_listing_url


def _try_text(sb, selectors: list, default: str = "") -> str:
    for sel in selectors:
        try:
            el = sb.find_element(sel)
            text = el.text.strip()
            if text:
                return text
        except Exception:
            continue
    return default


def _extract_phone(sb) -> str:
    try:
        for link in sb.find_elements("a[href^='tel:']"):
            phone = link.get_attribute("href").replace("tel:", "").strip()
            if phone:
                return phone
    except Exception:
        pass
    try:
        src = sb.get_page_source()
        matches = re.findall(r'\(?\d{3}\)?[\s.\-]\d{3}[\s.\-]\d{4}', src)
        if matches:
            return matches[0]
    except Exception:
        pass
    return ""


def _extract_email(sb) -> str:
    try:
        for link in sb.find_elements("a[href^='mailto:']"):
            email = link.get_attribute("href").replace("mailto:", "").strip()
            if email and "@" in email and "guns.com" not in email:
                return email
    except Exception:
        pass
    try:
        src = sb.get_page_source()
        for m in re.findall(r'[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}', src):
            if "guns.com" not in m and "example" not in m:
                return m
    except Exception:
        pass
    return ""


def _extract_website(sb) -> str:
    try:
        for link in sb.find_elements("a[href^='http']"):
            href = (link.get_attribute("href") or "").strip()
            if not href or "guns.com" in href:
                continue
            # Filter out social/tracking/CDN links
            skip = ["facebook", "instagram", "twitter", "google", "youtube",
                    "cloudflare", "akamai", "listrak", "doubleclick", "amazon"]
            if any(s in href.lower() for s in skip):
                continue
            text = (link.text or link.get_attribute("aria-label") or "").lower()
            if any(k in text for k in ("website", "visit", "dealer", "our site", "store")):
                return href
    except Exception:
        pass
    return ""


def _extract_rating(sb) -> tuple:
    rating, count = "", ""
    for sel in ["[class*='rating']", "[itemprop='ratingValue']", "[class*='stars']"]:
        try:
            el = sb.find_element(sel)
            val = el.get_attribute("content") or el.text.strip()
            if val:
                rating = val
                break
        except Exception:
            continue
    for sel in ["[itemprop='reviewCount']", "[class*='review-count']"]:
        try:
            el = sb.find_element(sel)
            val = el.get_attribute("content") or el.text.strip()
            nums = re.findall(r'^\d+$', val.strip())
            if nums:
                count = nums[0]
                break
        except Exception:
            continue
    return rating, count


def _extract_location_from_seller_section(sb) -> tuple:
    """
    Try to find city/state from a seller/dealer info section on the page.
    Deliberately narrow: only looks inside dealer-specific UI elements
    to avoid picking up guns.com's own address from the footer.
    """
    # Selectors that typically wrap the seller/dealer info block
    container_selectors = [
        "[class*='seller-info']", "[class*='dealer-info']", "[class*='store-info']",
        "[class*='seller-detail']", "[class*='dealer-detail']",
        "[class*='listing-seller']", "[class*='item-seller']",
        "[data-testid='seller-info']", "[data-testid='dealer-info']",
    ]
    try:
        for sel in container_selectors:
            try:
                container = sb.find_element(sel)
                text = container.text if hasattr(container, 'text') else ""
                match = re.search(r'\b([A-Z][a-zA-Z\s]{2,20}),\s*([A-Z]{2})\b', text)
                if match:
                    city, state = match.group(1).strip(), match.group(2)
                    if state in _US_STATES and len(city) >= 3:
                        return city, state
            except Exception:
                continue
    except Exception:
        pass
    return "", ""


_US_STATES = {
    "AL","AK","AZ","AR","CA","CO","CT","DE","FL","GA","HI","ID","IL","IN","IA",
    "KS","KY","LA","ME","MD","MA","MI","MN","MS","MO","MT","NE","NV","NH","NJ",
    "NM","NY","NC","ND","OH","OK","OR","PA","RI","SC","SD","TN","TX","UT","VT",
    "VA","WA","WV","WI","WY","DC",
}


def _extract_dealer_display_name(sb, default_name: str) -> str:
    """Try to find the dealer's display name on the product page."""
    for sel in [
        "[class*='seller-name']", "[class*='dealer-name']", "[class*='store-name']",
        "[class*='store-info'] h1", "[class*='store-info'] h2",
        "[class*='dealer-info'] h1", "[class*='dealer-info'] h2",
        "[data-testid='seller-name']", "[data-testid='dealer-name']",
    ]:
        try:
            el = sb.find_element(sel)
            text = el.text.strip()
            if text and len(text) < 120:
                return text
        except Exception:
            continue
    return default_name


def scrape_dealer_profile(sb, dealer_name: str, profile_url: str) -> dict:
    """
    Get one product listing for this dealer, visit the product page,
    and extract all available contact/location info.

    Returns a dict with keys:
      display_name, address, city, state, phone, email, website,
      dealer_rating, review_count, sample_listing_url
    """
    result = {
        "display_name": dealer_name,
        "address": "",
        "city": "",
        "state": "",
        "phone": "",
        "email": "",
        "website": "",
        "dealer_rating": "",
        "review_count": "",
        "sample_listing_url": "",
    }

    # Step 1: get a sample listing URL via the catalog API
    listing_url = get_sample_listing_url(sb, dealer_name)
    if not listing_url:
        return result

    result["sample_listing_url"] = listing_url

    # Step 2: visit the product page
    ok = safe_get(sb, listing_url)
    if not ok:
        return result
    time.sleep(2)

    # Step 3: extract dealer info from the page
    result["display_name"] = _extract_dealer_display_name(sb, dealer_name)
    result["phone"] = _extract_phone(sb)
    result["email"] = _extract_email(sb)
    result["website"] = _extract_website(sb)
    result["dealer_rating"], result["review_count"] = _extract_rating(sb)

    # Address field
    result["address"] = _try_text(sb, [
        "[class*='address']", "[itemprop='streetAddress']",
        "[class*='location']", "[data-testid='dealer-address']",
        "[class*='store-info'] address", "[class*='dealer-address']",
    ])

    # City + state (only from dealer-specific sections, not from page-wide scan)
    if not result["city"]:
        city, state = _extract_location_from_seller_section(sb)
        result["city"] = city
        result["state"] = state

    return result
