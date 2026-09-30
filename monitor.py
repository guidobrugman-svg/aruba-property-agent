import json
import os
import re
import time
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin, urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None


# ============================================================
# CONFIGURATION
# ============================================================

RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RECIPIENT = "guidobrugman@live.nl"
SENDER = "Aruba Property Agent <alerts@arubapropertywatch.com>"

STATE_FILE = "state.json"
SOURCES_FILE = "SOURCES.json"

PRICE_LIMIT = 650000
BATCH_MINUTES = 30

ARUBA_TZ = (
    ZoneInfo("America/Aruba")
    if ZoneInfo
    else timezone(timedelta(hours=-4))
)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (X11; Linux x86_64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/128.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Cache-Control": "no-cache",
    "Pragma": "no-cache",
}

EXCLUDED_STATUS = (
    "sold",
    "under contract",
    "sale in progress",
    "withdrawn",
    "unavailable",
    "off market",
    "off-market",
    "rented",
)

EXCLUDED_TYPES = (
    "commercial",
    "office",
    "warehouse",
    "retail",
    "store",
    "hospitality",
    "restaurant",
    "hotel",
)

EXCLUDED_RESIDENTIAL_UNIT_TYPES = (
    "Apartment",
    "Condominium",
)

DIRECT_SOURCE_NAMES = {
    "Aruba Brokers",
    "Ben Real Estate",
    "RE/MAX Aruba",
    "RES Aruba Realty",
    "Aruba Happy Realty",
    "Aruba Palms Realtors",
    "Home 4 Everyone",
    "Smiley Real Estate",
}

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


def aruba_now():
    return datetime.now(ARUBA_TZ)


def clean_text(value):
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def normalize(value):
    value = clean_text(value).lower()
    value = re.sub(r"[^\w\s]", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def html_escape(value):
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def absolute_url(base_url, href):
    if not href:
        return ""
    return urljoin(base_url, href)


def source_priority(source_name):
    if source_name in DIRECT_SOURCE_NAMES:
        return 0
    if source_name == "Bluefin Realtors":
        return 1
    return 2


def looks_like_url(value):
    try:
        parsed = urlparse(value)
        return parsed.scheme in ("http", "https") and bool(parsed.netloc)
    except Exception:
        return False


# ============================================================
# HTTP / SOURCE ACCESS
# ============================================================

def get_page(url):
    """
    Fetch a public listing page.

    Normal HTTPS is always preferred. Some Aruba websites have
    certificate-chain or hostname issues, so a verify=False fallback
    is attempted only after a normal SSL request fails.

    403/404/etc. remain failures and are reported to the source-health
    system rather than being interpreted as "zero listings".
    """

    last_error = None

    for attempt in range(3):
        try:
            response = session.get(
                url,
                timeout=25,
                allow_redirects=True,
            )

            response.raise_for_status()
            return response.text, response.url

        except requests.exceptions.SSLError as exc:
            last_error = exc

            try:
                response = session.get(
                    url,
                    timeout=25,
                    allow_redirects=True,
                    verify=False,
                )

                response.raise_for_status()

                print(
                    "  SSL fallback succeeded with certificate "
                    "verification disabled."
                )

                return response.text, response.url

            except Exception as fallback_error:
                last_error = fallback_error

        except requests.RequestException as exc:
            last_error = exc

            if attempt < 2:
                time.sleep(1.5 * (attempt + 1))

    raise last_error


# ============================================================
# PRICE PARSING
# ============================================================

def parse_number(value):
    value = clean_text(value)

    if not value:
        return None

    value = value.replace(",", "")

    try:
        return float(value)
    except ValueError:
        return None


def extract_area_m2(text):
    """
    Prefer larger/lot-style square-metre values when several areas
    are present. This is useful for land sold at a price per m².
    """

    text = clean_text(text)

    values = []

    for match in re.finditer(
        r"\b([\d,.]+)\s*(?:m²|m2|sqm|sq\.?\s*m)\b",
        text,
        re.IGNORECASE,
    ):
        number = parse_number(match.group(1))

        if number and number > 0:
            values.append(number)

    if not values:
        return None

    return max(values)


def extract_per_m2_rate(text):
    """
    Detect prices such as:
      $98/M2
      US$ 98 per m²
      USD 98 / sqm
    """

    patterns = [
        (
            r"(?:USD|US\$|\$)\s*([\d,]+(?:\.\d+)?)"
            r"\s*(?:/|per\s+)"
            r"(?:m²|m2|sqm|sq\.?\s*m)\b"
        ),
        (
            r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$|\$)"
            r"\s*(?:/|per\s+)"
            r"(?:m²|m2|sqm|sq\.?\s*m)\b"
        ),
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return parse_number(match.group(1))

    return None


def parse_price_details(text):
    """
    Returns:
        (price, metadata)

    Per-square-metre prices are never mistaken for total asking
    prices. If a reliable m² area is available, a calculated total is
    produced and marked as calculated.
    """

    text = clean_text(text)

    if not text:
        return None, {}

    per_m2_rate = extract_per_m2_rate(text)

    if per_m2_rate:
        area = extract_area_m2(text)

        if area:
            total = per_m2_rate * area

            return total, {
                "calculated_price": True,
                "price_per_m2": per_m2_rate,
                "price_area_m2": area,
            }

        # Never treat the rate itself as the total asking price.
        return None, {
            "calculated_price": False,
            "price_per_m2": per_m2_rate,
        }

    patterns = [
        r"(?:USD|US\$)\s*([\d,]+(?:\.\d+)?)",
        r"\$\s*([\d,]+(?:\.\d+)?)",
        r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$)",
    ]

    for pattern in patterns:
        for match in re.finditer(
            pattern,
            text,
            re.IGNORECASE,
        ):
            start = max(0, match.start() - 8)
            end = min(len(text), match.end() + 20)
            context = text[start:end]

            # Ignore a dollar value if it is clearly a rate.
            if re.search(
                r"(?:/|per\s+)\s*"
                r"(?:m²|m2|sqm|sq\.?\s*m)",
                context,
                re.IGNORECASE,
            ):
                continue

            value = parse_number(match.group(1))

            if value is not None:
                return value, {}

    # Do not accidentally interpret pure AWG as USD.
    if (
        re.search(r"\bAWG\b", text, re.IGNORECASE)
        and not re.search(
            r"\bUSD\b|US\$|\$",
            text,
            re.IGNORECASE,
        )
    ):
        return None, {}

    return None, {}


def parse_price(text):
    price, _ = parse_price_details(text)
    return price


# ============================================================
# LISTING / RENTAL FILTERING
# ============================================================

