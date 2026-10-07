import json
import os
import re
import time
import hashlib
import sys
import threading
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
import collectors
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

STATE_FILE = os.environ.get("STATE_FILE", "state.json")
SOURCES_FILE = "SOURCES.json"

PRICE_LIMIT = 800000
BATCH_MINUTES = 30
DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"
_local = threading.local()

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
    "pending",
    "on hold",
    "reserved",
    "in process",
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
    "XCLSV Aruba Realty",
    "Aruba Brokers",
    "Ben Real Estate",
    "RE/MAX Aruba",
    "RES Aruba Realty",
    "Aruba Happy Realty",
    "Aruba Palms Realtors",
    "Home 4 Everyone",
    "Smiley Real Estate",
    'Berkshire Hathaway Aruba',
    'Realty ONE Group Aruba',
    'HKG Real Estate Aruba',
    'MPG Aruba',
    'Aruba Happy Homes',
    'Objective Realty Aruba',
    'Alto Vista Real Estate',
    'Kermit Real Estate',
    'C & C Real Estate',
    'Cas y Estilo',
    'ID Realty Group',
    'ABLE Realty',
    'JZ Realty Aruba',
    'Bold Properties Aruba',
    'All Property Aruba',
    'AJ Real Estate Aruba',
    'Prima Casa Real Estate',
    'Casnan Real Estate',
    'Maurer Real Estate',
    'Bon Choice Aruba Realty',
    'Aruba Home Minders',
    'Aruba Real Estate Brokers',
    'Capital Reliance Aruba',
    'Real Estate Aruba',
}



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
        .replace("'", "&#x27;")
    )


def absolute_url(base_url, href):
    if not href:
        return ""
    return urljoin(base_url, href)


def source_priority(source_name):
    if source_name in DIRECT_SOURCE_NAMES or source_name in {"Century 21 Aruba", "Coldwell Banker Aruba", "Keller Williams Aruba"}:
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

def get_page(url, request_data=None, timeout=(4, 8), request_headers=None):
    """One bounded retry for transient failures; TLS verification stays enabled."""
    if not hasattr(_local, "session"):
        _local.session = requests.Session()
        _local.session.headers.update(HEADERS)
    for attempt in range(2):
        try:
            if request_data is None:
                response = _local.session.get(url, headers=request_headers, timeout=timeout, allow_redirects=True)
            else:
                response = _local.session.post(url, data=request_data, headers=request_headers, timeout=timeout, allow_redirects=True)
            response.raise_for_status()
            return response.text, response.url
        except requests.exceptions.SSLError:
            raise
        except requests.exceptions.HTTPError as exc:
            if exc.response.status_code != 429 and exc.response.status_code < 500:
                raise
            if attempt:
                raise
        except (requests.exceptions.Timeout, requests.exceptions.ConnectionError):
            if attempt:
                raise
        time.sleep(0.5)


def canonical_url(url):
    parsed = urlsplit(clean_text(url))
    query = [(k, v) for k, v in parse_qsl(parsed.query, keep_blank_values=True)
             if not k.lower().startswith('utm_') and k.lower() not in ('fbclid', 'gclid')]
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                      parsed.path.rstrip('/'), urlencode(sorted(query)), ''))


# ============================================================
# PRICE PARSING
# ============================================================

def parse_number(value):
    value = clean_text(value).replace(' ', '')
    if not value:
        return None
    # Aruba brokers use both 650,000 and 650.000 (and decimal rates).
    if re.fullmatch(r"\d{1,3}(?:\.\d{3})+\.\d{2}", value):
        groups = value.split('.')
        value = ''.join(groups[:-1]) + '.' + groups[-1]
    elif re.fullmatch(r"\d{1,3}(?:[.,]\d{3})+", value):
        value = value.replace(',', '').replace('.', '')
    elif ',' in value and '.' in value:
        value = value.replace(',', '') if value.rfind('.') > value.rfind(',') else value.replace('.', '').replace(',', '.')
    else:
        value = value.replace(',', '.')
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
        r"\b([\d,.]+)\s*(?:m²|m2|sqm|sq\.?\s*m|sq\s*mt)(?!\w)",
        text,
        re.IGNORECASE,
    ):
        number = parse_number(match.group(1))

        if number and number > 0:
            values.append(number)

    if not values:
        return None

    return values[0] if len(set(values)) == 1 else None


