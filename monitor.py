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

# Each real listing has an H2 containing the property title.
for heading in soup.find_all("h2"):

    link = heading.find("a", href=True)

    if not link:
        continue

    title = heading.get_text(" ", strip=True)
    href = urljoin(URL, link["href"])

    # Ignore non-property headings.
    if "/property/" not in href:
        continue

    # The listing information is contained in the heading's
    # surrounding article/card.
    card = heading

    for _ in range(6):
        if card.parent:
            card = card.parent

        card_text = card.get_text(" ", strip=True)

        # Stop once we have found the price belonging to this listing.
        if re.search(r"\$[\d,]+", card_text):
            break

    card_text = card.get_text(" ", strip=True)

    # Extract price.
    price_match = re.search(r"\$[\d,]+", card_text)

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

    # Must be an active For Sale listing.
    if not re.search(r"\bFor Sale\b", card_text, re.IGNORECASE):
        continue

    # Exclude commercial properties.
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
