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

for heading in soup.find_all("h2"):

    link = heading.find("a", href=True)

    if not link:
        continue

    href = urljoin(URL, link["href"])

    if "/property/" not in href:
        continue

    title = heading.get_text(" ", strip=True)

    # Find the smallest surrounding element that contains
    # exactly one dollar price.
    card = None
    current = heading

    for _ in range(8):

        if not current.parent:
            break

        current = current.parent
        text = current.get_text(" ", strip=True)

        prices = re.findall(r"\$[\d,]+", text)

        if len(prices) == 1:
            card = current
            break

    if card is None:
        continue

    card_text = card.get_text(" ", strip=True)

    price_match = re.search(r"\$[\d,]+", card_text)

    if not price_match:
        continue

    price = int(
        price_match.group(0)
        .replace("$", "")
        .replace(",", "")
    )

    if price > MAX_PRICE:
        continue

    # The page itself is the For Sale page,
    # so an individual property found here is considered for sale.

    # Exclude commercial listings.
    if re.search(r"\bCommercial\b", card_text, re.IGNORECASE):
        continue

    properties.append({
        "title": title,
        "price": price,
        "url": href,
        "details": card_text
    })


# Remove duplicate URLs.
unique = {}

for property_item in properties:
    unique[property_item["url"]] = property_item

properties = list(unique.values())

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
