import json
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from html import escape


STATE_FILE = "state.json"
SOURCES_FILE = "SOURCES.json"

MAX_PRICE = 650000

EMAIL_RECIPIENT = "guidobrugman@live.nl"
EMAIL_FROM = "Aruba Property Agent <alerts@arubapropertywatch.com>"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}


def send_email(subject, html):
    api_key = os.environ.get("RESEND_API_KEY")

    if not api_key:
        print("RESEND_API_KEY is not available.")
        return False

    response = requests.post(
        "https://api.resend.com/emails",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "from": EMAIL_FROM,
            "to": [EMAIL_RECIPIENT],
            "subject": subject,
            "html": html,
        },
        timeout=30,
    )

    print(f"Resend HTTP status: {response.status_code}")

    if response.ok:
        print("Email sent successfully.")
        return True

    print("Resend error:")
    print(response.text)

    return False


def load_json_file(filename, default):
    if not os.path.exists(filename):
        return default

    with open(filename, "r", encoding="utf-8") as file:
        return json.load(file)


def get_price(text):
    matches = re.findall(r"\$\s*([\d,]+)", text)

    if not matches:
        return None

    prices = []

    for match in matches:
        try:
            prices.append(int(match.replace(",", "")))
        except ValueError:
            pass

    if not prices:
        return None

    return min(prices)


def is_excluded(text):
    text_lower = text.lower()

    excluded_terms = [
        "commercial building",
        "commercial property",
        "warehouse",
        "office",
        "retail",
    ]

    return any(term in text_lower for term in excluded_terms)


def is_unavailable(text):
    text_lower = text.lower()

    unavailable_terms = [
        "under contract",
        "sale in progress",
        "sold",
        "withdrawn",
        "unavailable",
        "off market",
    ]

    return any(term in text_lower for term in unavailable_terms)


def get_image_from_container(container, base_url):
    if not container:
        return None

    image = container.find("img")

    if not image:
        return None

    image_url = (
        image.get("src")
        or image.get("data-src")
        or image.get("data-lazy-src")
    )

    if not image_url:
        srcset = image.get("srcset")

        if srcset:
            image_url = srcset.split(",")[0].strip().split(" ")[0]

    if not image_url:
        return None

    return urljoin(base_url, image_url)