def extract_per_m2_rate(text):
    """
    Detect prices such as:
      $98/M2
      US$ 98 per m²
      USD 98 / sqm
    """

    patterns = [
        (
            r"(?:USD|US\$|\$)\s*([\d,.]+)"
            r"\s*(?:/|per\s+)"
            r"(?:m²|m2|sqm|sq\.?\s*m|sq\s*mt)(?!\w)"
        ),
        (
            r"([\d,.]+)\s*(?:USD|US\$|\$)"
            r"\s*(?:/|per\s+)"
            r"(?:m²|m2|sqm|sq\.?\s*m|sq\s*mt)(?!\w)"
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
        r"(?:USD|US\$|U\$D)\s*:?\s*([\d,.]+)",
        r"\$\s*([\d,.]+)",
        r"([\d,.]+)\s*(?:USD|US\$)",
        r"(\d[\d,.]*(?:\s\d{3})*)\s*\$",
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
        if re.search(r"\b" + re.escape(normalize(status)) + r"\b", lower):
            return status

    if has_explicit_rental_status(text):
        return "for rent"

    return ""


# ============================================================
# PROPERTY TYPE INFERENCE
# ============================================================

def house_with_apartments(title, text):
    combined = normalize(title + ' ' + text)
    if re.search(r'\bmain house\b.{0,40}\bseparate (?:apartment|studio unit)\b', combined):
        return True
    count = r'(?:a|an|one|two|three|four|five|six|seven|eight|nine|ten|\d+)'
    if re.search(r'\b(?:the |this )?property consists of (?:a |the )?main house\b.{0,180}\b(?:there are|and|plus|with)\s+' + count + r'\s+(?:studio\s+)?apartments?\b', combined):
        return True
    return bool(re.search(r'\b(?:house|home|villa)\s+(?:with|and|plus|including)\s+(?:' + count + r'\s+)?(?:studio\s+)?apartments?\b|\bapartments?\s+(?:with|and|plus)\s+(?:a\s+|an\s+)?(?:main\s+)?(?:house|home|villa)\b', combined))


def residential_income_offer(title, text, property_type=''):
    return property_type == 'Apartment Complex' or house_with_apartments(title, text)


def residence_unit_offer(title, text):
    title_n, combined = normalize(title), normalize(title + ' ' + text)
    if re.search(r'\bresidences\b|\bresidence (?:complex|development|project|pools)\b', combined):
        return True
    if re.search(r'\b(?:monte verde|wayaca|reina sophia|ora|orquidea|napa valley|harbou?r|mikassa|solarium) residence\b', combined):
        return True
    if re.search(r'\b(?:model|unit)\b.{0,60}\bresidence\b|\bresidence\b.{0,60}\b(?:model|unit|\d+ bedroom)\b', title_n):
        return True
    return bool(re.search(r'\bharbou?r house\b.*\bunit\b', title_n))


def whole_apartment_offer(title, text):
    """Explicit multi-apartment sale offers; unit numbers and per-unit projects fail closed."""
    title_n, text_n = normalize(title), normalize(text)
    combined = title_n + ' ' + text_n
    if re.search(r'\b(?:apartments?|units?) (?:remaining|left|available)\b|\b(?:starting (?:at|from)|price per (?:unit|apartment)|priced per (?:unit|apartment))\b', combined):
        return False
    if re.search(r'(?:USD|US\$|\$)[\s\d.,]{1,25}\s*(?:per|/)\s*(?:unit|apartment)\b', title + ' ' + text, re.I):
        return False
    if re.search(r'\b(?:apartment|studio|condo|condominium|unit)\s+(?:unit\s+|number\s+|no\s+)?\d+\b', title_n):
        return False
    count = r'(?:[2-9]|[1-9]\d+|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve)'
    apartments = r'(?:studio\s+)?apartments'
    if re.search(r'^' + count + r'\s+' + apartments + r'\b', title_n):
        return True
    return bool(re.search(r'\b(?:entire|whole)\s+(?:apartment (?:building|complex)|building (?:with|of) ' + count + r'\s+' + apartments + r')\b|\b(?:property|building|complex)\s+(?:consists of|comprises|contains|includes)\s+' + count + r'\s+' + apartments + r'\b', text_n))


def infer_property_type(title, text):
    """
    Title-first classification.

    This deliberately avoids allowing generic template phrases such
    as "Land Details" to turn an obvious house into Land.
    """

    title_n = normalize(title)
    text_n = normalize(text)

    if whole_apartment_offer(title, text):
        return 'Apartment Complex'
    if re.match(r'^(?:apartment|studio|condo|condominium)\s+(?:unit\s+|number\s+|no\s+)?\d+\b', title_n):
        return 'Condominium' if re.match(r'^condo', title_n) else 'Apartment'
    if re.match(r'^(?:apartment|studio|condo|condominium|penthouse|unit)\b', title_n) and not re.search(r'\bapartment (?:building|complex)\b', title_n):
        return 'Condominium' if re.match(r'^condo', title_n) else 'Apartment'
    if house_with_apartments(title, text):
        return 'House'
    if re.search(r'\b(?:house|home|villa)\b.{0,35}\bon property land\b', title_n):
        return 'House'

    full_home = bool(re.search(r"\b(houses?|homes?|villas?|townhouses?|townhomes?|town houses?)\b", title_n))
    full_home = full_home or house_with_apartments(title, text)
    complex_signal = bool(re.search(r"\b(apartment complex|apartment building|multi unit|multi family|multifamily|\d+ units)\b", title_n))
    if not full_home and not complex_signal and re.search(r"\b(condos?|condominiums?|apartments?|studio|penthouse)\b", text_n):
        return "Condominium" if re.search(r"\b(condos?|condominiums?)\b", text_n) else "Apartment"

    # --------------------------------------------------------
    # Strong title signals
    # --------------------------------------------------------

    if re.search(
        r"\b("
        r"apartment complex|apartment building|"
        r"multi unit|multiunit|multi family|multifamily|"
        r"entire apartment complex"
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
        r"\b(townhouses?|townhomes?|town houses?|town homes?)\b",
        title_n,
    ):
        return "Townhouse"

    if re.search(
        r"\bvillas?\b",
        title_n,
    ):
        return "Villa"

    if re.search(
        r"\b("
        r"houses?|homes?|residence|residential home|"
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

    if re.search(r"\b(houses?|single family homes?|new homes)\b", text_n):
        return 'House'
    if re.search(r"\b(vacant land|land lot)\b", text_n):
        return 'Land'

    # Beds/baths plus residential language strongly suggests a house.
    has_beds = bool(
        re.search(
            r"(?:\b\d+\s*(?:bed|beds|bedroom|bedrooms|bd|bdr)\b|\bbeds?\s*:?\s*\d+)",
            text_n,
        )
    )

    has_baths = bool(
        re.search(
            r"\b\d+(?:\.\d+)?\s*"
            r"(?:bath|baths|bathroom|bathrooms|ba)\b|\bbaths?\s*:?\s*\d+",
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
        r"\b(townhouses?|townhomes?|town houses?|town homes?)\b",
        combined,
    ):
        return "Townhouse"

    if re.search(
        r"\bvillas?\b",
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
            r"\b(?:beds?|bedrooms?)\s*:\s*(\d+)\b",
            r"\b(\d+)\s*"
            r"(?:bed|beds|bedroom|bedrooms|bd|bdr)\b",
        ],
        text,
    )

    baths = first_match(
        [
            r"\b(?:baths?|bathrooms?)\s*:\s*(\d+(?:\.\d+)?)\b",
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
            "Salina Cerca",
            "Ruby",
            "Safir",
            "Madiki",
            "Koyari",
            "Bushiri",
            "Caya Juan Pablo II",
        ]

        combined = f"{title} {text}".lower()

        for area in known_areas:
            if area.lower() in combined:
                location = area
                break

    return beds, baths, size, location


def area_m2(value):
    """Only parse a source area field, never a price or an arbitrary card number."""
    match = re.search(r"([\d.,]+)\s*(m²|m2|sqm|sq\s*mt|sq\s*ft|sqft|ft²)", str(value), re.I)
    if not match:
        prefix = re.fullmatch(r'\s*(?:m²|m2|sqm)\s*:\s*([\d.,]+)\s*', str(value), re.I)
        if prefix:
            match = re.search(r'([\d.,]+)\s*(m2)', prefix[1] + ' m2')
    if not match:
        return ""
    number = parse_number(match.group(1))
    if not number or number <= 1:
        return ""  # Known source placeholder: 1 m².
    if re.search(r"ft", match.group(2), re.I):
        number *= 0.09290304
    return f"{number:,.2f}".rstrip('0').rstrip('.') + " m²"


def explicit_areas(text):
    unit = r"([\d.,]+\s*(?:m²|m2|sqm|sq\s*mt|sq\s*ft|sqft|ft²))"
    building = first_match([r"\b(?:built[ -]?up(?: area| size)?|build up(?: area| size)?|building(?: area| size)?|living(?: area| space)?|interior(?: area)?|construction area)\s*:?\s*" + unit], text)
    land = first_match([r"\b(?:lot(?: area| size)?|land(?: area| size| space)?|plot(?: area| size)?)\s*:?\s*" + unit], text)
    return area_m2(building), area_m2(land)


def image_from(node, base_url):
    if not node:
        return ""

    for image in node.find_all("img"):
        if image.get('alt', '').lower() in ('beds', 'baths', 'sq mt'):
            continue
        for attribute in ('data-src', 'data-lazy-src', 'data-original', 'src', 'data-srcset', 'srcset'):
            value = image.get(attribute, '').strip()
            if not value or value.startswith('data:'):
                continue
            if 'srcset' in attribute:
                value = value.split(',')[0].strip().split(' ')[0]
            result = absolute_url(base_url, value)
            if looks_like_url(result) and not re.search(r'(?:placeholder|spacer|blank)\.', result, re.I):
                return result

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
    type_hint="",
    residential_income_evidence=False,
):
    title = clean_text(title)
    text = clean_text(text)
    price_meta = price_meta or {}

    if not looks_like_property_title(title) or not looks_like_url(url):
        return None

    if re.search(r"\b(timeshare|each week|per week|per night|modular homes|own land in aruba)\b", normalize(f'{title} {text}')):
        return None
    # A named one-bedroom development unit is not purchase of a whole project.
    if re.search(r"\b(?:\d+ bedroom|unit \d+|\d+bdr)\b", normalize(title)) and re.search(r"new development|pre construction", normalize(text)) and not re.search(r"\b(house|home|villa|townhouse|townhome)\b", normalize(title)):
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

    if type_hint:
        hinted = infer_property_type(type_hint, '')
        if hinted == 'Land' and infer_property_type(title, '') == 'Land':
            property_type = 'Land'
        if hinted == 'Apartment Complex' and residential_income_evidence and not re.search(r'\b(?:apartment|studio|condo|condominium|unit)\s+(?:unit\s+|number\s+|no\s+)?\d+\b', normalize(title)):
            property_type = hinted
        if hinted and property_type not in ('House', 'Villa', 'Townhouse', 'Apartment Complex', 'Condominium', 'Apartment'):
            property_type = hinted

    # Individual apartments and condominiums are intentionally
    # excluded. Whole apartment complexes / multi-unit investment
    # properties remain eligible.
    if not property_type or property_type in EXCLUDED_RESIDENTIAL_UNIT_TYPES:
        return None

    # Standalone new builds remain eligible; residence-complex offerings do not.
    if residence_unit_offer(title, text):
        return None
    if property_type == 'Land':
        beds = baths = ''
    building_area, land_area = explicit_areas(text)
    if property_type == 'Land':
        building_area = ''

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

    if re.search(r"\b(commercial|warehouse|office|retail|hotel)\b", normalize(title)) and not (property_type == "Land" and "residential" in normalize(title + " " + text)):
        strong_commercial = True

    if strong_commercial:
        income = residential_income_evidence or residential_income_offer(title, text, property_type)
        business = bool(re.search(r'\b(warehouse|office|retail|hotel|restaurant|shop|store)\b', normalize(title)))
        business = business or any(re.search(rf'\b{re.escape(excluded)}\s+(?:building|property|space|unit|for sale)\b', combined) for excluded in EXCLUDED_TYPES if excluded != 'commercial')
        if not income or business:
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
        "building_area": building_area,
        "land_area": land_area or (area_m2(size) if property_type == 'Land' else ''),
        "image": image if looks_like_url(image) else "",
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


def address_identity(prop):
    """Conservative exact street/area + house-number evidence across marketing titles."""
    title = normalize(prop.get('name', ''))
    if re.search(r"\b(unit|apt|lot|phase)\s*\d", title):
        return ''
    names = ('tanki leendert', 'tanki flip', 'rooi koochi', 'rooi santo', 'alto vista',
             'pos chiquito', 'sabana basora', 'sabana grandi', 'salina cerca', 'saliña cerca',
             'pavia', 'ponton', 'paradera', 'papaya', 'bubali', 'calabas', 'calbas', 'moko',
             'morgenster', 'macuarima', 'wayaca', 'bakval', 'savaneta', 'siribana', 'safir')
    for name in names:
        match = re.search(r"\b" + re.escape(name) + r"\s+(\d{1,4}[a-z]?(?:\s+[a-z](?=\s|$))?)\b", title)
        if match:
            return name + ':' + match[1].replace(' ', '')
    return ''


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
    if prop.get("history_key"):
        return prop["history_key"]
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

    return f"{location}|{title}"


def development_key(prop):
    if prop.get("type") != "Development" or not prop.get("project_id"):
        return ""
    return normalize(prop["project_id"])


def _legacy_development_key(prop):
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
    result['aliases'] = sorted({canonical_url(u) for u in
        preferred.get('aliases', []) + alternate.get('aliases', []) +
        [preferred.get('url', ''), alternate.get('url', '')] if u})

    for field in (
        "image",
        "description",
        "location",
        "beds",
        "baths",
        "size",
        "building_area",
        "land_area",
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
    address_indexes = {}

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

        address = address_identity(prop)
        if address and address in address_indexes:
            index = address_indexes[address]
            existing = result[index]
            same_kind = (existing.get('type') == 'Land') == (prop.get('type') == 'Land')
            same_beds = not existing.get('beds') or not prop.get('beds') or existing['beds'] == prop['beds']
            if same_kind and same_beds:
                result[index] = choose_preferred_property(existing, prop)
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

        if address:
            address_indexes[address] = len(result)
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

    except FileNotFoundError:
        return default


def save_json(path, data):
    temporary = path + '.tmp'
    with open(temporary, 'w', encoding='utf-8') as file:
        json.dump(data, file, indent=2, ensure_ascii=False)
        file.flush()
        os.fsync(file.fileno())
    os.replace(temporary, path)


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

    if prop.get("history_key") in previous:
        key = prop["history_key"]
        return key, previous[key]

    # An established URL/alias is stronger evidence than a shared address/title.
    url = canonical_url(prop.get('url', ''))
    if url:
        for key, candidate in previous.items():
            if url in {canonical_url(u) for u in candidate.get('aliases', []) + [candidate.get('url', '')] if u}:
                return key, candidate

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

        address = address_identity(prop)
        if address and address == address_identity(candidate):
            same_kind = (candidate.get('type') == 'Land') == (prop.get('type') == 'Land')
            if same_kind and (not prop.get('beds') or not candidate.get('beds') or prop['beds'] == candidate['beds']):
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
            and (not prop.get("location") or not candidate.get("location") or normalize(prop["location"]) == normalize(candidate["location"]))
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
            and canonical_url(url) in {canonical_url(u) for u in candidate.get("aliases", []) + [candidate_url]}
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
    # Apply the latest residence preference to retained activity as well as new scans.
    if re.search(r"\bresidences\b|\bresidence (?:complex|development|project)\b", normalize(f"{prop.get('name', '')} {prop.get('description', '')}")):
        return ''
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

    if event_type == "new":
        html.append("<div style='font-weight:700;color:#174f36;margin-bottom:8px'>NEW PROPERTY</div>")
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

    html.append(f"<div><strong>Location:</strong> {html_escape(prop.get('location') or 'Not provided by source')}</div>")

    if prop.get("type"):
        html.append(
            f"<div><strong>Property type:</strong> "
            f"{html_escape(prop['type'])}"
            f"</div>"
        )

    facts = []

    if prop.get("beds") and prop.get('type') != 'Land':
        facts.append(
            f"{html_escape(prop['beds'])} beds"
        )

    if prop.get("baths") and prop.get('type') != 'Land':
        facts.append(
            f"{html_escape(prop['baths'])} baths"
        )

    if prop.get('type') == 'Land':
        html.append(f"<div><strong>Building area:</strong> Not applicable — land-only listing</div>")
    else:
        html.append(f"<div><strong>Building / built-up area:</strong> {html_escape(prop.get('building_area') or 'Not provided by source')}</div>")
    if prop.get('land_area'):
        html.append(f"<div><strong>Land area:</strong> {html_escape(prop['land_area'])}</div>")
    if prop.get('size') and not prop.get('building_area') and not prop.get('land_area'):
        html.append(f"<div><strong>Source area (scope unspecified):</strong> {html_escape(prop['size'])}</div>")

    evidence = prop.get('development_evidence', [])
    html.append("<div style='margin-top:8px'><strong>Development checks — listing claims:</strong></div>")
    for label in ('Ownership', 'Zoning', 'Building restrictions', 'Apartment construction'):
        claims = [x for x in evidence if x.get('label') == label]
        if not claims:
            html.append(f"<div><strong>{label}:</strong> Unknown — not stated in collected source information</div>")
        else:
            html.append(f"<div><strong>{label}:</strong> " + ' · '.join(html_escape(x.get('excerpt', '')) for x in claims) + '</div>')
            for claim in claims:
                if looks_like_url(claim.get('url', '')):
                    html.append(f"<div><a href='{html_escape(claim['url'])}'>Source evidence</a></div>")
    html.append("<div style='font-size:12px'>Apartment construction permission remains unverified; confirm restrictions before relying on listing claims.</div>")

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
            f"{html_escape(prop.get('broker') or prop['source'])} ({html_escape(prop['source'])})"
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
    else:
        html.append("<div><strong>Image:</strong> Not provided by source; view the property page.</div>")

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
    return ("<div style='margin-top:25px;padding-top:15px;border-top:1px solid #ddd;font-size:12px;color:#555'>"
            "<a href='https://github.com/guidobrugman-svg/aruba-property-agent/actions/workflows/monitor.yml'>"
            "Manage alerts in GitHub</a>. Disable the monitoring workflow to stop alerts.</div>")


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

    if DRY_RUN:
        return False
    payload = {'from': SENDER, 'to': [RECIPIENT], 'subject': subject, 'html': html}
    key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    try:
        response = requests.post('https://api.resend.com/emails',
            headers={'Authorization': f'Bearer {RESEND_API_KEY}',
                     'Content-Type': 'application/json', 'Idempotency-Key': key},
            json=payload, timeout=(4, 15))
        response.raise_for_status()
        print('Email accepted by Resend.')
        return True
    except requests.RequestException as exc:
        # Never include request headers, API key, or provider response body in logs.
        print(f'Email not confirmed ({type(exc).__name__}); keeping durable outbox for retry.')
        return False


# ============================================================
# ALERT EMAILS
# ============================================================

def send_event_email(
    new_items,
    reductions,
    major_changes,
    render_only=False,
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

    if render_only:
        return subject, html
    return send_email(subject, html)


# ============================================================
# DAILY ACTIVITY DIGEST
# ============================================================

def send_daily_digest(
    activity,
    current_count,
    source_health,
    render_only=False,
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
        if data.get("status") in ("ok", "empty")
    ]

    failed = [
        name
        for name, data
        in source_health.items()
        if data.get("status") not in ("ok", "empty")
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
        f"Aruba Property Daily Digest — {aruba_now().strftime('%b %d')} — "
        f"{len(new_items)} new, "
        f"{len(reductions)} reductions"
    )

    if render_only:
        return subject, "".join(html_parts)
    return send_email(subject, "".join(html_parts))


# ============================================================
# DAILY DIGEST TIMING
# ============================================================

def should_send_daily_digest(state):
    local = aruba_now()

    if local.hour < 8:
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

def event_token(item):
    return hashlib.sha256(json.dumps(item, sort_keys=True, default=str).encode()).hexdigest()


def queue_message(state, kind, args, snapshot):
    if any(m['kind'] == kind for m in state.setdefault('outbox', [])):
        return
    # Render once. Retries use the exact same persisted payload/idempotency key.
    subject, html = (send_daily_digest(*args, render_only=True) if kind == 'digest'
                     else send_event_email(*args, render_only=True))
    state['outbox'].append({'kind': kind, 'subject': subject, 'html': html,
                            'snapshot': snapshot, 'created_at': iso_now()})


def flush_outbox(state):
    for message in list(state.get('outbox', [])):
        if not send_email(message['subject'], message['html']):
            continue
        snapshot = message['snapshot']
        if message['kind'] == 'digest':
            for field, tokens in snapshot['activity'].items():
                state['daily_activity'][field] = [x for x in state['daily_activity'][field]
                                                  if event_token(x) not in tokens]
            state['last_digest_date'] = snapshot['date']
            state['last_digest_at'] = iso_now()
        else:
            for field, tokens in snapshot.items():
                state[field] = [x for x in state[field] if event_token(x) not in tokens]
        state['outbox'].remove(message)
        save_json(STATE_FILE, state)


def comparable_price(old, new):
    return (old.get('source') == new.get('source')
            and canonical_url(old.get('url', '')) == canonical_url(new.get('url', ''))
            and bool(old.get('calculated_price')) == bool(new.get('calculated_price'))
            and old.get('price_area_m2') == new.get('price_area_m2'))


def history_dedupe(previous, current):
    """One observation per permanent identity, even for conflicting broker cards."""
    unique = {}
    for prop in current:
        old_key, old = find_previous_record(previous, prop)
        key = ('history', old_key) if old else ('new', property_key(prop))
        if old:
            prop = dict(prop, history_key=old_key)
        unique[key] = choose_preferred_property(unique[key], prop) if key in unique else prop
    return list(unique.values())


def reconcile(previous, current):
    history = dict(previous)
    new_items, reductions, major = [], [], []
    for observed in history_dedupe(previous, current):
        prop = dict(observed)
        old_key, old = find_previous_record(history, prop)
        if old:
            prop['history_key'] = old_key
            prop['detected_at'] = old.get('detected_at', iso_now())
            prop['aliases'] = sorted(set(old.get('aliases', []) + prop.get('aliases', []) +
                                          [canonical_url(old.get('url', '')), canonical_url(prop['url'])]))
            changes = field_changes(old, prop)
            changes.pop('Property type', None)
            # Source/parser switches cannot be advertised as price reductions or major changes.
            price_changed = comparable_price(old, prop) and old.get('price') and prop['price'] < old['price'] - 1
            signature = event_token({'price': prop['price'] if price_changed else None, 'changes': changes})
            candidate = old.get('change_candidate', {})
            if comparable_price(old, prop) and (price_changed or changes):
                if candidate.get('signature') == signature:
                    if price_changed:
                        reductions.append((dict(prop), dict(old)))
                    if changes:
                        major.append((dict(prop), changes))
                else:
                    prop['change_candidate'] = {'signature': signature, 'observed_at': iso_now()}
                    # Preserve confirmed values until another successful observation agrees.
                    for field in ('price', 'beds', 'baths', 'size', 'location', 'status'):
                        if old.get(field):
                            prop[field] = old[field]
            for field in ('image', 'description', 'size', 'location', 'building_area', 'land_area'):
                if not prop.get(field):
                    prop[field] = old.get(field, '')
            if prop.get('type') == 'Land':
                prop.update(beds='', baths='', building_area='')
            key = old_key
        else:
            key = property_key(prop)
            prop['history_key'] = key
            prop['detected_at'] = iso_now()
            new_items.append(prop)
        prop['last_seen_at'] = iso_now()
        # Pending changes retain every confirmed field, including availability.
        # Overwriting status here changes the candidate signature on the next
        # scan and delays confirmation of simultaneous metadata changes.
        prop.setdefault('status', 'available')
        history[key] = prop
    return history, new_items, reductions, major


def monitored_count(history):
    cutoff = now_utc() - timedelta(days=7)
    return sum(p.get('status') == 'available' and
               (parse_datetime(p.get('last_seen_at', '')) or datetime.min.replace(tzinfo=timezone.utc)) >= cutoff
               and p.get('type') not in EXCLUDED_RESIDENTIAL_UNIT_TYPES
               and p.get('price', PRICE_LIMIT + 1) <= PRICE_LIMIT
               for p in history.values())


def main():
    started = time.monotonic()
    if os.environ.get('DELIVER_ONLY') == '1':
        state = load_json(STATE_FILE, {})
        flush_outbox(state)
        return
    source_data = load_json(SOURCES_FILE, {'sources': []})
    sources = [s for s in source_data['sources'] if s.get('enabled')]
    state = load_json(STATE_FILE, {'properties': {}})
    for key in ('pending_new', 'pending_reductions', 'pending_major_changes', 'outbox'):
        state.setdefault(key, [])
    state['daily_activity'] = normalize_daily_activity(state.get('daily_activity'))
    baselined = set(state.get('baselined_sources', []))
    state.setdefault('source_health', {})
    previous = previous_properties(state)
    migration = state.get('schema_version', 1) < 2
    if migration:
        # Retain all legacy activity/queues for audit, without resending historical NEWs.
        state['legacy_queues'] = {k: state[k] for k in ('pending_new', 'pending_reductions', 'pending_major_changes')}
        state['legacy_daily_activity'] = state['daily_activity']
        for k in ('pending_new', 'pending_reductions', 'pending_major_changes'):
            state[k] = []
        state['schema_version'] = 2
    last_deep = parse_datetime(state.get('last_deep_at', ''))
    requested = os.environ.get('SCAN_MODE', 'auto')
    mode = ('deep' if not last_deep or now_utc() - last_deep >= timedelta(hours=1) else 'fast') if requested == 'auto' else requested
    if mode not in ('fast', 'deep'):
        raise ValueError('SCAN_MODE must be auto, fast, or deep')
    print(f'Aruba Property Agent: {mode} scan')
    current, observations = [], []
    newly_baselined = set()
    previously_unclassified = set()
    results = collectors.scan(sources, mode, state['source_health'], sys.modules[__name__])
    for source, (items, observed, health) in results:
        name = source['name']
        old_health = state['source_health'].get(name, {})
        if health.get('source_revision'):
            coverage_revision = old_health.get('coverage_revision', old_health.get('source_revision'))
            if coverage_revision != health['source_revision']:
                newly_baselined.add(name)
            if mode == 'deep' and health['status'] in ('ok', 'empty', 'partial') and (not health.get('pagination_limited', health.get('coverage_limited')) or source.get('adapter') == 'reallinkr'):
                coverage_revision = health['source_revision']
            health['coverage_revision'] = coverage_revision
        if source.get('detail_description_selector') or source.get('adapter') == 'reallinkr':
            previous_review_urls = set(old_health.get('review_observed_urls', []))
            previously_unclassified.update((name, url) for url in previous_review_urls)
            health['review_observed_urls'] = sorted(previous_review_urls | {canonical_url(o['url']) for o in observed if o.get('needs_type_review')})
            if not old_health.get('type_review_initialized'):
                newly_baselined.add(name)
            health['type_review_initialized'] = old_health.get('type_review_initialized', False) or (mode == 'deep' and health['status'] in ('ok', 'empty', 'partial'))
        if health['status'] not in ('ok', 'empty', 'partial'):
            failures = old_health.get('consecutive_failures', 0) + 1
            health['consecutive_failures'] = failures
            minutes = collectors.retry_delay_minutes(source, health['status'], failures)
            health['retry_after'] = (now_utc()+timedelta(minutes=minutes)).isoformat()
            if old_health.get('last_success_at'):
                health['last_success_at'] = old_health['last_success_at']
        else:
            health['last_success_at'] = iso_now()
            health['consecutive_failures'] = 0
        if health['status'] in ('ok', 'empty', 'partial') and name not in baselined:
            newly_baselined.add(name)
            # A shallow first page must not make the rest of an existing broker's
            # inventory look NEW when the first deep scan discovers it later.
            if mode == 'deep':
                baselined.add(name)
        state['source_health'][name] = health
        print(f"{name}: {health['status']}, {len(items)} qualifying / {health['cards_seen']} cards, {health['pages_fetched']} pages, {health['duration_seconds']}s")
        current.extend(items)
        observations.extend((name, x) for x in observed)
    state['baselined_sources'] = sorted(baselined)
    unavailable_urls = {canonical_url(o['url']) for _, o in observations if o.get('status')}
    current = [p for p in current if canonical_url(p['url']) not in unavailable_urls]
    current = cross_source_dedupe(current)
    import development
    current = development.enrich(current, previous, mode, sys.modules[__name__])
    for prop in current:
        if prop.get('residence_complex'):
            observations.append((prop['source'], dict(prop, status='ineligible')))
    current = [p for p in current if not p.get('residence_complex')]
    current = history_dedupe(previous, current)
    history, new, reductions, major = reconcile(previous, current)
    confirmed_urls = {canonical_url(p['url']) for p in current}
    # Explicit unavailability updates history; absence on a shallow scan never means sold.
    for name, observed in observations:
        if not observed.get('status'):
            # Scoped descriptions can establish a whole-property sale that a
            # shorter inventory title alone would classify as an apartment.
            if canonical_url(observed['url']) in confirmed_urls:
                continue
            inferred = infer_property_type(observed.get('name', ''), observed.get('type', ''))
            if inferred in EXCLUDED_RESIDENTIAL_UNIT_TYPES:
                observed['status'] = 'ineligible'
            else:
                continue
        key, old = find_previous_record(history, observed)
        if old and canonical_url(old.get('url', '')) == canonical_url(observed['url']):
            history[key] = dict(old, status=observed['status'], last_checked_at=iso_now())
    state['properties'] = history
    if migration:
        for observed in current:
            key, baseline = find_previous_record(history, observed)
            if key:
                baseline.update({k: v for k,v in observed.items() if k not in ('detected_at', 'history_key', 'aliases')})
                baseline.pop('change_candidate', None)
        state['migration'] = {'at': iso_now(), 'baseline_discoveries': len(new), 'preserved_history': len(previous)}
        new, reductions, major = [], [], []
    if not migration:
        def coverage_discovery(p):
            return p['source'] in newly_baselined or (p['source'], canonical_url(p['url'])) in previously_unclassified or (p['source'], canonical_url(p.get('listing_api_url', ''))) in previously_unclassified
        discoveries = [p for p in new if coverage_discovery(p)]
        state.setdefault('coverage_discoveries', []).extend({'key':property_key(p), 'at':iso_now(), 'source':p['source']} for p in discoveries)
        new = [p for p in new if not coverage_discovery(p)]
    record_daily_activity(state['daily_activity'], new, reductions, major)
    state['pending_new'] = dedupe_pending_new(state['pending_new'] + [dict(p, queued_at=iso_now()) for p in new])
    for field, events, label in [('pending_reductions', reductions, 'old'), ('pending_major_changes', major, 'changes')]:
        state[field] = dedupe_pending_events(state[field] + [{'property': p, label: extra, 'queued_at': iso_now()} for p, extra in events])
    # Revalidate unsent legacy queues against currently confirmed eligibility.
    eligible = {key: p for key, p in history.items() if p.get('status') == 'available' and p.get('type') not in EXCLUDED_RESIDENTIAL_UNIT_TYPES}
    def valid(p):
        key, old = find_previous_record(history, p)
        return bool(old and key in eligible and p.get('type') not in EXCLUDED_RESIDENTIAL_UNIT_TYPES)
    state['pending_new'] = [p for p in state['pending_new'] if valid(p)]
    for field in ('pending_reductions', 'pending_major_changes'):
        state[field] = [e for e in state[field] if valid(e['property'])]
    if state['pending_new']:
        snapshot = {'pending_new': [event_token(x) for x in state['pending_new']]}
        queue_message(state, 'new', (state['pending_new'], [], []), snapshot)
    cutoff = now_utc() - timedelta(minutes=BATCH_MINUTES)
    if any((parse_datetime(x.get('queued_at','')) or now_utc()) <= cutoff for x in state['pending_reductions'] + state['pending_major_changes']):
        snapshot = {k: [event_token(x) for x in state[k]] for k in ('pending_reductions', 'pending_major_changes')}
        queue_message(state, 'changes', ([], [(x['property'], x['old']) for x in state['pending_reductions']],
                                        [(x['property'], x['changes']) for x in state['pending_major_changes']]), snapshot)
    if should_send_daily_digest(state):
        snapshot = {'date': aruba_now().date().isoformat(), 'activity': {k: [event_token(x) for x in v] for k,v in state['daily_activity'].items()}}
        digest_activity = empty_daily_activity()
        for field, entries in state['daily_activity'].items():
            for entry in entries:
                target = entry if field == 'new' else entry.get('property', {})
                if valid(target):
                    key, latest = find_previous_record(history, target)
                    digest_activity[field].append(latest if field == 'new' else dict(entry, property=latest))
        queue_message(state, 'digest', (digest_activity, monitored_count(history), state['source_health']), snapshot)
    if mode == 'deep':
        state['last_deep_at'] = iso_now()
    state['last_scan'] = {'mode': mode, 'at': iso_now(), 'qualifying_observed': len(current), 'new': len(new),
                          'reductions': len(reductions), 'major_changes': len(major),
                          'duration_seconds': round(time.monotonic()-started, 2)}
    # Persist discovery and frozen messages BEFORE external email side effects.
    save_json(STATE_FILE, state)
    if os.environ.get('DEFER_DELIVERY') != '1':
        flush_outbox(state)
    print(json.dumps(state['last_scan']))
    print(f'Known recent qualifying properties: {monitored_count(history)}; durable messages: {len(state["outbox"])}')


if __name__ == '__main__':
    main()