def has_explicit_rental_status(text):
    """
    Reject explicit rental listings while avoiding false positives
    such as "excellent rental income" on a property that is for sale.
    """

    raw = clean_text(text)
    lower = raw.lower()

    strong_patterns = [
        r"\bfor\s+rent\b",
        r"\bfor\s+rental\b",
        r"\bavailable\s+for\s+rent\b",
        r"\blong[\s-]?term\s+rent(?:al)?\b",
        r"\bshort[\s-]?term\s+rent(?:al)?\b",
        r"\bvacation\s+rental\b",
        r"\bmonthly\s+rent\b",
        r"\brent\s*:\s*(?:USD|US\$|\$)",
        r"(?:USD|US\$|\$)\s*[\d,.]+\s*/\s*month\b",
        r"(?:USD|US\$|\$)\s*[\d,.]+\s*per\s+month\b",
    ]

    for pattern in strong_patterns:
        if re.search(
            pattern,
            raw,
            re.IGNORECASE,
        ):
            # A listing can mention both sale and rental. If it has a
            # clear sale status and a plausible sale asking price, do
            # not automatically reject it.
            if re.search(
                r"\bfor\s+sale\b",
                raw,
                re.IGNORECASE,
            ):
                price, _ = parse_price_details(raw)

                if price and price >= 10000:
                    continue

            return True

    # Very low dollar prices combined with rent language are almost
    # certainly monthly rental listings.
    price, _ = parse_price_details(raw)

    if (
        price
        and price < 10000
        and re.search(
            r"\brent(?:al|ed|ing)?\b",
            lower,
            re.IGNORECASE,
        )
    ):
        return True

    return False


def extract_status(text):
    lower = normalize(text)

    for status in EXCLUDED_STATUS:
        if status in lower:
            return status

    if has_explicit_rental_status(text):
        return "for rent"

    return ""


# ============================================================
# PROPERTY TYPE INFERENCE
# ============================================================

def infer_property_type(title, text):
    """
    Title-first classification.

    This deliberately avoids allowing generic template phrases such
    as "Land Details" to turn an obvious house into Land.
    """

    title_n = normalize(title)
    text_n = normalize(text)

    # --------------------------------------------------------
    # Strong title signals
    # --------------------------------------------------------

    if re.search(
        r"\b("
        r"apartment complex|apartment building|"
        r"multi unit|multiunit|multi family|multifamily|"
        r"income property|investment property"
        r")\b",
        title_n,
    ):
        return "Apartment Complex"

    if re.search(
        r"\b("
        r"land|lot|parcel|plot|terrein|building lot"
        r")\b",
        title_n,
    ):
        return "Land"

    if re.search(
        r"\b(townhouse|townhome|town house|town home)\b",
        title_n,
    ):
        return "Townhouse"

    if re.search(
        r"\bvilla\b",
        title_n,
    ):
        return "Villa"

    if re.search(
        r"\b("
        r"house|home|residence|residential home|"
        r"family home|fixer upper|fixer-upper"
        r")\b",
        title_n,
    ):
        return "House"

    if re.search(
        r"\b("
        r"condominium|condo|apartment|studio"
        r")\b",
        title_n,
    ):
        return "Condominium" if (
            "condo" in title_n
            or "condominium" in title_n
        ) else "Apartment"

    if re.search(
        r"\b("
        r"development|project|new construction|"
        r"pre construction|pre-construction"
        r")\b",
        title_n,
    ):
        return "Development"

    # --------------------------------------------------------
    # Strong combined-text signals
    # --------------------------------------------------------

    combined = f"{title_n} {text_n}"

    if re.search(
        r"\b("
        r"apartment complex|apartment building|"
        r"multi unit|multiunit|multi family|multifamily"
        r")\b",
        combined,
    ):
        return "Apartment Complex"

    # Beds/baths plus residential language strongly suggests a house.
    has_beds = bool(
        re.search(
            r"\b\d+\s*(?:bed|beds|bedroom|bedrooms|bd|bdr)\b",
            text_n,
        )
    )

    has_baths = bool(
        re.search(
            r"\b\d+(?:\.\d+)?\s*"
            r"(?:bath|baths|bathroom|bathrooms|ba)\b",
            text_n,
        )
    )

    if (
        has_beds
        and has_baths
        and re.search(
            r"\b("
            r"home|house|residence|residential|"
            r"family property"
            r")\b",
            combined,
        )
    ):
        return "House"

    if re.search(
        r"\b(townhouse|townhome|town house|town home)\b",
        combined,
    ):
        return "Townhouse"

    if re.search(
        r"\bvilla\b",
        combined,
    ):
        return "Villa"

    # Only infer Land from body text when the evidence is specific.
    if re.search(
        r"\b("
        r"vacant land|vacant lot|land lot|building lot|"
        r"parcel of land|plot of land|lot for sale|"
        r"land for sale"
        r")\b",
        combined,
    ):
        return "Land"

    if re.search(
        r"\b("
        r"condominium|condo unit|apartment unit|"
        r"apartment for sale"
        r")\b",
        combined,
    ):
        return "Condominium" if (
            "condominium" in combined
            or "condo" in combined
        ) else "Apartment"

    if re.search(
        r"\b("
        r"development|new construction|pre construction|"
        r"pre-construction"
        r")\b",
        combined,
    ):
        return "Development"

    return ""


def extract_type(text, title=""):
    return infer_property_type(
        title,
        text,
    )


# ============================================================
# DETAIL EXTRACTION
# ============================================================

def first_match(patterns, text):
    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE,
        )

        if match:
            return clean_text(match.group(1))

    return ""


def extract_details(title, text):
    beds = first_match(
        [
            r"\b(\d+)\s*"
            r"(?:bed|beds|bedroom|bedrooms|bd|bdr)\b",
        ],
        text,
    )

    baths = first_match(
        [
            r"\b(\d+(?:\.\d+)?)\s*"
            r"(?:bath|baths|bathroom|bathrooms|ba)\b",
        ],
        text,
    )

    size_match = re.search(
        r"\b([\d,.]+)\s*(m²|m2|sqm|sq\s*ft|sqft|ft²)\b",
        text,
        re.IGNORECASE,
    )

    size = ""

    if size_match:
        size = (
            clean_text(size_match.group(1))
            + " "
            + clean_text(size_match.group(2))
        )

    location = first_match(
        [
            r"\b(?:location|area|neighborhood|neighbourhood)"
            r"\s*[:\-]\s*([^|•\n]+)",
        ],
        text,
    )

    if not location:
        known_areas = [
            "Noord",
            "Oranjestad",
            "Palm Beach",
            "Eagle Beach",
            "Bakval",
            "Malmok",
            "Tanki Leendert",
            "Tanki Flip",
            "Tankiflip",
            "Santa Cruz",
            "Paradera",
            "Savaneta",
            "Pos Chiquito",
            "Ponton",
            "Bubali",
            "Wayaca",
            "Alto Vista",
            "Westpunt",
            "San Nicolas",
            "Hooiberg",
            "Sabana Grandi",
            "Sabana Basora",
            "Rooi Taki",
            "Pavia",
            "Rooi Santo",
            "Rooi Kooshi",
            "Seroe Pita",
            "Tierra del Sol",
            "Papaya",
            "Morgenster",
            "Modanza",
            "Matadera",
            "Moko",
            "Washington",
            "Seroe Janchi",
            "Catashi",
            "Cunucu Abao",
            "Palm Beach",
            "Shaba",
            "Kudawecha",
            "Sabana Liber",
            "Balashi",
            "Barcadera",
        ]

        combined = f"{title} {text}".lower()

        for area in known_areas:
            if area.lower() in combined:
                location = area
                break

    return beds, baths, size, location


def image_from(node, base_url):
    if not node:
        return ""

    image = node.find("img")

    if not image:
        return ""

    for attribute in (
        "src",
        "data-src",
        "data-lazy-src",
        "data-original",
    ):
        value = image.get(attribute)

        if value:
            return absolute_url(
                base_url,
                value,
            )

    srcset = image.get("srcset")

    if srcset:
        first = (
            srcset
            .split(",")[0]
            .strip()
            .split(" ")[0]
        )

        return absolute_url(
            base_url,
            first,
        )

    return ""


