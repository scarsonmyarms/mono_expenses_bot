import requests
from decouple import config

# Вставь свой токен сюда
MONO_TOKEN = config("MONO_TOKEN")

url = "https://api.monobank.ua/personal/client-info"
response = requests.get(url, headers={"X-Token": MONO_TOKEN})
data = response.json()

print("Твои карты:")
for acc in data.get("accounts", []):
    # Код валюты 980 - это гривна, 840 - доллар, 978 - евро
    currency = "Гривна" if acc['currencyCode'] == 980 else acc['currencyCode']
    balance = acc['balance'] / 100
    card_type = acc.get('type', 'неизвестно')

    print(f"💳 Тип: {card_type.upper()} | Баланс: {balance} | Валюта: {currency}")
    print(f"🔑 ID: {acc['id']}\n" + "-" * 40)