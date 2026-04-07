import requests
from decouple import config

# Вставьте ваш новый токен в кавычки
MONO_TOKEN = config("MONO_TOKEN")

# Ссылка на ваш запущенный бот на Render
WEBHOOK_URL = "https://mono-expenses-bot.onrender.com/mono-webhook"

print("Отправляем запрос в Монобанк...")

response = requests.post(
    "https://api.monobank.ua/personal/webhook",
    headers={"X-Token": MONO_TOKEN},
    json={"webHookUrl": WEBHOOK_URL}  # Python сам правильно расставит все кавычки!
)

# Смотрим ответ от банка
print("Ответ от сервера:", response.text)