def looks_like_property_title(title):
    title = clean_text(title)
    normalized = normalize(title)

    if not normalized:
        return False

    if normalized in {
        "details",
        "view",
        "view details",
        "read more",
        "learn more",
        "property",
        "properties",
        "for sale",
        "sale",
    }:
        return False

    if len(title) < 5:
        return False

    junk = (
        "lorem ipsum",
        "test listing",
        "sample property",
        "asdfsaf",
        "privacy policy",
        "terms conditions",
        "contact us",
        "about us",
        "our team",
    )

    return not any(
        item in normalized
        for item in junk
    )


def make_description(text, title):
    text = clean_text(text)

    if title:
        text = text.replace(
            title,
            " ",
            1,
        )

    text = re.sub(
        r"\bview details\b",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = clean_text(text)

    if len(text) > 360:
        text = (
            text[:357]
            .rsplit(" ", 1)[0]
            + "..."
        )

    return text


# ============================================================
# PROPERTY BUILDING
# ============================================================

def build_property(
    source,
    title,
    url,
    price,
    text,
    image="",
    price_meta=None,
):
    title = clean_text(title)
    text = clean_text(text)
    price_meta = price_meta or {}

    if not looks_like_property_title(title):
        return None

    if has_explicit_rental_status(
        f"{title} {text}"
    ):
        return None

    if price is None:
        return None

    if price <= 0 or price > PRICE_LIMIT:
        return None

    status = extract_status(
        f"{title} {text}"
    )

    if status:
        return None

    beds, baths, size, location = extract_details(
        title,
        text,
    )

    property_type = infer_property_type(
        title,
        text,
    )

    # Individual apartments and condominiums are intentionally
    # excluded. Whole apartment complexes / multi-unit investment
    # properties remain eligible.
    if property_type in EXCLUDED_RESIDENTIAL_UNIT_TYPES:
        return None

    combined = normalize(
        f"{title} {property_type} {text}"
    )

    # Do not reject residential developments simply because the
    # description happens to mention a nearby hotel/resort. Strong
    # commercial signals are required.
    strong_commercial = (
        property_type in {
            "Commercial",
            "Office",
            "Warehouse",
            "Retail",
            "Hotel",
        }
        or any(
            re.search(
                rf"\b{re.escape(excluded)}\s+"
                r"(?:building|property|space|unit|for sale)\b",
                combined,
            )
            for excluded in EXCLUDED_TYPES
        )
    )

    if strong_commercial:
        return None

    mls = first_match(
        [
            r"\bMLS\s*[:#]?\s*([A-Z0-9\-]+)\b",
            r"\b(AW\d{6,})\b",
        ],
        text,
    )

    result = {
        "name": title,
        "url": url,
        "price": round(price, 2),
        "location": location,
        "type": property_type or "Property",
        "beds": beds,
        "baths": baths,
        "size": size,
        "image": image,
        "description": make_description(
            text,
            title,
        ),
        "source": source,
        "mls": mls,
        "status": "available",
        "detected_at": iso_now(),
    }

    if price_meta.get("calculated_price"):
        result["calculated_price"] = True
        result["price_per_m2"] = price_meta.get(
            "price_per_m2"
        )
        result["price_area_m2"] = price_meta.get(
            "price_area_m2"
        )

    return result


# ============================================================
# SCRAPING HELPERS
# ============================================================

def get_card_nodes(soup):
    selectors = [
        "article",
        ".property",
        ".property-item",
        ".property-card",
        ".listing",
        ".listing-item",
        ".property-listing",
        ".item-property",
        ".property-box",
        ".property-list",
        ".listing-box",
        ".listing-card",
        ".estate",
        ".real-estate",
        "[class*='property-card']",
        "[class*='listing-card']",
        "[class*='property-item']",
        "[class*='listing-item']",
    ]

    nodes = []
    seen = set()

    for selector in selectors:
        for node in soup.select(selector):
            marker = id(node)

            if marker not in seen:
                seen.add(marker)
                nodes.append(node)

    return nodes


def best_listing_link(node, base_url):
    links = node.find_all(
        "a",
        href=True,
    )

    best = ""

    for link in links:
        href = absolute_url(
            base_url,
            link.get("href"),
        )

        if not href:
            continue

        href_n = normalize(href)

        if any(
            word in href_n
            for word in (
                "property",
                "listing",
                "real estate",
                "for sale",
                "sale",
            )
        ):
            return href

        if not best:
            best = href

    return best


def best_title_from_node(node):
    for tag in (
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
    ):
        for heading in node.find_all(tag):
            candidate = clean_text(
                heading.get_text(
                    " ",
                    strip=True,
                )
            )

            if looks_like_property_title(
                candidate
            ):
                return candidate

    for link in node.find_all(
        "a",
        href=True,
    ):
        candidate = clean_text(
            link.get_text(
                " ",
                strip=True,
            )
        )

        if looks_like_property_title(
            candidate
        ):
            return candidate

    return ""


def extract_property_from_node(
    source,
    node,
    base_url,
):
    text = clean_text(
        node.get_text(
            " ",
            strip=True,
        )
    )

    if not text:
        return None

    title = best_title_from_node(node)

    if not title:
        return None

    price, price_meta = parse_price_details(
        text
    )

    if price is None:
        return None

    property_url = best_listing_link(
        node,
        base_url,
    )

    if not property_url:
        property_url = base_url

    return build_property(
        source=source,
        title=title,
        url=property_url,
        price=price,
        text=text,
        image=image_from(
            node,
            base_url,
        ),
        price_meta=price_meta,
    )


def scrape_page_cards(
    source,
    page_url,
):
    html, final_url = get_page(
        page_url
    )

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    nodes = get_card_nodes(soup)

    if not nodes:
        nodes = soup.find_all(
            ["article", "li", "div"],
            limit=1500,
        )

    properties = []

    for node in nodes:
        prop = extract_property_from_node(
            source,
            node,
            final_url,
        )

        if prop:
            properties.append(prop)

    return dedupe_properties(properties)


def page_candidates(
    url,
    max_pages=5,
):
    """
    Generate common pagination forms used by Aruba broker sites.
    Failed secondary pages are harmless; the main source page remains
    authoritative for source-health reporting.
    """

    result = [url]
    base = url.rstrip("/") + "/"

    for page_number in range(
        2,
        max_pages + 1,
    ):
        result.extend(
            [
                f"{base}page/{page_number}/",
                f"{url}?page={page_number}",
                f"{url}?paged={page_number}",
            ]
        )

    unique = []
    seen = set()

    for candidate in result:
        if candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)

    return unique


def scrape_paginated_generic(
    source,
    url,
    max_pages=5,
):
    all_properties = []
    successful_pages = 0

    for index, page_url in enumerate(
        page_candidates(
            url,
            max_pages=max_pages,
        )
    ):
        try:
            properties = scrape_page_cards(
                source,
                page_url,
            )

            successful_pages += 1

            if properties:
                print(
                    f"  Page {page_url}: "
                    f"{len(properties)} qualifying"
                )

                all_properties.extend(
                    properties
                )

        except Exception as error:
            # The first/main URL must work. Pagination guesses are
            # optional and should not fail the whole source.
            if index == 0:
                raise

            continue

    if successful_pages == 0:
        return []

    return dedupe_properties(
        all_properties
    )


