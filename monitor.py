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

# Find all links that point to individual property pages.
for link in soup.find_all("a", href=True):

    href = urljoin(URL, link["href"])

    # Individual Aruba Brokers property pages use /property/
    if "/property/" not in href:
        continue

    # Find a nearby container containing the listing information.
    container = link

    for _ in range(8):
        if container.parent:
            container = container.parent

        text = container.get_text(" ", strip=True)

        if "$" in text and "For Sale" in text:
            break

    text = container.get_text(" ", strip=True)

    # Extract price.
    prices = re.findall(r"\$[\d,]+", text)

    if not prices:
        continue

    price = int(
        prices[0]
        .replace("$", "")
        .replace(",", "")
    )

    # Price limit.
    if price > MAX_PRICE:
        continue

    # Must be for sale.
    if not re.search(r"\bFor Sale\b", text, re.IGNORECASE):
        continue

    # Exclude commercial buildings/properties.
    if re.search(r"\bCommercial\b", text, re.IGNORECASE):
        continue

    # Find a title.
    title = ""

    for heading in container.find_all(["h2", "h3", "h4"]):
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
        "details": text
    })


# Remove duplicate property URLs.
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
