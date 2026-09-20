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

    price_match = re.search(r"\$[\d,]+", text)

    if not price_match:
        continue

    price = int(
        price_match.group(0)
        .replace("$", "")
        .replace(",", "")
    )

    # Ignore properties above our maximum price.
    if price > MAX_PRICE:
        continue

    # Ignore commercial properties.
    if "Commercial" in text:
        continue

    properties.append({
        "title": title,
        "price": price,
        "url": url,
        "details": text
    })

print()
print("=" * 60)
print(f"QUALIFYING PROPERTIES FOUND: {len(properties)}")
print("=" * 60)

for number, property_item in enumerate(properties, start=1):
    print()
    print(f"{number}. {property_item['title']}")
    print(f"   Price: ${property_item['price']:,}")
    print(f"   URL: {property_item['url']}")

print()
print("Monitor test completed successfully.")