# ============================================================
# BLUEFIN
# ============================================================

def scrape_bluefin(source, url):
    """
    Bluefin is a consolidated discovery source.

    Scan several listing pages because relying only on the first page
    can miss newly-added or lower-ranked inventory.
    """

    properties = []
    seen_urls = set()

    page_urls = [url]
    clean_base = url.rstrip("/") + "/"

    for page_number in range(2, 7):
        page_urls.append(
            f"{clean_base}page/{page_number}/"
        )

    for page_url in page_urls:
        try:
            html, final_url = get_page(
                page_url
            )
        except Exception:
            if page_url == url:
                raise
            continue

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        heading_links = []

        for heading in soup.find_all(
            ["h1", "h2", "h3", "h4", "h5"]
        ):
            link = heading.find(
                "a",
                href=True,
            )

            if not link:
                continue

            title = clean_text(
                link.get_text(
                    " ",
                    strip=True,
                )
            )

            if not looks_like_property_title(
                title
            ):
                continue

            href = absolute_url(
                final_url,
                link.get("href"),
            )

            if (
                not href
                or href in seen_urls
            ):
                continue

            heading_links.append(
                (
                    heading,
                    title,
                    href,
                )
            )

        page_properties = 0

        for heading, title, property_url in heading_links:
            seen_urls.add(property_url)

            container = heading
            text = ""

            for _ in range(9):
                if not container:
                    break

                candidate_text = clean_text(
                    container.get_text(
                        " ",
                        strip=True,
                    )
                )

                price, _ = parse_price_details(
                    candidate_text
                )

                if price is not None:
                    text = candidate_text
                    break

                container = container.parent

            if not text:
                continue

            price, price_meta = parse_price_details(
                text
            )

            if price is None:
                continue

            prop = build_property(
                source=source,
                title=title,
                url=property_url,
                price=price,
                text=text,
                image=image_from(
                    container,
                    final_url,
                ),
                price_meta=price_meta,
            )

            if prop:
                properties.append(prop)
                page_properties += 1

        print(
            f"  Bluefin page {page_url}: "
            f"{page_properties} qualifying listings"
        )

    return dedupe_properties(
        properties
    )


# ============================================================
# LINK-DRIVEN SCRAPER
# ============================================================

def scrape_listing_links(
    source,
    url,
    max_pages=6,
):
    """
    Some broker websites do not use conventional property-card CSS.
    This parser finds links surrounded by price/property information
    instead.

    It is intentionally conservative: a link must have a plausible
    title and nearby dollar price before becoming a listing.
    """

    all_properties = []
    seen_urls = set()

    pages = page_candidates(
        url,
        max_pages=max_pages,
    )

    main_success = False

    for index, page_url in enumerate(pages):
        try:
            html, final_url = get_page(
                page_url
            )

            if index == 0:
                main_success = True

        except Exception:
            if index == 0:
                raise
            continue

        soup = BeautifulSoup(
            html,
            "html.parser",
        )

        page_count = 0

        for link in soup.find_all(
            "a",
            href=True,
        ):
            href = absolute_url(
                final_url,
                link.get("href"),
            )

            if (
                not href
                or href in seen_urls
            ):
                continue

            title = clean_text(
                link.get_text(
                    " ",
                    strip=True,
                )
            )

            if not looks_like_property_title(
                title
            ):
                # A linked image can sit inside a property card while
                # the heading is a sibling. Search a nearby container.
                parent = link.parent

                if parent:
                    candidate = best_title_from_node(
                        parent
                    )

                    if candidate:
                        title = candidate

            if not looks_like_property_title(
                title
            ):
                continue

            container = link
            text = ""

            for _ in range(7):
                if not container:
                    break

                candidate_text = clean_text(
                    container.get_text(
                        " ",
                        strip=True,
                    )
                )

                price, _ = parse_price_details(
                    candidate_text
                )

                if price is not None:
                    text = candidate_text
                    break

                container = container.parent

            if not text:
                continue

            price, price_meta = parse_price_details(
                text
            )

            if price is None:
                continue

            prop = build_property(
                source=source,
                title=title,
                url=href,
                price=price,
                text=text,
                image=image_from(
                    container,
                    final_url,
                ),
                price_meta=price_meta,
            )

            if prop:
                seen_urls.add(href)
                all_properties.append(prop)
                page_count += 1

        if page_count:
            print(
                f"  Link scan {page_url}: "
                f"{page_count} qualifying"
            )

    if not main_success:
        return []

    return dedupe_properties(
        all_properties
    )


# ============================================================
# SOURCE-SPECIFIC ROUTING
# ============================================================

def combine_scraper_results(*groups):
    combined = []

    for group in groups:
        combined.extend(
            group or []
        )

    return dedupe_properties(
        combined
    )


def scrape_robust_direct(
    source,
    url,
    max_pages=6,
):
    """
    Use both card-driven and link-driven extraction. This costs a few
    extra requests but materially improves coverage across broker
    websites with different WordPress/themes.
    """

    card_results = []
    link_results = []
    card_error = None
    link_error = None

    try:
        card_results = scrape_paginated_generic(
            source,
            url,
            max_pages=max_pages,
        )
    except Exception as error:
        card_error = error

    try:
        link_results = scrape_listing_links(
            source,
            url,
            max_pages=max_pages,
        )
    except Exception as error:
        link_error = error

    results = combine_scraper_results(
        card_results,
        link_results,
    )

    if results:
        return results

    # If the site was reachable but no qualifying properties were
    # extracted, return zero. If both extraction methods could not
    # reach the source at all, report the failure.
    if card_error and link_error:
        raise card_error

    return []


def scrape_source(source):
    name = source["name"]
    url = source["url"]

    if name == "Bluefin Realtors":
        return scrape_bluefin(
            name,
            url,
        )

    # Direct brokers get the broader extraction strategy.
    if source.get("type") == "direct_broker":
        return scrape_robust_direct(
            name,
            url,
            max_pages=6,
        )

    # Aggregators other than Bluefin.
    return scrape_robust_direct(
        name,
        url,
        max_pages=5,
    )


# ============================================================
# DEDUPLICATION
# ============================================================

def normalized_title_for_match(title):
    value = normalize(title)

    value = re.sub(
        r"\b(?:for sale|sale|aruba)\b",
        " ",
        value,
    )

    value = re.sub(
        r"\b(?:property|real estate)\b",
        " ",
        value,
    )

    return re.sub(
        r"\s+",
        " ",
        value,
    ).strip()


def canonical_text(prop):
    values = [
        normalized_title_for_match(
            prop.get("name", "")
        ),
        normalize(
            prop.get("location", "")
        ),
        normalize(
            prop.get("mls", "")
        ),
    ]

    return clean_text(
        " ".join(values)
    )


def stable_identity_text(prop):
    """
    Stable identity intentionally does NOT include inferred property
    type. This prevents classifier improvements from turning an
    already-known listing into a false NEW alert.
    """

    mls = normalize(
        prop.get("mls", "")
    )

    if mls:
        return f"mls:{mls}"

    title = normalized_title_for_match(
        prop.get("name", "")
    )

    location = normalize(
        prop.get("location", "")
    )

    if title:
        return f"title:{title}|location:{location}"

    url = clean_text(
        prop.get("url", "")
    )

    return f"url:{url}"


