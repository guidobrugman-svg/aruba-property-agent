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

print("H2 HEADINGS FOUND:", len(soup.find_all("h2")))
print()

for number, heading in enumerate(soup.find_all("h2"), start=1):
    print(f"{number}: {heading.get_text(' ', strip=True)}")
    print("   LINK:", heading.find("a", href=True))
    print()
