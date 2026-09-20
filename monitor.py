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

    # Extract the price from this individual listing.
    price_match = re.search(r"\$\s*([\d,]+)", text)

    if not price_match:
        continue

    price = int(
        price_match.group(1).replace(",", "")
    )

    # Exclude properties above our maximum price.
    if price > MAX_PRICE:
        continue

    # Exclude commercial properties.
    if re.search(r"\bCommercial\b", text, re.IGNORECASE):
        continue

    properties.append({
        "title": title,
        "price": price,
        "url": url,
        "details": text
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
