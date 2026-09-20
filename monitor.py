import json
import os
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urljoin

URL = "https://www.arubabrokers.com/property-status/for-sale/"
MAX_PRICE = 650000
STATE_FILE = "state.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

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


print()
print("Monitor test completed successfully.")
