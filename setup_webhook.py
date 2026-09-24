import requests
from decouple import config

MONO_TOKEN = config("MONO_TOKEN")
MONO_WEBHOOK_SECRET = config("MONO_WEBHOOK_SECRET")
WEBHOOK_URL = f"https://mono-expenses-bot.onrender.com/mono-webhook/{MONO_WEBHOOK_SECRET}"

print("Надсилаємо запит у Монобанк...")
response = requests.post(
    "https://api.monobank.ua/personal/webhook",
    headers={"X-Token": MONO_TOKEN},
    json={"webHookUrl": WEBHOOK_URL},
    timeout=30,
)
print(response.status_code, response.text)