def property_key(prop):
    identity = stable_identity_text(
        prop
    )

    return hashlib.sha1(
        identity.encode("utf-8")
    ).hexdigest()


def legacy_property_key(prop):
    """
    Key compatible with the previous monitor version.

    Used only to find existing state records after this upgrade so
    historical listings are not re-alerted.
    """

    mls = normalize(
        prop.get("mls", "")
    )

    if mls:
        return f"mls:{mls}"

    values = [
        prop.get("name", ""),
        prop.get("location", ""),
        prop.get("type", ""),
        prop.get("mls", ""),
    ]

    canonical = normalize(
        " ".join(values)
    )

    if not canonical:
        canonical = prop.get(
            "url",
            "",
        )

    return hashlib.sha1(
        canonical.encode("utf-8")
    ).hexdigest()


def fuzzy_identity(prop):
    title = normalized_title_for_match(
        prop.get("name", "")
    )

    location = normalize(
        prop.get("location", "")
    )

    # Remove common unit/lot numbers for cross-source matching.
    title = re.sub(
        r"\b(?:unit|apt|apartment|condo|lot|phase)"
        r"\s*[-#]?\s*[a-z0-9]+\b",
        " ",
        title,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

    return f"{location}|{title}"


def development_key(prop):
    title = normalized_title_for_match(
        prop.get("name", "")
    )

    location = normalize(
        prop.get("location", "")
    )

    title = re.sub(
        r"\b(?:unit|apt|apartment|condo|villa|lot|phase)"
        r"\s*[-#]?\s*[a-z0-9]+\b",
        "",
        title,
    )

    title = re.sub(
        r"\b\d{1,4}[a-z]?\b",
        "",
        title,
    )

    title = re.sub(
        r"\s+",
        " ",
        title,
    ).strip()

    if len(title) < 10:
        return ""

    return f"{location}|{title}"


def merge_property_data(
    preferred,
    alternate,
):
    """
    Keep the preferred listing/source but enrich missing fields from
    another source.
    """

    result = dict(preferred)

    for field in (
        "image",
        "description",
        "location",
        "beds",
        "baths",
        "size",
        "mls",
        "type",
    ):
        if (
            not result.get(field)
            and alternate.get(field)
        ):
            result[field] = alternate[field]

    return result


def choose_preferred_property(a, b):
    a_rank = source_priority(
        a.get("source", "")
    )
    b_rank = source_priority(
        b.get("source", "")
    )

    if b_rank < a_rank:
        return merge_property_data(
            b,
            a,
        )

    if a_rank < b_rank:
        return merge_property_data(
            a,
            b,
        )

    # Same source priority: prefer the richer record.
    def richness(prop):
        return sum(
            bool(prop.get(field))
            for field in (
                "image",
                "description",
                "location",
                "beds",
                "baths",
                "size",
                "mls",
            )
        )

    if richness(b) > richness(a):
        return merge_property_data(
            b,
            a,
        )

    return merge_property_data(
        a,
        b,
    )


def dedupe_properties(properties):
    unique = {}

    for prop in properties:
        key = property_key(prop)

        if key not in unique:
            unique[key] = prop
        else:
            unique[key] = choose_preferred_property(
                unique[key],
                prop,
            )

    return list(
        unique.values()
    )


def cross_source_dedupe(properties):
    """
    Prefer direct broker records over aggregators and collapse obvious
    duplicate development/unit records.
    """

    result = []
    identity_indexes = {}
    fuzzy_indexes = {}
    development_indexes = {}

    ordered = sorted(
        properties,
        key=lambda p: (
            source_priority(
                p.get("source", "")
            ),
            p.get("name", ""),
        ),
    )

    for prop in ordered:
        identity = property_key(prop)

        if identity in identity_indexes:
            index = identity_indexes[
                identity
            ]

            result[index] = choose_preferred_property(
                result[index],
                prop,
            )

            continue

        fuzzy = fuzzy_identity(prop)

        if (
            fuzzy
            and fuzzy in fuzzy_indexes
        ):
            index = fuzzy_indexes[fuzzy]
            existing = result[index]

            existing_price = existing.get(
                "price",
                0,
            )

            new_price = prop.get(
                "price",
                0,
            )

            similar_price = (
                not existing_price
                or not new_price
                or abs(
                    existing_price
                    - new_price
                )
                <= max(
                    10000,
                    existing_price * 0.05,
                )
            )

            if similar_price:
                result[index] = choose_preferred_property(
                    existing,
                    prop,
                )
                continue

        dev_key = development_key(
            prop
        )

        if dev_key:
            existing_index = (
                development_indexes.get(
                    dev_key
                )
            )

            if existing_index is not None:
                existing = result[
                    existing_index
                ]

                existing_price = existing.get(
                    "price",
                    0,
                )

                new_price = prop.get(
                    "price",
                    0,
                )

                similar_price = (
                    abs(
                        existing_price
                        - new_price
                    )
                    <= max(
                        5000,
                        existing_price * 0.03,
                    )
                )

                if similar_price:
                    result[
                        existing_index
                    ] = choose_preferred_property(
                        existing,
                        prop,
                    )

                    continue

            development_indexes[
                dev_key
            ] = len(result)

        identity_indexes[
            identity
        ] = len(result)

        if fuzzy:
            fuzzy_indexes[
                fuzzy
            ] = len(result)

        result.append(prop)

    return result


# ============================================================
# STATE
# ============================================================

def load_json(path, default):
    try:
        with open(
            path,
            "r",
            encoding="utf-8",
        ) as file:
            return json.load(file)

    except (
        FileNotFoundError,
        json.JSONDecodeError,
    ):
        return default


def save_json(path, data):
    with open(
        path,
        "w",
        encoding="utf-8",
    ) as file:
        json.dump(
            data,
            file,
            indent=2,
            ensure_ascii=False,
        )


def previous_properties(state):
    if (
        isinstance(state, dict)
        and isinstance(
            state.get("properties"),
            dict,
        )
    ):
        return state["properties"]

    if isinstance(state, dict):
        return state

    return {}


def parse_datetime(value):
    try:
        return datetime.fromisoformat(
            value.replace(
                "Z",
                "+00:00",
            )
        )
    except Exception:
        return None


def find_previous_record(
    previous,
    prop,
):
    """
    Find an existing property using both the new stable identity and
    the previous monitor's key format.
    """

    new_key = property_key(prop)

    if new_key in previous:
        return new_key, previous[new_key]

    old_key = legacy_property_key(
        prop
    )

    if old_key in previous:
        return old_key, previous[old_key]

    mls = normalize(
        prop.get("mls", "")
    )

    title = normalized_title_for_match(
        prop.get("name", "")
    )

    url = clean_text(
        prop.get("url", "")
    )

    for key, candidate in previous.items():
        candidate_mls = normalize(
            candidate.get("mls", "")
        )

        if (
            mls
            and candidate_mls
            and mls == candidate_mls
        ):
            return key, candidate

        candidate_title = (
            normalized_title_for_match(
                candidate.get(
                    "name",
                    ""
                )
            )
        )

        if (
            title
            and candidate_title
            and title == candidate_title
        ):
            return key, candidate

        candidate_url = clean_text(
            candidate.get(
                "url",
                ""
            )
        )

        if (
            url
            and candidate_url
            and url == candidate_url
        ):
            return key, candidate

    return None, None


# ============================================================
# CHANGE DETECTION
# ============================================================

def field_changes(old, new):
    changes = {}

    fields = [
        ("beds", "Bedrooms"),
        ("baths", "Bathrooms"),
        ("size", "Size"),
        ("location", "Location"),
        ("type", "Property type"),
        ("status", "Status"),
    ]

    for field, label in fields:
        before = clean_text(
            old.get(field, "")
        )

        after = clean_text(
            new.get(field, "")
        )

        if (
            before
            and after
            and before != after
        ):
            changes[label] = {
                "old": before,
                "new": after,
            }

    return changes


def reduction_percent(
    old_price,
    new_price,
):
    if (
        not old_price
        or new_price >= old_price
    ):
        return 0

    return round(
        (
            (old_price - new_price)
            / old_price
        )
        * 100,
        1,
    )


# ============================================================
# DAILY ACTIVITY
# ============================================================

def empty_daily_activity():
    return {
        "new": [],
        "reductions": [],
        "major_changes": [],
    }


def normalize_daily_activity(value):
    if not isinstance(value, dict):
        return empty_daily_activity()

    return {
        "new": list(
            value.get(
                "new",
                []
            )
        ),
        "reductions": list(
            value.get(
                "reductions",
                []
            )
        ),
        "major_changes": list(
            value.get(
                "major_changes",
                []
            )
        ),
    }


def append_unique_property(
    items,
    prop,
):
    key = property_key(prop)

    for index, existing in enumerate(
        items
    ):
        if property_key(existing) == key:
            items[index] = prop
            return

    items.append(prop)


def record_daily_activity(
    activity,
    new_items,
    reductions,
    major_changes,
):
    for prop in new_items:
        append_unique_property(
            activity["new"],
            prop,
        )

    for prop, old in reductions:
        key = property_key(prop)

        found = False

        for item in activity[
            "reductions"
        ]:
            if property_key(
                item.get(
                    "property",
                    {}
                )
            ) == key:
                # Preserve the earliest old price and newest property.
                item["property"] = prop
                found = True
                break

        if not found:
            activity[
                "reductions"
            ].append(
                {
                    "property": prop,
                    "old": old,
                }
            )

    for prop, changes in major_changes:
        key = property_key(prop)
        found = False

        for item in activity[
            "major_changes"
        ]:
            if property_key(
                item.get(
                    "property",
                    {}
                )
            ) == key:
                item["property"] = prop
                item["changes"].update(
                    changes
                )
                found = True
                break

        if not found:
            activity[
                "major_changes"
            ].append(
                {
                    "property": prop,
                    "changes": changes,
                }
            )


# ============================================================
# EMAIL HTML
# ============================================================

def property_block(
    prop,
    event_type="new",
    old=None,
    changes=None,
):
    name = html_escape(
        prop.get("name")
        or "Unnamed property"
    )

    price = prop.get("price")

    price_text = (
        f"${price:,.0f}"
        if price is not None
        else "Price not available"
    )

    html = [
        "<div style='padding:20px 0;"
        "border-bottom:1px solid #ddd'>"
    ]

    html.append(
        f"<h2 style='margin:0 0 8px;"
        f"font-size:22px'>{name}</h2>"
    )

    html.append(
        f"<div style='font-size:24px;"
        f"font-weight:700;"
        f"margin-bottom:12px'>"
        f"{price_text}</div>"
    )

    if prop.get(
        "calculated_price"
    ):
        rate = prop.get(
            "price_per_m2"
        )

        area = prop.get(
            "price_area_m2"
        )

        if rate and area:
            html.append(
                "<div style='margin-bottom:8px;"
                "font-size:13px;color:#666'>"
                "Calculated asking price: "
                f"${rate:,.0f}/m² × "
                f"{area:,.0f} m²"
                "</div>"
            )

    if (
        event_type == "reduction"
        and old
    ):
        old_price = old.get(
            "price",
            0,
        )

        percentage = reduction_percent(
            old_price,
            price,
        )

        html.append(
            f"<div style='margin-bottom:8px'>"
            f"<strong>PRICE REDUCTION:</strong> "
            f"${old_price:,.0f} → "
            f"${price:,.0f} "
            f"({percentage}% reduction)"
            f"</div>"
        )

    if prop.get("location"):
        html.append(
            f"<div><strong>Location:</strong> "
            f"{html_escape(prop['location'])}"
            f"</div>"
        )

    if prop.get("type"):
        html.append(
            f"<div><strong>Property type:</strong> "
            f"{html_escape(prop['type'])}"
            f"</div>"
        )

    facts = []

    if prop.get("beds"):
        facts.append(
            f"{html_escape(prop['beds'])} beds"
        )

    if prop.get("baths"):
        facts.append(
            f"{html_escape(prop['baths'])} baths"
        )

    if prop.get("size"):
        facts.append(
            html_escape(
                prop["size"]
            )
        )

    if facts:
        html.append(
            "<div><strong>Details:</strong> "
            + " · ".join(facts)
            + "</div>"
        )

    if changes:
        change_items = []

        for label, values in changes.items():
            change_items.append(
                f"{html_escape(label)}: "
                f"{html_escape(values['old'])} → "
                f"{html_escape(values['new'])}"
            )

        html.append(
            "<div style='margin-top:8px'>"
            "<strong>Major changes:</strong> "
            + " · ".join(change_items)
            + "</div>"
        )

    if prop.get("description"):
        html.append(
            f"<p style='margin:12px 0'>"
            f"{html_escape(prop['description'])}"
            f"</p>"
        )

    if prop.get("source"):
        html.append(
            f"<div><strong>Broker/source:</strong> "
            f"{html_escape(prop['source'])}"
            f"</div>"
        )

    if prop.get("detected_at"):
        detected = prop[
            "detected_at"
        ][:10]

        html.append(
            f"<div><strong>First detected:</strong> "
            f"{detected}</div>"
        )

    if prop.get("image"):
        html.append(
            "<div style='margin:15px 0'>"
            f"<img src='{html_escape(prop['image'])}' "
            "style='max-width:100%;height:auto;"
            "border-radius:8px' "
            "alt='Property image'>"
            "</div>"
        )

    if prop.get("url"):
        html.append(
            "<p style='margin-top:16px'>"
            f"<a href='{html_escape(prop['url'])}' "
            "style='display:inline-block;"
            "padding:12px 20px;"
            "background:#111;"
            "color:#fff;"
            "text-decoration:none;"
            "border-radius:6px;"
            "font-weight:600'>"
            "VIEW PROPERTY"
            "</a>"
            "</p>"
        )

    html.append("</div>")

    return "".join(html)


def email_footer():
    return (
        "<div style='margin-top:25px;"
        "padding-top:15px;"
        "border-top:1px solid #ddd;"
        "font-size:12px;"
        "color:#777'>"
        "<strong>Manage alerts:</strong> "
        "<a href='mailto:"
        "guidobrugman@live.nl"
        "?subject=STOP%20ARUBA%20PROPERTY%20ALERTS'>"
        "request changes or stop alerts"
        "</a>."
        "</div>"
    )


# ============================================================
# RESEND
# ============================================================

def send_email(
    subject,
    html,
):
    if not RESEND_API_KEY:
        print(
            "RESEND_API_KEY missing; "
            "email not sent."
        )
        return False

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": (
                f"Bearer {RESEND_API_KEY}"
            ),
            "Content-Type": "application/json",
        },
        json={
            "from": SENDER,
            "to": [RECIPIENT],
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )

    response.raise_for_status()

    print(
        "Email sent successfully."
    )

    return True


