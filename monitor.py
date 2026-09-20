import json
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from html import escape

URL = "https://www.arubabrokers.com/property-status/for-sale/"
MAX_PRICE = 650000
STATE_FILE = "state.json"

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


def build_new_property_email(properties):
    rows = []

    for property_item in properties:
        title = escape(property_item["title"])
        price = property_item["price"]
        url = escape(property_item["url"], quote=True)
        details = escape(property_item["details"])

        rows.append(
            f"""
            <div style="margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid #dddddd;">
                <h2 style="margin-bottom:8px;">NEW PROPERTY — ${price:,}</h2>

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
                    Source: Aruba Brokers
                </p>
            </div>
            """
        )

    return f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family:Arial,sans-serif;line-height:1.5;color:#222;">
        <h1>Aruba Property Alert</h1>

        <p>
            {len(properties)} new qualifying property
            {"has" if len(properties) == 1 else "have"} been detected.
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

        rows.append(
            f"""
            <div style="margin-bottom:32px;padding-bottom:24px;border-bottom:1px solid #dddddd;">
                <h2 style="margin-bottom:8px;">
                    PRICE REDUCTION
                </h2>

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
                    Source: Aruba Brokers
                </p>
            </div>
            """
        )

    return f"""
    <!DOCTYPE html>
    <html>
    <body style="font-family:Arial,sans-serif;line-height:1.5;color:#222;">
        <h1>Aruba Property Alert</h1>

        <p>
            {len(changes)} qualifying property
            {"has" if len(changes) == 1 else "have"} had a price reduction.
        </p>

        {"".join(rows)}

        <p style="font-size:12px;color:#777;">
            Aruba Property Agent
        </p>
    </body>
    </html>
    """


print("Aruba Property Agent starting...")
print("Checking Aruba Brokers...")

# Load previous state.
if os.path.exists(STATE_FILE):
    with open(STATE_FILE, "r", encoding="utf-8") as file:
        previous_state = json.load(file)
else:
    previous_state = {}

response = requests.get(
    URL,
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
    url = urljoin(URL, link["href"])
    text = article.get_text(" ", strip=True)

    price_match = re.search(r"\$\s*([\d,]+)", text)

    if not price_match:
        continue

    price = int(
        price_match.group(1).replace(",", "")
    )

    if price > MAX_PRICE:
        continue

    if re.search(r"\bCommercial\b", text, re.IGNORECASE):
        continue

    properties.append({
        "title": title,
        "price": price,
        "url": url,
        "details": text
    })


# Remove duplicate URLs.
current_state = {}

for property_item in properties:
    current_state[property_item["url"]] = {
        "title": property_item["title"],
        "price": property_item["price"],
        "details": property_item["details"]
    }


# Find genuinely new properties.
new_properties = []

for url, property_item in current_state.items():

    if url not in previous_state:
        new_properties.append({
            "url": url,
            **property_item
        })


# Find price reductions.
price_reductions = []

for url, property_item in current_state.items():

    if url not in previous_state:
        continue

    old_price = previous_state[url]["price"]
    new_price = property_item["price"]

    if new_price < old_price:

        reduction_percent = (
            (old_price - new_price) / old_price
        ) * 100

        price_reductions.append({
            "url": url,
            "title": property_item["title"],
            "old_price": old_price,
            "new_price": new_price,
            "reduction_percent": reduction_percent
        })


# Save the current state.
with open(STATE_FILE, "w", encoding="utf-8") as file:
    json.dump(current_state, file, indent=2, ensure_ascii=False)


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
    print(f"URL: {property_item['url']}")


for change in price_reductions:

    print()
    print("PRICE REDUCTION:")
    print(f"Title: {change['title']}")
    print(f"Old price: ${change['old_price']:,}")
    print(f"New price: ${change['new_price']:,}")
    print(f"Reduction: {change['reduction_percent']:.1f}%")
    print(f"URL: {change['url']}")


# Send alerts only when something actually changed.
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
