import requests
from decouple import config

BOT_TOKEN = config("BOT_TOKEN")
# Твоя ссылка на Render! Обрати внимание на конец ссылки: /tg-ТВОЙ_ТОКЕН_БОТА
RENDER_URL = f"https://mono-expenses-bot.onrender.com/tg-{BOT_TOKEN}"

url = f"https://api.telegram.org/bot{BOT_TOKEN}/setWebhook"
response = requests.post(url, json={"url": RENDER_URL})

print(response.text)