# ============================================================
# ALERT EMAILS
# ============================================================

def send_event_email(
    new_items,
    reductions,
    major_changes,
):
    sections = []

    if new_items:
        sections.append(
            "<h2>NEW PROPERTIES</h2>"
        )

        for prop in new_items:
            sections.append(
                property_block(
                    prop,
                    "new",
                )
            )

    if reductions:
        sections.append(
            "<h2>PRICE REDUCTIONS</h2>"
        )

        for prop, old in reductions:
            sections.append(
                property_block(
                    prop,
                    "reduction",
                    old=old,
                )
            )

    if major_changes:
        sections.append(
            "<h2>MAJOR CHANGES</h2>"
        )

        for prop, changes in major_changes:
            sections.append(
                property_block(
                    prop,
                    "major",
                    changes=changes,
                )
            )

    if not sections:
        return False

    html = (
        "<html><body style='font-family:Arial,"
        "sans-serif;max-width:760px;"
        "margin:auto;padding:20px;"
        "color:#222'>"
        "<h1>Aruba Property Alert</h1>"
        + "".join(sections)
        + email_footer()
        + "</body></html>"
    )

    subject_parts = []

    if new_items:
        subject_parts.append(
            f"{len(new_items)} new"
        )

    if reductions:
        subject_parts.append(
            f"{len(reductions)} price reduction"
        )

    if major_changes:
        subject_parts.append(
            f"{len(major_changes)} major change"
        )

    subject = (
        "Aruba Property Alert — "
        + ", ".join(subject_parts)
    )

    return send_email(
        subject,
        html,
    )


