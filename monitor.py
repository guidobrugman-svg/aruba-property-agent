import json
import os
import re
import time
import hashlib
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin

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
    "resort",
)

session = requests.Session()
session.headers.update(HEADERS)


# ============================================================
# GENERAL HELPERS
# ============================================================

def now_utc():
    return datetime.now(timezone.utc)


def iso_now():
    return now_utc().isoformat()


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


# ============================================================
# HTTP / SOURCE ACCESS
# ============================================================

def get_page(url):
    """
    Try normal HTTPS first.

    If an Aruba site has a certificate-hostname problem, retry
    without certificate verification. This is only a fallback for
    fetching public listing pages; normal HTTPS remains preferred.

    403 responses are not treated as successful. They are reported
    and skipped so the other sources continue working.
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
# PARSING HELPERS
# ============================================================

def parse_price(text):
    text = clean_text(text)

    if not text:
        return None

    # Explicit USD / dollar values first.
    patterns = [
        r"(?:USD|US\$)\s*([\d,]+(?:\.\d+)?)",
        r"\$\s*([\d,]+(?:\.\d+)?)",
        r"([\d,]+(?:\.\d+)?)\s*(?:USD|US\$)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            try:
                return float(match.group(1).replace(",", ""))
            except ValueError:
                pass

    # Do not accidentally interpret a pure AWG amount as USD.
    if (
        re.search(r"\bAWG\b", text, re.IGNORECASE)
        and not re.search(r"\bUSD\b|\$", text, re.IGNORECASE)
    ):
        return None

    return None


def first_match(patterns, text):
    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return clean_text(match.group(1))

    return ""


def extract_status(text):
    lower = normalize(text)

    for status in EXCLUDED_STATUS:
        if status in lower:
            return status

    return ""


def extract_type(text, title=""):
    combined = normalize(f"{title} {text}")

    mapping = [
        ("apartment complex", "Apartment Complex"),
        ("condominium", "Condominium"),
        ("townhouse", "Townhouse"),
        ("townhome", "Townhouse"),
        ("development", "Development"),
        ("villa", "Villa"),
        ("apartment", "Apartment"),
        ("house", "House"),
        ("land", "Land"),
        ("commercial", "Commercial"),
        ("office", "Office"),
        ("warehouse", "Warehouse"),
        ("retail", "Retail"),
        ("store", "Retail"),
        ("hotel", "Hotel"),
        ("resort", "Resort"),
    ]

    for needle, result in mapping:
        if needle in combined:
            return result

    return ""


def extract_details(title, text):
    beds = first_match(
        [
            r"\b(\d+)\s*(?:bed|beds|bedroom|bedrooms|bd|bdr)\b",
        ],
        text,
    )

    baths = first_match(
        [
            r"\b(\d+(?:\.\d+)?)\s*(?:bath|baths|bathroom|bathrooms|ba)\b",
        ],
        text,
    )

    size = first_match(
        [
            r"\b([\d,.]+)\s*(?:m²|m2|sqm)\b",
            r"\b([\d,.]+)\s*(?:sq\s*ft|sqft|ft²)\b",
        ],
        text,
    )

    location = first_match(
        [
            r"\b(?:location|area|neighborhood|neighbourhood)"
            r"\s*[:\-]\s*([^|•\n]+)",
        ],
        text,
    )

    # Many Aruba listings simply contain the location as a known area.
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
            "Pavia",
        ]

        lower_text = text.lower()

        for area in known_areas:
            if area.lower() in lower_text:
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
            return absolute_url(base_url, value)

    srcset = image.get("srcset")

    if srcset:
        first = srcset.split(",")[0].strip().split(" ")[0]
        return absolute_url(base_url, first)

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
    }:
        return False

    if len(title) < 5:
        return False

    junk = (
        "lorem ipsum",
        "test listing",
        "sample property",
        "asdfsaf",
    )

    return not any(item in normalized for item in junk)


def make_description(text, title):
    text = clean_text(text)

    if title:
        text = text.replace(title, " ", 1)

    text = re.sub(
        r"\bview details\b",
        "",
        text,
        flags=re.IGNORECASE,
    )

    text = clean_text(text)

    if len(text) > 320:
        text = text[:317].rsplit(" ", 1)[0] + "..."

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
):
    title = clean_text(title)
    text = clean_text(text)

    if not looks_like_property_title(title):
        return None

    if price is None:
        return None

    if price <= 0 or price > PRICE_LIMIT:
        return None

    status = extract_status(text)

    if status:
        return None

    beds, baths, size, location = extract_details(
        title,
        text,
    )

    property_type = extract_type(
        text,
        title,
    )

    combined = normalize(
        f"{title} {property_type} {text}"
    )

    if any(
        excluded in combined
        for excluded in EXCLUDED_TYPES
    ):
        return None

    mls = first_match(
        [
            r"\bMLS\s*[:#]?\s*([A-Z0-9\-]+)\b",
            r"\bAW\d{6,}\b",
        ],
        text,
    )

    if not mls:
        m = re.search(
            r"\b(AW\d{6,})\b",
            text,
            re.IGNORECASE,
        )

        if m:
            mls = m.group(1)

    return {
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


# ============================================================
# SCRAPING
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
        "[class*='property-card']",
        "[class*='listing-card']",
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


def scrape_generic(source, url):
    html, final_url = get_page(url)

    soup = BeautifulSoup(
        html,
        "html.parser",
    )

    results = get_card_nodes(soup)

    if not results:
        results = soup.find_all(
            ["article", "li"],
            limit=500,
        )

    properties = []

    for node in results:
        text = clean_text(
            node.get_text(
                " ",
                strip=True,
            )
        )

        if not text:
            continue

        title = ""

        for tag in (
            "h1",
            "h2",
            "h3",
            "h4",
            "h5",
        ):
            heading = node.find(tag)

            if heading:
                candidate = clean_text(
                    heading.get_text(
                        " ",
                        strip=True,
                    )
                )

                if looks_like_property_title(candidate):
                    title = candidate
                    break

        link = node.find(
            "a",
            href=True,
        )

        if not title and link:
            candidate = clean_text(
                link.get_text(
                    " ",
                    strip=True,
                )
            )

            if looks_like_property_title(candidate):
                title = candidate

        if not title:
            continue

        price = parse_price(text)

        if price is None:
            continue

        property_url = (
            absolute_url(
                final_url,
                link.get("href"),
            )
            if link
            else final_url
        )

        prop = build_property(
            source=source,
            title=title,
            url=property_url,
            price=price,
            text=text,
            image=image_from(
                node,
                final_url,
            ),
        )

        if prop:
            properties.append(prop)

    return dedupe_properties(properties)


def scrape_bluefin(source, url):
    """
    Bluefin is treated as one consolidated source.

    Bluefin's current page structure exposes listing titles/prices as
    heading links, so this parser does not depend on a specific card
    CSS class. It also follows the first three listing pages to improve
    coverage while keeping the run lightweight.
    """

    properties = []
    seen_urls = set()
    page_urls = [url]

    clean_base = url.rstrip("/") + "/"
    for page_number in (2, 3):
        page_urls.append(f"{clean_base}page/{page_number}/")
        page_urls.append(f"{clean_base}?paged={page_number}")

    fetched_pages = set()

    for page_url in page_urls:
        if page_url in fetched_pages:
            continue

        fetched_pages.add(page_url)

        try:
            html, final_url = get_page(page_url)
        except Exception:
            if page_url == url:
                raise
            continue

        soup = BeautifulSoup(html, "html.parser")

        heading_links = []

        for heading in soup.find_all(
            ["h1", "h2", "h3", "h4", "h5"]
        ):
            link = heading.find("a", href=True)
            if not link:
                continue

            title = clean_text(
                link.get_text(" ", strip=True)
            )

            if not looks_like_property_title(title):
                continue

            href = absolute_url(
                final_url,
                link.get("href"),
            )

            if not href or href in seen_urls:
                continue

            heading_links.append(
                (heading, link, title, href)
            )

        if not heading_links:
            for link in soup.find_all("a", href=True):
                title = clean_text(
                    link.get_text(" ", strip=True)
                )

                if not looks_like_property_title(title):
                    continue

                href = absolute_url(
                    final_url,
                    link.get("href"),
                )

                if not href or href in seen_urls:
                    continue

                parent_text = (
                    link.parent.get_text(
                        " ",
                        strip=True,
                    )
                    if link.parent
                    else ""
                )

                if parse_price(parent_text) is not None:
                    heading_links.append(
                        (
                            link,
                            link,
                            title,
                            href,
                        )
                    )

        page_properties = 0

        for heading, link, title, property_url in heading_links:
            seen_urls.add(property_url)

            container = heading
            text = ""

            for _ in range(8):
                if not container:
                    break

                candidate_text = clean_text(
                    container.get_text(
                        " ",
                        strip=True,
                    )
                )

                if parse_price(candidate_text) is not None:
                    text = candidate_text
                    break

                container = container.parent

            if not text:
                parent = heading.parent
                if parent:
                    text = clean_text(
                        parent.get_text(
                            " ",
                            strip=True,
                        )
                    )

            price = parse_price(text)

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
            )

            if prop:
                properties.append(prop)
                page_properties += 1

        print(
            f"  Bluefin page {page_url}: "
            f"{page_properties} qualifying listings"
        )

    return dedupe_properties(properties)

def scrape_source(source):
    name = source["name"]
    url = source["url"]

    if name == "Bluefin Realtors":
        return scrape_bluefin(
            name,
            url,
        )

    return scrape_generic(
        name,
        url,
    )


# ============================================================
# DEDUPLICATION
# ============================================================

def canonical_text(prop):
    values = [
        prop.get("name", ""),
        prop.get("location", ""),
        prop.get("type", ""),
        prop.get("mls", ""),
    ]

    return normalize(
        " ".join(values)
    )


def property_key(prop):
    mls = normalize(
        prop.get("mls", "")
    )

    if mls:
        return f"mls:{mls}"

    canonical = canonical_text(prop)

    if not canonical:
        canonical = prop.get(
            "url",
            "",
        )

    return hashlib.sha1(
        canonical.encode("utf-8")
    ).hexdigest()


def development_key(prop):
    """
    Collapse obvious unit-level duplicates for the
    same development while retaining genuinely different
    properties.
    """

    title = normalize(
        prop.get("name", "")
    )

    location = normalize(
        prop.get("location", "")
    )

    # Remove unit / apartment / lot identifiers.
    title = re.sub(
        r"\b(?:unit|apt|apartment|condo|villa|lot|phase)"
        r"\s*[-#]?\s*[a-z0-9]+\b",
        "",
        title,
    )

    # Remove standalone unit numbers.
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


def dedupe_properties(properties):
    unique = {}

    for prop in properties:
        mls = normalize(
            prop.get("mls", "")
        )

        if mls:
            key = f"mls:{mls}"
        else:
            key = canonical_text(prop)

        if not key:
            key = prop.get(
                "url",
                "",
            )

        if key not in unique:
            unique[key] = prop
        else:
            existing = unique[key]

            # Preserve richer information.
            for field in (
                "image",
                "description",
                "location",
                "beds",
                "baths",
                "size",
                "mls",
            ):
                if (
                    not existing.get(field)
                    and prop.get(field)
                ):
                    existing[field] = prop[field]

    return list(
        unique.values()
    )


def cross_source_dedupe(properties):
    result = []
    seen = set()
    developments = {}

    for prop in sorted(
        properties,
        key=lambda p: (
            p.get("source", ""),
            p.get("name", ""),
        ),
    ):
        key = property_key(prop)

        if key in seen:
            continue

        dev_key = development_key(prop)

        if dev_key:
            existing_index = developments.get(
                dev_key
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
                        existing_price
                        * 0.03,
                    )
                )

                if similar_price:
                    # Keep the lower qualifying price.
                    if (
                        new_price
                        and new_price
                        < existing_price
                    ):
                        result[
                            existing_index
                        ] = prop

                    continue

            developments[
                dev_key
            ] = len(result)

        seen.add(key)
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
    """
    Supports both the current nested state format and
    the older flat format.
    """

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

    price = prop.get(
        "price"
    )

    price_text = (
        f"${price:,.0f}"
        if price is not None
        else "Price not available"
    )

    html = [
        "<div style='padding:20px 0;"
        "border-bottom:1px solid #ddd'>"
    ]

    # PROPERTY NAME — deliberately prominent.
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
        detected = (
            prop["detected_at"][:10]
        )

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


def send_daily_digest(properties):
    if not properties:
        return False

    items = []

    for prop in sorted(
        properties,
        key=lambda p: p.get(
            "price",
            0,
        ),
    ):
        items.append(
            property_block(
                prop,
                "digest",
            )
        )

    html = (
        "<html><body style='font-family:Arial,"
        "sans-serif;max-width:760px;"
        "margin:auto;padding:20px;"
        "color:#222'>"
        "<h1>Aruba Property Daily Digest</h1>"
        f"<p>{len(properties)} qualifying "
        "properties currently tracked "
        "under $650,000.</p>"
        + "".join(items)
        + email_footer()
        + "</body></html>"
    )

    return send_email(
        "Aruba Property Daily Digest",
        html,
    )


# ============================================================
# DAILY DIGEST TIMING
# ============================================================

def should_send_daily_digest(state):
    local = datetime.now(
        ARUBA_TZ
    )

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
            "last_digest_date": None,
        },
    )

    previous = previous_properties(
        state
    )

    current = []

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

        except Exception as error:
            print(
                f"ERROR checking {name}: "
                f"{error}"
            )

    current = cross_source_dedupe(
        current
    )

    current_by_key = {
        property_key(prop): prop
        for prop in current
    }

    new_items = []
    reductions = []
    major_changes = []

    for key, prop in current_by_key.items():
        old = previous.get(key)

        if not old:
            prop["detected_at"] = iso_now()

            new_items.append(
                prop
            )

            continue

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

        if changes:
            major_changes.append(
                (
                    prop,
                    changes,
                )
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

    for prop, old in reductions:
        pending_reductions.append(
            {
                "property": prop,
                "old": old,
                "queued_at": iso_now(),
            }
        )

    for prop, changes in major_changes:
        pending_major.append(
            {
                "property": prop,
                "changes": changes,
                "queued_at": iso_now(),
            }
        )

    # Give new items an explicit queue time.
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
        send_event_email(
            pending_new,
            [
                (
                    item["property"],
                    item["old"],
                )
                for item in pending_reductions
            ],
            [
                (
                    item["property"],
                    item["changes"],
                )
                for item in pending_major
            ],
        )

        pending_new = []
        pending_reductions = []
        pending_major = []

    # --------------------------------------------------------
    # STATE
    # --------------------------------------------------------

    new_state = {
        "properties": current_by_key,
        "pending_new": pending_new,
        "pending_reductions": pending_reductions,
        "pending_major_changes": pending_major,
        "last_digest_date": state.get(
            "last_digest_date"
        ),
    }

    # --------------------------------------------------------
    # DAILY DIGEST
    # --------------------------------------------------------

    if should_send_daily_digest(
        new_state
    ):
        if send_daily_digest(
            list(
                current_by_key.values()
            )
        ):
            new_state[
                "last_digest_date"
            ] = datetime.now(
                ARUBA_TZ
            ).date().isoformat()

    save_json(
        STATE_FILE,
        new_state,
    )

    # --------------------------------------------------------
    # SUMMARY
    # --------------------------------------------------------

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
        "============================================================"
    )

    print(
        "Monitor run completed successfully."
    )


if __name__ == "__main__":
    main()
