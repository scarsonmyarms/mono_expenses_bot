from flask import Flask, request
import requests
import os
from decouple import config
import time
from datetime import datetime
import json
import threading
import traceback

app = Flask(__name__)

# Читаем ключи
BOT_TOKEN = config('BOT_TOKEN')
CHAT_ID = config('CHAT_ID')
MONO_TOKEN = config('MONO_TOKEN')
WHITE_CARD_ID = config('WHITE_CARD_ID')
PROCESSED_TX = set()

# Читаем датасет и сразу выбираем нужный язык
with open('mcc_codes.json', 'r', encoding='utf-8') as file:
    raw_data = json.load(file)
    MCC_DATASET = {}
    for k, v in raw_data.items():
        # Если значение — это словарь с языками, берем украинский ('uk').
        # Если русского почему-то нет, берем русский ('uk').
        if isinstance(v, dict):
            category_name = v.get('uk', v.get('ru', 'Неизвестная категория'))
        else:
            # Защита: если там просто текст, оставляем как есть
            category_name = str(v)

        MCC_DATASET[int(k)] = category_name

def send_to_telegram(text, chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)


def process_stats_background(chat_id):
    try:
        # Пытаемся посчитать стату
        stats_message = get_monthly_stats()
        send_to_telegram(stats_message, chat_id)

    except Exception as e:
        # Если код сломался (любая ошибка), бот не зависнет, а напишет причину!
        error_msg = f"❌ <b>Ой, код сломался!</b>\nПричина: {str(e)}"
        send_to_telegram(error_msg, chat_id)

        # Печатаем полную ошибку в логи Render, чтобы мы могли её изучить
        print(traceback.format_exc())

def get_monthly_stats():
    """Считает стату за месяц, используя загруженный датасет MCC кодов"""
    now = datetime.now()
    first_day = datetime(now.year, now.month, 1)

    from_time = int(first_day.timestamp())
    to_time = int(now.timestamp())

    url = f"https://api.monobank.ua/personal/statement/{WHITE_CARD_ID}/{from_time}/{to_time}"
    headers = {"X-Token": MONO_TOKEN}

    response = requests.get(url, headers=headers)

    if response.status_code != 200:
        return "❌ Ошибка при получении данных от Монобанка."

    transactions = response.json()

    total_spent = 0
    categories_sum = {}

    for item in transactions:
        amount = item.get('amount', 0)

        if amount < 0:
            spent_uah = abs(amount) / 100
            total_spent += spent_uah

            # Достаем MCC код транзакции
            mcc = item.get('mcc')

            # Ищем этот код в нашем датасете.
            # Если такого кода в файле нет, пишем "Другое" и выводим сам код,
            # чтобы ты мог потом добавить его в свой mcc_codes.json!
            category_name = MCC_DATASET.get(mcc, f"❓ Неизвестный MCC: {mcc}")

            categories_sum[category_name] = categories_sum.get(category_name, 0) + spent_uah

    # Формируем сообщение
    message = f"📊 <b>Статистика за месяц (по MCC):</b>\n"
    message += f"💸 <b>Всего потрачено:</b> {total_spent:.2f} грн\n\n"

    sorted_cats = sorted(categories_sum.items(), key=lambda x: x[1], reverse=True)

    for cat, summ in sorted_cats:
        message += f"▪️ {cat}: {summ:.2f} грн\n"

    return message


# --- РОУТ ДЛЯ МОНОБАНКА (Остался без изменений) ---
def process_mono_background(data):
    try:
        item = data['data']['statementItem']
        tx_id = item.get('id')  # У каждой транзакции есть свой уникальный ID

        # 1. Защита от дублей: проверяем, видели ли мы этот ID
        if tx_id in PROCESSED_TX:
            return  # Если видели — молча выходим, ничего не отправляем

        # Записываем ID в наш "блокнот"
        PROCESSED_TX.add(tx_id)

        # Защита от переполнения памяти: если накопилось больше 1000 ID, очищаем блокнот
        if len(PROCESSED_TX) > 1000:
            PROCESSED_TX.clear()

        amount = item.get('amount', 0)

        if amount < 0:
            spent_uah = abs(amount) / 100
            balance_uah = item.get('balance', 0) / 100
            description = item.get('description', 'Неизвестно')

            message = (
                f"💸 <b>Новая трата:</b> {spent_uah:.2f} грн\n"
                f"📝 <b>Детали:</b> {description}\n"
                f"🏦 <b>Остаток:</b> {balance_uah:.2f} грн"
            )
            send_to_telegram(message)

    except Exception as e:
        print(f"Ошибка при обработке транзакции: {e}")


@app.route('/mono-webhook', methods=['POST'])
def mono_webhook():
    data = request.json

    if data and data.get('type') == 'StatementItem':
        # Моментально отдаем задачу в фон
        thread = threading.Thread(target=process_mono_background, args=(data,))
        thread.start()

    # Сразу отвечаем банку, чтобы он не слал повторные запросы
    return "OK", 200

# --- РОУТ ДЛЯ TG (Остался без изменений) ---
@app.route(f'/tg-{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.json

    if "message" in data and "text" in data["message"]:
        text = data["message"]["text"]
        chat_id = data["message"]["chat"]["id"]

        if text == "/stats":
            # Запускаем подсчет статистики в отдельном фоновом потоке!
            thread = threading.Thread(target=process_stats_background, args=(chat_id,))
            thread.start()

    # Сервер моментально отвечает Телеграму "ОК", не дожидаясь окончания подсчетов
    return "OK", 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)