# ============================================================
# DAILY ACTIVITY DIGEST
# ============================================================

def send_daily_digest(
    activity,
    current_count,
    source_health,
):
    new_items = activity.get(
        "new",
        []
    )

    reductions = activity.get(
        "reductions",
        []
    )

    major_changes = activity.get(
        "major_changes",
        []
    )

    html_parts = [
        "<html><body style='font-family:Arial,"
        "sans-serif;max-width:760px;"
        "margin:auto;padding:20px;"
        "color:#222'>",
        "<h1>Aruba Property Daily Digest</h1>",
        "<p style='font-size:16px'>"
        "<strong>"
        f"{len(new_items)} new properties"
        "</strong><br>",
        "<strong>"
        f"{len(reductions)} price reductions"
        "</strong><br>",
        "<strong>"
        f"{len(major_changes)} major changes"
        "</strong><br>",
        f"{current_count} qualifying properties "
        "currently detected across available sources."
        "</p>",
    ]

    if (
        not new_items
        and not reductions
        and not major_changes
    ):
        html_parts.append(
            "<p><strong>No new opportunities or "
            "meaningful listing changes were detected "
            "since the previous daily digest.</strong></p>"
        )

    if new_items:
        html_parts.append(
            "<h2>NEW SINCE LAST DIGEST</h2>"
        )

        for prop in new_items:
            html_parts.append(
                property_block(
                    prop,
                    "new",
                )
            )

    if reductions:
        html_parts.append(
            "<h2>PRICE REDUCTIONS SINCE LAST DIGEST</h2>"
        )

        for item in reductions:
            html_parts.append(
                property_block(
                    item["property"],
                    "reduction",
                    old=item["old"],
                )
            )

    if major_changes:
        html_parts.append(
            "<h2>MAJOR CHANGES SINCE LAST DIGEST</h2>"
        )

        for item in major_changes:
            html_parts.append(
                property_block(
                    item["property"],
                    "major",
                    changes=item["changes"],
                )
            )

    # Source health is summarized, not allowed to dominate the email.
    successful = [
        name
        for name, data
        in source_health.items()
        if data.get("status") == "ok"
    ]

    failed = [
        name
        for name, data
        in source_health.items()
        if data.get("status") == "error"
    ]

    html_parts.append(
        "<div style='margin-top:25px;"
        "padding:14px;"
        "background:#f5f5f5;"
        "border-radius:6px;"
        "font-size:13px'>"
        "<strong>Source coverage:</strong> "
        f"{len(successful)} sources checked successfully"
    )

    if failed:
        html_parts.append(
            f"; {len(failed)} source"
            f"{'s' if len(failed) != 1 else ''} "
            "could not be checked during the latest run."
        )
    else:
        html_parts.append(
            "; no source failures in the latest run."
        )

    html_parts.append(
        "</div>"
    )

    html_parts.append(
        email_footer()
    )

    html_parts.append(
        "</body></html>"
    )

    subject = (
        "Aruba Property Daily Digest — "
        f"{len(new_items)} new, "
        f"{len(reductions)} reductions"
    )

    return send_email(
        subject,
        "".join(html_parts),
    )


# ============================================================
# DAILY DIGEST TIMING
# ============================================================

def should_send_daily_digest(state):
    local = aruba_now()

    if local.hour != 8:
        return False

    today = local.date().isoformat()

    return (
        state.get(
            "last_digest_date"
        )
        != today
    )


# ============================================================
# QUEUE HELPERS
# ============================================================

def dedupe_pending_new(items):
    unique = {}

    for item in items:
        if item.get("type") in (
            EXCLUDED_RESIDENTIAL_UNIT_TYPES
        ):
            continue

        unique[
            property_key(item)
        ] = item

    return list(
        unique.values()
    )


def dedupe_pending_events(
    items,
):
    unique = {}

    for item in items:
        prop = item.get(
            "property",
            {}
        )

        if not prop:
            continue

        unique[
            property_key(prop)
        ] = item

    return list(
        unique.values()
    )


# ============================================================
# MAIN
# ============================================================

