import requests
from decouple import config

BOT_TOKEN = config("BOT_TOKEN")
TG_SECRET = config("TG_SECRET")
WEBHOOK_URL = "https://mono-expenses-bot.onrender.com/tg-webhook"

response = requests.post(
    f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook",
    json={"url": WEBHOOK_URL, "secret_token": TG_SECRET},
    timeout=15,
)
print(response.text)