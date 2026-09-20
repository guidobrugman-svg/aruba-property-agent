import requests
from bs4 import BeautifulSoup

URL = "https://www.arubabrokers.com/property-status/for-sale/"

response = requests.get(
    URL,
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=30
)

response.raise_for_status()

soup = BeautifulSoup(response.text, "html.parser")

# Find the first real property heading.
for heading in soup.find_all("h2"):

    link = heading.find("a", href=True)

    if not link:
        continue

    if "/property/" not in link.get("href", ""):
        continue

    print("PROPERTY TITLE:")
    print(heading.get_text(" ", strip=True))
    print()

    current = heading

    for level in range(1, 7):

        if not current.parent:
            break

        current = current.parent

        print("=" * 60)
        print("PARENT LEVEL:", level)
        print("TAG:", current.name)
        print("CLASS:", current.get("class"))
        print("TEXT:")
        print(current.get_text(" ", strip=True)[:1500])
        print()

    break