def extract_bedrooms(text):
    patterns = [
        r"\bBeds?\s*:\s*(\d+)",
        r"\b(\d+)\s*(?:BDR|BDRS|Bedroom|Bedrooms)\b",
        r"\bBed\s*:\s*(\d+)",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return int(match.group(1))

    return None


def extract_bathrooms(text):
    patterns = [
        r"\bBaths?\s*:\s*(\d+(?:\.\d+)?)",
        r"\b(\d+(?:\.\d+)?)\s*(?:Bath|Baths|Bathroom|Bathrooms)\b",
    ]

    for pattern in patterns:
        match = re.search(pattern, text, re.IGNORECASE)

        if match:
            return match.group(1)

    return None


def extract_size(text):
    patterns = [
        r"\bm2\s*:\s*([\d,.]+)",
        r"\b([\d,.]+)\s*(?:m²|m2|Sq\s*Mt|Sq\s*M)\b",
    ]

    for pattern in patterns:
        match = re.search(
            pattern,
            text,
            re.IGNORECASE
        )

        if match:
            return match.group(1)

    return None


def extract_property_type(text):
    text_lower = text.lower()

    property_types = [
        ("single family home", "House"),
        ("single-family home", "House"),
        ("villa", "Villa"),
        ("townhouse", "Townhouse"),
        ("townhomes", "Townhouse"),
        ("condos/apartments", "Condo / Apartment"),
        ("condominium", "Condo / Apartment"),
        ("condo", "Condo"),
        ("apartment complex", "Apartment Complex"),
        ("apartment", "Apartment"),
        ("land", "Land"),
        ("development", "Development"),
        ("new construction", "New Construction"),
        ("commercial", "Commercial"),
    ]

    for search_term, display_name in property_types:
        if search_term in text_lower:
            return display_name

    return None


def extract_location(text):
    locations = [
        "Noord",
        "Oranjestad",
        "Palm Beach",
        "Eagle Beach",
        "Malmok",
        "Savaneta",
        "Paradera",
        "San Nicolas",
        "Santa Cruz",
        "Ponton",
        "Rooi Santo",
        "Bushiri",
        "Tanki Leendert",
        "Turibana",
        "Pos Chiquito",
        "Kudawecha",
        "Sabana Basora",
        "Sabana Liber",
        "Cas Ariba",
        "Washington",
        "Alto Vista",
        "Bubali",
        "Wayaca",
        "Tanki Flip",
        "Brasil",
        "Pavia",
        "Dakota",
        "Hooiberg",
        "Piedra Plat",
        "Balashi",
    ]

    found = []

    for location in locations:

        if re.search(
            r"\b" + re.escape(location) + r"\b",
            text,
            re.IGNORECASE
        ):
            if location.lower() not in [
                item.lower() for item in found
            ]:
                found.append(location)

    if found:
        return ", ".join(found[:2])

    return None


def clean_title(title):
    title = re.sub(r"\s+", " ", title).strip()

    if not title:
        return "Untitled property"

    title = re.sub(
        r"\s+\|\s+\d+\s*BDR.*$",
        "",
        title,
        flags=re.IGNORECASE
    )

    title = re.sub(
        r"\s+\|\s+.*?\$[\d,]+.*$",
        "",
        title,
        flags=re.IGNORECASE
    )

    return title.strip()


def build_property_record(
    title,
    price,
    url,
    details,
    source,
    source_priority,
    image=None
):
    return {
        "title": clean_title(title),
        "price": price,
        "url": url,
        "details": details,
        "source": source,
        "source_priority": source_priority,
        "image": image,
        "bedrooms": extract_bedrooms(details),
        "bathrooms": extract_bathrooms(details),
        "size": extract_size(details),
        "property_type": extract_property_type(details),
        "location": extract_location(details),
    }


def scrape_aruba_brokers(source):
    response = requests.get(
        source["url"],
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    properties = []

    for article in soup.find_all("article"):

        heading = article.find("h2")

        if not heading:
            continue

        link = heading.find("a", href=True)

        if not link:
            continue

        title = heading.get_text(" ", strip=True)
        url = urljoin(source["url"], link["href"])
        text = article.get_text(" ", strip=True)

        if is_unavailable(text):
            continue

        price = get_price(text)

        if price is None:
            continue

        if price > MAX_PRICE:
            continue

        if is_excluded(text):
            continue

        image = get_image_from_container(
            article,
            source["url"]
        )

        properties.append(
            build_property_record(
                title=title,
                price=price,
                url=url,
                details=text,
                source=source["name"],
                source_priority=source["priority"],
                image=image
            )
        )

    return properties


def scrape_bluefin(source):
    response = requests.get(
        source["url"],
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    properties = []

    seen_urls = set()

    for link in soup.find_all("a", href=True):

        href = link.get("href", "")

        if "/property/" not in href:
            continue

        url = urljoin(source["url"], href)

        if url in seen_urls:
            continue

        seen_urls.add(url)

        container = link

        for _ in range(5):

            if not container.parent:
                break

            container = container.parent

            text = container.get_text(
                " ",
                strip=True
            )

            if "$" in text and len(text) > 40:
                break

        text = container.get_text(
            " ",
            strip=True
        )

        if not text:
            continue

        if is_unavailable(text):
            continue

        price = get_price(text)

        if price is None:
            continue

        if price > MAX_PRICE:
            continue

        if is_excluded(text):
            continue

        title = None

        for candidate in container.find_all(
            ["h1", "h2", "h3", "h4"]
        ):

            candidate_text = candidate.get_text(
                " ",
                strip=True
            )

            if (
                candidate_text
                and candidate_text.lower() != "details"
            ):
                title = candidate_text
                break

        if not title:

            link_text = link.get_text(
                " ",
                strip=True
            )

            if (
                link_text
                and link_text.lower() != "details"
            ):
                title = link_text

        if not title:
            continue

        image = get_image_from_container(
            container,
            source["url"]
        )

        properties.append(
            build_property_record(
                title=title,
                price=price,
                url=url,
                details=text,
                source=source["name"],
                source_priority=source["priority"],
                image=image
            )
        )

    return properties


def scrape_generic_source(source):
    response = requests.get(
        source["url"],
        headers=HEADERS,
        timeout=30
    )

    response.raise_for_status()

    soup = BeautifulSoup(response.text, "html.parser")

    properties = []

    seen_urls = set()

    for link in soup.find_all("a", href=True):

        url = urljoin(
            source["url"],
            link["href"]
        )

        if not url.startswith("http"):
            continue

        if url in seen_urls:
            continue

        seen_urls.add(url)

        container = link.parent

        if not container:
            continue

        text = container.get_text(
            " ",
            strip=True
        )

        if not text:
            continue

        if is_unavailable(text):
            continue

        price = get_price(text)

        if price is None:
            continue

        if price > MAX_PRICE:
            continue

        if is_excluded(text):
            continue

        title = link.get_text(
            " ",
            strip=True
        )

        if not title:
            continue

        if title.lower() == "details":
            for heading in container.find_all(
                ["h1", "h2", "h3", "h4"]
            ):
                candidate = heading.get_text(
                    " ",
                    strip=True
                )

                if (
                    candidate
                    and candidate.lower() != "details"
                ):
                    title = candidate
                    break

        if len(title) < 5:
            continue

        image = get_image_from_container(
            container,
            source["url"]
        )

        properties.append(
            build_property_record(
                title=title,
                price=price,
                url=url,
                details=text,
                source=source["name"],
                source_priority=source["priority"],
                image=image
            )
        )

    return properties


def scrape_source(source):
    print()
    print(f"Checking source: {source['name']}")
    print(f"URL: {source['url']}")

    if source["name"] == "Aruba Brokers":
        properties = scrape_aruba_brokers(source)

    elif source["name"] == "Bluefin Realtors":
        properties = scrape_bluefin(source)

    else:
        properties = scrape_generic_source(source)

    print(f"Properties found: {len(properties)}")

    return properties


def normalize_url(url):
    return url.rstrip("/").lower()


def normalize_text(text):
    if not text:
        return ""

    text = text.lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\s+", " ", text)

    return text.strip()


def property_identity(property_item):
    url_key = normalize_url(property_item["url"])

    title_key = normalize_text(
        property_item.get("title", "")
    )

    location_key = normalize_text(
        property_item.get("location", "")
    )

    price_key = str(
        property_item.get("price", "")
    )

    return (
        title_key,
        location_key,
        price_key
    )


def deduplicate_properties(properties):
    deduplicated = {}

    for property_item in properties:

        url_key = normalize_url(
            property_item["url"]
        )

        identity_key = property_identity(
            property_item
        )

        keys_to_check = [
            f"url:{url_key}",
            f"identity:{identity_key}"
        ]

        existing_key = None

        for key in keys_to_check:
            if key in deduplicated:
                existing_key = key
                break

        if existing_key is None:

            deduplicated[
                f"url:{url_key}"
            ] = property_item

            continue

        existing = deduplicated[
            existing_key
        ]

        if (
            property_item["source_priority"]
            <
            existing["source_priority"]
        ):
            deduplicated[
                existing_key
            ] = property_item

    return list(
        deduplicated.values()
    )


def build_property_details_html(property_item):
    rows = []

    location = property_item.get("location")
    property_type = property_item.get(
        "property_type"
    )
    bedrooms = property_item.get("bedrooms")
    bathrooms = property_item.get("bathrooms")
    size = property_item.get("size")

    if location:
        rows.append(
            f"<strong>Location:</strong> "
            f"{escape(location)}"
        )

    if property_type:
        rows.append(
            f"<strong>Property type:</strong> "
            f"{escape(property_type)}"
        )

    if bedrooms is not None:
        rows.append(
            f"<strong>Bedrooms:</strong> "
            f"{bedrooms}"
        )

    if bathrooms is not None:
        rows.append(
            f"<strong>Bathrooms:</strong> "
            f"{escape(str(bathrooms))}"
        )

    if size:
        rows.append(
            f"<strong>Size:</strong> "
            f"{escape(str(size))} m²"
        )

    if not rows:
        rows.append(
            escape(property_item["details"])
        )

    return "<br>".join(rows)


def build_new_property_email(properties):
    rows = []

    for property_item in properties:

        title = escape(
            property_item["title"]
        )

        price = property_item["price"]

        url = escape(
            property_item["url"],
            quote=True
        )

        source = escape(
            property_item["source"]
        )

        details_html = (
            build_property_details_html(
                property_item
            )
        )

        image_html = ""

        if property_item.get("image"):

            image_url = escape(
                property_item["image"],
                quote=True
            )

            image_html = f"""
                <p>
                    <img src="{image_url}"
                         alt="{title}"
                         style="max-width:100%;
                                width:600px;
                                height:auto;
                                border-radius:8px;">
                </p>
            """

        rows.append(
            f"""
            <div style="margin-bottom:36px;
                        padding-bottom:28px;
                        border-bottom:1px solid #dddddd;">

                <h2 style="margin-bottom:8px;">
                    NEW PROPERTY — ${price:,}
                </h2>

                <h3 style="margin-bottom:12px;">
                    {title}
                </h3>

                {image_html}

                <p style="line-height:1.7;">
                    {details_html}
                </p>

                <p>
                    <a href="{url}"
                       style="display:inline-block;
                              padding:12px 20px;
                              background:#111827;
                              color:#ffffff;
                              text-decoration:none;
                              border-radius:6px;">
                        VIEW PROPERTY
                    </a>
                </p>

                <p style="font-size:12px;color:#666666;">
                    Source: {source}
                </p>

            </div>
            """
        )

    return f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family:Arial,sans-serif;
                 line-height:1.5;
                 color:#222;
                 max-width:700px;
                 margin:0 auto;
                 padding:20px;">

        <h1>Aruba Property Alert</h1>

        <p>
            {len(properties)} new qualifying property
            {"has" if len(properties) == 1 else "have"}
            been detected.
        </p>

        {"".join(rows)}

        <p style="font-size:12px;color:#777;">
            Aruba Property Agent
        </p>

    </body>
    </html>
    """


def build_price_reduction_email(changes):
    rows = []

    for change in changes:

        title = escape(
            change["title"]
        )

        url = escape(
            change["url"],
            quote=True
        )

        source = escape(
            change["source"]
        )

        old_price = change["old_price"]
        new_price = change["new_price"]
        reduction_percent = (
            change["reduction_percent"]
        )

        rows.append(
            f"""
            <div style="margin-bottom:36px;
                        padding-bottom:28px;
                        border-bottom:1px solid #dddddd;">

                <h2>PRICE REDUCTION</h2>

                <h3>{title}</h3>

                <p style="font-size:20px;">
                    ${old_price:,}
                    &nbsp;&rarr;&nbsp;
                    <strong>${new_price:,}</strong>
                </p>

                <p>
                    Reduction:
                    <strong>
                        {reduction_percent:.1f}%
                    </strong>
                </p>

                <p>
                    <a href="{url}"
                       style="display:inline-block;
                              padding:12px 20px;
                              background:#111827;
                              color:#ffffff;
                              text-decoration:none;
                              border-radius:6px;">
                        VIEW PROPERTY
                    </a>
                </p>

                <p style="font-size:12px;color:#666666;">
                    Source: {source}
                </p>

            </div>
            """
        )

    return f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family:Arial,sans-serif;
                 line-height:1.5;
                 color:#222;
                 max-width:700px;
                 margin:0 auto;
                 padding:20px;">

        <h1>Aruba Property Alert</h1>

        <p>
            {len(changes)} qualifying property
            {"has" if len(changes) == 1 else "have"}
            had a price reduction.
        </p>

        {"".join(rows)}

        <p style="font-size:12px;color:#777;">
            Aruba Property Agent
        </p>

    </body>
    </html>
    """


print("Aruba Property Agent starting...")


previous_state = load_json_file(
    STATE_FILE,
    {}
)

source_config = load_json_file(
    SOURCES_FILE,
    {"sources": []}
)

sources = [
    source
    for source in source_config.get(
        "sources",
        []
    )
    if source.get(
        "enabled",
        True
    )
]


all_properties = []
successful_sources = 0


for source in sources:

    try:

        source_properties = scrape_source(
            source
        )

        all_properties.extend(
            source_properties
        )

        successful_sources += 1

    except Exception as error:

        print()
        print(
            f"ERROR checking "
            f"{source['name']}:"
        )

        print(str(error))


if successful_sources == 0:

    print()
    print(
        "ERROR: No sources could be "
        "checked successfully."
    )

    print(
        "Existing state will NOT be changed."
    )

    print("Monitor stopped safely.")

    raise SystemExit(1)


properties = deduplicate_properties(
    all_properties
)


if not properties:

    print()
    print(
        "WARNING: No qualifying "
        "properties were found."
    )

    print(
        "Existing state will NOT be replaced."
    )

    raise SystemExit(0)


current_state = {}


for property_item in properties:

    key = normalize_url(
        property_item["url"]
    )

    current_state[key] = {
        "title": property_item["title"],
        "price": property_item["price"],
        "details": property_item["details"],
        "source": property_item["source"],
        "url": property_item["url"],
        "image": property_item.get("image"),
        "bedrooms": property_item.get(
            "bedrooms"
        ),
        "bathrooms": property_item.get(
            "bathrooms"
        ),
        "size": property_item.get(
            "size"
        ),
        "property_type": property_item.get(
            "property_type"
        ),
        "location": property_item.get(
            "location"
        )
    }


new_properties = []


for key, property_item in current_state.items():

    if key not in previous_state:

        new_properties.append(
            property_item
        )


price_reductions = []


for key, property_item in current_state.items():

    if key not in previous_state:
        continue

    old_price = previous_state[key]["price"]
    new_price = property_item["price"]

    if new_price < old_price:

        reduction_percent = (
            (
                old_price - new_price
            )
            /
            old_price
        ) * 100

        price_reductions.append({
            "url": property_item["url"],
            "title": property_item["title"],
            "old_price": old_price,
            "new_price": new_price,
            "reduction_percent":
                reduction_percent,
            "source": property_item["source"]
        })


with open(
    STATE_FILE,
    "w",
    encoding="utf-8"
) as file:

    json.dump(
        current_state,
        file,
        indent=2,
        ensure_ascii=False
    )


print()
print("=" * 60)
print(
    f"CURRENT QUALIFYING PROPERTIES: "
    f"{len(current_state)}"
)
print(
    f"NEW PROPERTIES: "
    f"{len(new_properties)}"
)
print(
    f"PRICE REDUCTIONS: "
    f"{len(price_reductions)}"
)
print("=" * 60)


for property_item in new_properties:

    print()
    print("NEW PROPERTY:")

    print(
        f"Title: "
        f"{property_item['title']}"
    )

    print(
        f"Price: "
        f"${property_item['price']:,}"
    )

    print(
        f"Source: "
        f"{property_item['source']}"
    )

    print(
        f"URL: "
        f"{property_item['url']}"
    )


for change in price_reductions:

    print()
    print("PRICE REDUCTION:")

    print(
        f"Title: "
        f"{change['title']}"
    )

    print(
        f"Old price: "
        f"${change['old_price']:,}"
    )

    print(
        f"New price: "
        f"${change['new_price']:,}"
    )

    print(
        f"Reduction: "
        f"{change['reduction_percent']:.1f}%"
    )

    print(
        f"Source: "
        f"{change['source']}"
    )

    print(
        f"URL: "
        f"{change['url']}"
    )


if new_properties:

    print()
    print(
        "Sending new-property email..."
    )

    subject = (
        "Aruba Property Alert — "
        f"{len(new_properties)} "
        "New Property"
        f"{'ies' if len(new_properties) != 1 else ''}"
    )

    html = build_new_property_email(
        new_properties
    )

    send_email(
        subject,
        html
    )


if price_reductions:

    print()
    print(
        "Sending price-reduction email..."
    )

    subject = (
        "Aruba Property Alert — "
        f"{len(price_reductions)} "
        "Price Reduction"
        f"{'s' if len(price_reductions) != 1 else ''}"
    )

    html = build_price_reduction_email(
        price_reductions
    )

    send_email(
        subject,
        html
    )


print()
print(
    "Monitor test completed successfully."
)
