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

print("Aruba Property Agent starting...")
print("Checking Aruba Brokers...")
print("Status code:", response.status_code)
print("Page title:", soup.title.get_text(strip=True) if soup.title else "NO TITLE")

articles = soup.find_all("article")

print("ARTICLE COUNT:", len(articles))

for article in articles[:5]:
    print()
    print("ARTICLE CLASS:", article.get("class"))
    print("ARTICLE TEXT:", article.get_text(" ", strip=True)[:500])

print()
print("Diagnostic test completed successfully.")
