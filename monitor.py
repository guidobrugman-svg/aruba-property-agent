import requests

URL = "https://www.arubabrokers.com/property-status/for-sale/"

print("Aruba Property Agent starting...")
print("Checking Aruba Brokers...")

response = requests.get(
    URL,
    headers={"User-Agent": "Mozilla/5.0"},
    timeout=30,
)

response.raise_for_status()

print("Successfully connected to Aruba Brokers.")
print(f"Page size: {len(response.text)} characters")
print(response.text[:500])
