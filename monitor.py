import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

URL = "https://www.arubabrokers.com/property-status/for-sale/"
MAX_PRICE = 650000

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

print("Aruba Property Agent starting...")
print("Checking Aruba Brokers...")

response = requests.get(URL, headers=HEADERS, timeout=30)
response.raise_for_status()

soup = BeautifulSoup(response.text, "html.parser")

properties = []

# Look for links to individual property pages.
for link in soup.find_all("a", href=True):
    href = urljoin(URL, link["href"])

    if "/property/" not in href:
        continue

    # Find the nearest useful listing container.
    card = link
    for _ in range(6):
        if card.parent:
            card = card.parent

        card_text = card.get_text(" ", strip=True)

        if "$" in card_text and len(card_text) < 2000:
            break

    card_text = card.get_text(" ", strip=True)

    # Price
    price_match = re.search(r"\$[\d,]+", card_text)

    if not price_match:
        continue

    price = int(
        price_match.group(0)
        .replace("$", "")
        .replace(",", "")
    )

    # Maximum price
    if price > MAX_PRICE:
        continue

    # Must explicitly be an active For Sale listing.
    # This also accepts "For Sale New Construction".
    if not re.search(r"\bFor Sale\b", card_text, re.IGNORECASE):
        continue

    # Exclude commercial properties.
    if re.search(r"\bCommercial\b", card_text, re.IGNORECASE):
        continue

    # Property title
    headings = card.find_all(["h2", "h3", "h4"])

    title = ""

    for heading in headings:
        candidate = heading.get_text(" ", strip=True)

        if candidate:
            title = candidate
            break

    if not title:
        title = link.get_text(" ", strip=True)

    if not title:
        title = "Untitled property"

    properties.append({
        "title": title,
        "price": price,
        "url": href,
        "details": card_text
    })


# Remove duplicate URLs.
unique_properties = {}

for property_item in properties:
    unique_properties[property_item["url"]] = property_item

properties = list(unique_properties.values())

print()
print("=" * 60)
print(f"QUALIFYING PROPERTIES FOUND: {len(properties)}")
print("=" * 60)

for number, property_item in enumerate(properties, start=1):

    print()
    print(f"{number}. {property_item['title']}")
    print(f"   Price: ${property_item['price']:,}")
    print(f"   URL: {property_item['url']}")
    print(f"   Details: {property_item['details'][:500]}")

print()
print("Monitor test completed successfully.")
