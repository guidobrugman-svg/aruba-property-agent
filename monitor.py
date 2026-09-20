import requests
from bs4 import BeautifulSoup

URL = "https://www.arubabrokers.com/property-status/for-sale/"

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

articles = soup.find_all("article")

print()
print("ARTICLE COUNT:", len(articles))
print()

for number, article in enumerate(articles, start=1):

    print("=" * 60)
    print("ARTICLE", number)

    heading = article.find("h2")

    if heading:
        print("H2 FOUND:", heading.get_text(" ", strip=True))
    else:
        print("H2 FOUND: NO")

    if heading:
        link = heading.find("a", href=True)

        if link:
            print("LINK FOUND:", link["href"])
        else:
            print("LINK FOUND: NO")
    else:
        print("LINK FOUND: NO")

    text = article.get_text(" ", strip=True)

    print("TEXT:", text[:500])

print()
print("Diagnostic test completed successfully.")
