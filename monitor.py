import requests
from bs4 import BeautifulSoup

URL = "https://arubalistings.com/sale/all"
MAX_PRICE = 650000

print("Aruba Property Agent starting...")
print("Checking Aruba Listings...")

response = requests.get(
    URL,
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=30,
)

response.raise_for_status()

soup = BeautifulSoup(response.text, "html.parser")

text = soup.get_text(" ", strip=True)

print("Successfully connected to Aruba Listings.")
print(f"Page size: {len(response.text)} characters")
print("Sale page contains:", text[:500])
