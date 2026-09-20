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

print("Status code:", response.status_code)
print("Response length:", len(response.text))

response.raise_for_status()

soup = BeautifulSoup(response.text, "html.parser")

print("Page title:", soup.title.get_text(" ", strip=True) if soup.title else "NO TITLE")
print("H1 COUNT:", len(soup.find_all("h1")))
print("H2 COUNT:", len(soup.find_all("h2")))
print("H3 COUNT:", len(soup.find_all("h3")))
print("ARTICLE COUNT:", len(soup.find_all("article")))
print("PROPERTY LINKS:", len(soup.find_all("a", href=lambda x: x and "/property/" in x)))

print()
print("Diagnostic test completed successfully.")
