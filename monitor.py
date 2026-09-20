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
    price_match = re.search(r"\$\s*([\d,]+)", text)

    if not price_match:
        return None

    return int(price_match.group(1).replace(",", ""))


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

        price = get_price(text)

        if price is None:
            continue

        if price > MAX_PRICE:
            continue

        if is_excluded(text):
            continue

        properties.append({
            "title": title,
            "price": price,
            "url": url,
            "details": text,
            "source": source["name"],
            "source_priority": source["priority"]
        })

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

    for link in soup.find_all("a", href=True):

        url = urljoin(source["url"], link["href"])

        if not url.startswith("http"):
            continue

        text = link.parent.get_text(" ", strip=True)

        if not text:
            continue

        price = get_price(text)

        if price is None:
            continue

        if price > MAX_PRICE:
            continue

        if is_excluded(text):
            continue

        title = link.get_text(" ", strip=True)

        if not title:
            continue

        if len(title) < 5:
            continue

        properties.append({
            "title": title,
            "price": price,
            "url": url,
            "details": text,
            "source": source["name"],
            "source_priority": source["priority"]
        })

    return properties


def scrape_source(source):
    print()
    print(f"Checking source: {source['name']}")
    print(f"URL: {source['url']}")

    if source["name"] == "Aruba Brokers":
        properties = scrape_aruba_brokers(source)
    else:
        properties = scrape_generic_source(source)

    print(f"Properties found: {len(properties)}")

    return properties


def normalize_url(url):
    return url.rstrip("/").lower()


def deduplicate_properties(properties):
    deduplicated = {}

    for property_item in properties:

        key = normalize_url(property_item["url"])

        if key not in deduplicated:
            deduplicated[key] = property_item
            continue

        existing = deduplicated[key]

        if property_item["source_priority"] < existing["source_priority"]:
            deduplicated[key] = property_item

    return list(deduplicated.values())


def build_new_property_email(properties):
    rows = []

    for property_item in properties:

        title = escape(property_item["title"])
        price = property_item["price"]
        url = escape(property_item["url"], quote=True)
        details = escape(property_item["details"])
        source = escape(property_item["source"])

        rows.append(
            f"""
            <div style="margin-bottom:32px;
                        padding-bottom:24px;
                        border-bottom:1px solid #dddddd;">

                <h2 style="margin-bottom:8px;">
                    NEW PROPERTY — ${price:,}
                </h2>

                <p>
                    <strong>{title}</strong>
                </p>

                <p>{details}</p>

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
                 color:#222;">

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

        title = escape(change["title"])
        url = escape(change["url"], quote=True)

        old_price = change["old_price"]
        new_price = change["new_price"]
        reduction_percent = change["reduction_percent"]
        source = escape(change["source"])

        rows.append(
            f"""
            <div style="margin-bottom:32px;
                        padding-bottom:24px;
                        border-bottom:1px solid #dddddd;">

                <h2>PRICE REDUCTION</h2>

                <p>
                    <strong>{title}</strong>
                </p>

                <p style="font-size:18px;">
                    ${old_price:,}
                    &nbsp;&rarr;&nbsp;
                    <strong>${new_price:,}</strong>
                </p>

                <p>
                    Reduction:
                    <strong>{reduction_percent:.1f}%</strong>
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
                 color:#222;">

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

previous_state = load_json_file(STATE_FILE, {})
source_config = load_json_file(SOURCES_FILE, {"sources": []})

sources = [
    source
    for source in source_config.get("sources", [])
    if source.get("enabled", True)
]


all_properties = []
successful_sources = 0


for source in sources:

    try:

        source_properties = scrape_source(source)

        all_properties.extend(source_properties)

        successful_sources += 1

    except Exception as error:

        print()
        print(f"ERROR checking {source['name']}:")
        print(str(error))


if successful_sources == 0:

    print()
    print("ERROR: No sources could be checked successfully.")
    print("Existing state will NOT be changed.")
    print("Monitor test stopped safely.")

    raise SystemExit(1)


properties = deduplicate_properties(all_properties)


if not properties:

    print()
    print("WARNING: No qualifying properties were found.")
    print("Existing state will NOT be replaced.")

    raise SystemExit(0)


current_state = {}


for property_item in properties:

    key = normalize_url(property_item["url"])

    current_state[key] = {
        "title": property_item["title"],
        "price": property_item["price"],
        "details": property_item["details"],
        "source": property_item["source"],
        "url": property_item["url"]
    }


new_properties = []


for key, property_item in current_state.items():

    if key not in previous_state:

        new_properties.append(property_item)


price_reductions = []


for key, property_item in current_state.items():

    if key not in previous_state:
        continue

    old_price = previous_state[key]["price"]
    new_price = property_item["price"]

    if new_price < old_price:

        reduction_percent = (
            (old_price - new_price) / old_price
        ) * 100

        price_reductions.append({
            "url": property_item["url"],
            "title": property_item["title"],
            "old_price": old_price,
            "new_price": new_price,
            "reduction_percent": reduction_percent,
            "source": property_item["source"]
        })


with open(STATE_FILE, "w", encoding="utf-8") as file:

    json.dump(
        current_state,
        file,
        indent=2,
        ensure_ascii=False
    )


print()
print("=" * 60)
print(f"CURRENT QUALIFYING PROPERTIES: {len(current_state)}")
print(f"NEW PROPERTIES: {len(new_properties)}")
print(f"PRICE REDUCTIONS: {len(price_reductions)}")
print("=" * 60)


for property_item in new_properties:

    print()
    print("NEW PROPERTY:")
    print(f"Title: {property_item['title']}")
    print(f"Price: ${property_item['price']:,}")
    print(f"Source: {property_item['source']}")
    print(f"URL: {property_item['url']}")


for change in price_reductions:

    print()
    print("PRICE REDUCTION:")
    print(f"Title: {change['title']}")
    print(f"Old price: ${change['old_price']:,}")
    print(f"New price: ${change['new_price']:,}")
    print(f"Reduction: {change['reduction_percent']:.1f}%")
    print(f"Source: {change['source']}")
    print(f"URL: {change['url']}")


if new_properties:

    print()
    print("Sending new-property email...")

    subject = (
        f"Aruba Property Alert — "
        f"{len(new_properties)} New Property"
        f"{'ies' if len(new_properties) != 1 else ''}"
    )

    html = build_new_property_email(new_properties)

    send_email(subject, html)


if price_reductions:

    print()
    print("Sending price-reduction email...")

    subject = (
        f"Aruba Property Alert — "
        f"{len(price_reductions)} Price Reduction"
        f"{'s' if len(price_reductions) != 1 else ''}"
    )

    html = build_price_reduction_email(price_reductions)

    send_email(subject, html)


print()
print("Monitor test completed successfully.")