def main():
    print(
        "Aruba Property Agent starting..."
    )

    source_data = load_json(
        SOURCES_FILE,
        {"sources": []},
    )

    sources = [
        source
        for source in source_data.get(
            "sources",
            [],
        )
        if source.get("enabled")
    ]

    state = load_json(
        STATE_FILE,
        {
            "properties": {},
            "pending_new": [],
            "pending_reductions": [],
            "pending_major_changes": [],
            "daily_activity": (
                empty_daily_activity()
            ),
            "last_digest_date": None,
            "source_health": {},
        },
    )

    previous = previous_properties(
        state
    )

    daily_activity = (
        normalize_daily_activity(
            state.get(
                "daily_activity"
            )
        )
    )

    source_health = dict(
        state.get(
            "source_health",
            {}
        )
    )

    current = []

    # --------------------------------------------------------
    # SCRAPE ALL ENABLED SOURCES
    # --------------------------------------------------------

    for source in sources:
        name = source["name"]
        url = source["url"]

        print(
            f"\nChecking source: {name}"
        )

        print(
            f"URL: {url}"
        )

        try:
            properties = scrape_source(
                source
            )

            print(
                f"Properties found: "
                f"{len(properties)}"
            )

            current.extend(
                properties
            )

            source_health[name] = {
                "status": "ok",
                "checked_at": iso_now(),
                "properties_found": len(
                    properties
                ),
                "error": "",
            }

        except Exception as error:
            error_text = clean_text(
                str(error)
            )

            print(
                f"ERROR checking {name}: "
                f"{error_text}"
            )

            source_health[name] = {
                "status": "error",
                "checked_at": iso_now(),
                "properties_found": 0,
                "error": error_text[:300],
            }

    current = cross_source_dedupe(
        current
    )

    # --------------------------------------------------------
    # MATCH CURRENT PROPERTIES TO PERMANENT HISTORY
    # --------------------------------------------------------

    current_records = []
    new_items = []
    reductions = []
    major_changes = []

    matched_previous_keys = set()

    for prop in current:
        old_key, old = find_previous_record(
            previous,
            prop,
        )

        if not old:
            prop["detected_at"] = iso_now()

            new_items.append(
                prop
            )

            current_records.append(
                (
                    property_key(prop),
                    prop,
                )
            )

            continue

        matched_previous_keys.add(
            old_key
        )

        prop["detected_at"] = old.get(
            "detected_at",
            prop.get(
                "detected_at",
                iso_now(),
            ),
        )

        old_price = old.get(
            "price"
        )

        new_price = prop.get(
            "price"
        )

        if (
            old_price
            and new_price
            and new_price < old_price
        ):
            reductions.append(
                (
                    prop,
                    old,
                )
            )

        changes = field_changes(
            old,
            prop,
        )

        # Type-only changes caused by this improved classifier are not
        # useful enough to email by themselves. They are simply saved
        # into state. Other substantive changes still qualify.
        if (
            changes
            and set(
                changes.keys()
            ) == {"Property type"}
        ):
            changes = {}

        if changes:
            major_changes.append(
                (
                    prop,
                    changes,
                )
            )

        current_records.append(
            (
                property_key(prop),
                prop,
            )
        )

    current_by_key = dict(
        current_records
    )

    # --------------------------------------------------------
    # RECORD DAILY ACTIVITY
    # --------------------------------------------------------

    record_daily_activity(
        daily_activity,
        new_items,
        reductions,
        major_changes,
    )

    # --------------------------------------------------------
    # BATCH QUEUE
    # --------------------------------------------------------

    pending_new = list(
        state.get(
            "pending_new",
            [],
        )
    )

    pending_reductions = list(
        state.get(
            "pending_reductions",
            [],
        )
    )

    pending_major = list(
        state.get(
            "pending_major_changes",
            [],
        )
    )

    pending_new.extend(
        new_items
    )

    pending_new = dedupe_pending_new(
        pending_new
    )

    for prop, old in reductions:
        pending_reductions.append(
            {
                "property": prop,
                "old": old,
                "queued_at": iso_now(),
            }
        )

    pending_reductions = (
        dedupe_pending_events(
            pending_reductions
        )
    )

    for prop, changes in major_changes:
        pending_major.append(
            {
                "property": prop,
                "changes": changes,
                "queued_at": iso_now(),
            }
        )

    pending_major = (
        dedupe_pending_events(
            pending_major
        )
    )

    # New listings use first-detected time as queue time.
    for prop in pending_new:
        if not prop.get("queued_at"):
            prop["queued_at"] = (
                prop.get(
                    "detected_at"
                )
                or iso_now()
            )

    cutoff = (
        now_utc()
        - timedelta(
            minutes=BATCH_MINUTES
        )
    )

    def is_old_enough(item):
        queued = parse_datetime(
            item.get(
                "queued_at",
                "",
            )
        )

        if not queued:
            return False

        return queued <= cutoff

    batch_ready = (
        any(
            is_old_enough(item)
            for item in pending_new
        )
        or any(
            is_old_enough(item)
            for item in pending_reductions
        )
        or any(
            is_old_enough(item)
            for item in pending_major
        )
    )

    if batch_ready:
        email_sent = send_event_email(
            pending_new,
            [
                (
                    item["property"],
                    item["old"],
                )
                for item
                in pending_reductions
            ],
            [
                (
                    item["property"],
                    item["changes"],
                )
                for item
                in pending_major
            ],
        )

        # Queues are cleared only after Resend confirms delivery.
        if email_sent:
            pending_new = []
            pending_reductions = []
            pending_major = []

    # --------------------------------------------------------
    # PERMANENT HISTORY
    # --------------------------------------------------------

    property_history = dict(
        previous
    )

    # Remove legacy-key copies only when we have successfully matched
    # them and are about to save the same property under the new stable
    # identity. This gradually migrates state without losing history.
    for prop in current:
        old_key, old = find_previous_record(
            previous,
            prop,
        )

        new_key = property_key(prop)

        if (
            old_key
            and old_key != new_key
            and old_key in property_history
        ):
            property_history.pop(
                old_key,
                None,
            )

        property_history[
            new_key
        ] = prop

    new_state = {
        "properties": property_history,
        "pending_new": pending_new,
        "pending_reductions": (
            pending_reductions
        ),
        "pending_major_changes": (
            pending_major
        ),
        "daily_activity": (
            daily_activity
        ),
        "last_digest_date": state.get(
            "last_digest_date"
        ),
        "source_health": source_health,
    }

    # --------------------------------------------------------
    # DAILY ACTIVITY DIGEST
    # --------------------------------------------------------

    if should_send_daily_digest(
        new_state
    ):
        digest_sent = send_daily_digest(
            daily_activity,
            len(current_by_key),
            source_health,
        )

        if digest_sent:
            new_state[
                "last_digest_date"
            ] = (
                aruba_now()
                .date()
                .isoformat()
            )

            # Activity is "since previous digest", so clear it only
            # after the new digest has successfully been sent.
            new_state[
                "daily_activity"
            ] = empty_daily_activity()

    save_json(
        STATE_FILE,
        new_state,
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

    successful_sources = [
        name
        for name, data
        in source_health.items()
        if data.get("status") == "ok"
    ]

    failed_sources = [
        name
        for name, data
        in source_health.items()
        if data.get("status") == "error"
    ]

    print(
        "\n============================================================"
    )

    print(
        f"CURRENT QUALIFYING PROPERTIES: "
        f"{len(current_by_key)}"
    )

    print(
        f"NEW DETECTED THIS RUN: "
        f"{len(new_items)}"
    )

    print(
        f"PRICE REDUCTIONS THIS RUN: "
        f"{len(reductions)}"
    )

    print(
        f"MAJOR CHANGES THIS RUN: "
        f"{len(major_changes)}"
    )

    print(
        f"PENDING NEW ALERTS: "
        f"{len(pending_new)}"
    )

    print(
        f"PENDING PRICE ALERTS: "
        f"{len(pending_reductions)}"
    )

    print(
        f"PENDING MAJOR CHANGE ALERTS: "
        f"{len(pending_major)}"
    )

    print(
        f"SOURCES OK: "
        f"{len(successful_sources)}"
    )

    print(
        f"SOURCES FAILED: "
        f"{len(failed_sources)}"
    )

    if failed_sources:
        print(
            "FAILED SOURCES: "
            + ", ".join(
                failed_sources
            )
        )

    print(
        "============================================================"
    )

    print(
        "Monitor run completed successfully."
    )


if __name__ == "__main__":
    main()
