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

# The Aruba Brokers page uses h2 headings for property titles.
for heading in soup.find_all("h2"):

    link = heading.find("a", href=True)

    if not link:
        continue

    title = heading.get_text(" ", strip=True)
    href = urljoin(URL, link["href"])

    # Walk upward until we find the listing block.
    container = heading

    for _ in range(8):
        if container.parent:
            container = container.parent

        text = container.get_text(" ", strip=True)

        if "$" in text and "For Sale" in text:
            break

    text = container.get_text(" ", strip=True)

    # Must be an active For Sale listing.
    if not re.search(r"\bFor Sale\b", text, re.IGNORECASE):
        continue

    # Find the price.
    price_match = re.search(r"\$[\d,]+", text)

    if not price_match:
        continue

    price = int(
        price_match.group(0)
        .replace("$", "")
        .replace(",", "")
    )

    # Maximum price.
    if price > MAX_PRICE:
        continue

    # Exclude commercial properties.
    if re.search(r"\bCommercial\b", text, re.IGNORECASE):
        continue

    properties.append({
        "title": title,
        "price": price,
        "url": href,
        "details": text
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
