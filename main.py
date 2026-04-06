from flask import Flask, request
import requests
import os
from decouple import config
import time
from datetime import datetime

app = Flask(__name__)

# Читаем ключи
BOT_TOKEN = config('BOT_TOKEN')
CHAT_ID = config('CHAT_ID')
MONO_TOKEN = config('MONO_TOKEN')

# Базовый словарь категорий MCC (можно дополнять своими)
MCC_CATEGORIES = {
    5411: "🛒 Супермаркеты",
    5812: "🍽 Рестораны",
    5814: "🍔 Фастфуд и кофе",
    4131: "🚌 Автобусы/Транспорт",
    4121: "🚕 Такси",
    5912: "💊 Аптеки",
    8999: "🛠 Услуги",
    # Если кода нет в списке, будет написано "Другое"
}


def send_to_telegram(text, chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)


def get_monthly_stats():
    """Спрашивает у Монобанка выписку за этот месяц и считает итог"""
    # 1. Вычисляем начало текущего месяца
    now = datetime.now()
    first_day = datetime(now.year, now.month, 1)

    # Монобанк понимает время только в формате UNIX (секунды)
    from_time = int(first_day.timestamp())
    to_time = int(now.timestamp())

    # 2. Делаем запрос (0 - это счет по умолчанию, черная карта)
    url = f"https://api.monobank.ua/personal/statement/0/{from_time}/{to_time}"
    headers = {"X-Token": MONO_TOKEN}

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return "❌ Ошибка при получении данных от Монобанка. Возможно, сработал лимит запросов."

    transactions = response.json()

    # 3. Считаем траты
    total_spent = 0
    categories_sum = {}

    for item in transactions:
        amount = item.get('amount', 0)
        # Нас интересуют только траты (сумма меньше 0)
        if amount < 0:
            spent_uah = abs(amount) / 100
            total_spent += spent_uah

            # Определяем категорию
            mcc = item.get('mcc')
            category_name = MCC_CATEGORIES.get(mcc, f"📦 Другое (MCC {mcc})")

            # Добавляем в копилку категории
            categories_sum[category_name] = categories_sum.get(category_name, 0) + spent_uah

    # 4. Формируем красивое сообщение
    message = f"📊 <b>Статистика за текущий месяц:</b>\n"
    message += f"💸 <b>Всего потрачено:</b> {total_spent:.2f} грн\n\n"
    message += "<b>По категориям:</b>\n"

    # Сортируем категории по убыванию суммы
    sorted_cats = sorted(categories_sum.items(), key=lambda x: x[1], reverse=True)
    for cat, summ in sorted_cats:
        message += f"▪️ {cat}: {summ:.2f} грн\n"

    return message


# --- РОУТ ДЛЯ МОНОБАНКА (Остался без изменений) ---
@app.route('/mono-webhook', methods=['POST'])
def mono_webhook():
    data = request.json
    if data and data.get('type') == 'StatementItem':
        item = data['data']['statementItem']
        amount = item['amount']
        description = item['description']

        if amount < 0:
            spent_uah = abs(amount) / 100
            balance_uah = item['balance'] / 100

            message = (
                f"💸 <b>Новая трата:</b> {spent_uah} грн\n"
                f"📝 <b>Детали:</b> {description}\n"
                f"🏦 <b>Остаток:</b> {balance_uah} грн"
            )
            send_to_telegram(message)
    return "OK", 200


# --- НОВЫЙ РОУТ ДЛЯ ТЕЛЕГРАМА ---
# В качестве адреса используем токен, чтобы никто чужой не угадал ссылку
@app.route(f'/tg-{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.json

    # Проверяем, что пришло текстовое сообщение
    if "message" in data and "text" in data["message"]:
        text = data["message"]["text"]
        chat_id = data["message"]["chat"]["id"]

        # Если пользователь написал /stats
        if text == "/stats":
            # Сначала отправляем "Загружаю...", так как Монобанк может отвечать 1-2 секунды
            send_to_telegram("⏳ Считаю траты за месяц...", chat_id)

            # Собираем стату и отправляем
            stats_message = get_monthly_stats()
            send_to_telegram(stats_message, chat_id)

    return "OK", 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)