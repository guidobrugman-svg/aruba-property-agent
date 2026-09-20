import requests

URL = "https://www.arubabrokers.com/property-status/for-sale/"

HEADERS = {
    "User-Agent": "Mozilla/5.0"
}

print("Aruba Property Agent starting...")
print("Checking Aruba Brokers...")

response = requests.get(URL, headers=HEADERS, timeout=30)

print("Status code:", response.status_code)
print("Page length:", len(response.text))

print()
print("FIRST 3000 CHARACTERS OF PAGE:")
print("=" * 60)
print(response.text[:3000])
print("=" * 60)

print()
print("Test completed.")
