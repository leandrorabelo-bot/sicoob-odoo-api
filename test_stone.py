import requests

STONE_KEY = "sk_a262ff8d1efb46aaaef31cd439957a7e"

headers = {
    "Authorization": STONE_KEY,
    "Content-Type": "application/json"
}

url = "https://conciliation.stone.com.br/v1/merchant"

response = requests.get(url, headers=headers)

print(response.status_code)
print(response.text)
