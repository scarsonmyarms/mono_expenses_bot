from flask import Flask, request
import requests
from decouple import config
import os

app = Flask(__name__)

# Читаем настройки из .env
BOT_TOKEN = config('BOT_TOKEN')
CHAT_ID = config('CHAT_ID')


def send_to_telegram(text):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    # Добавили parse_mode="HTML", чтобы работали жирные шрифты <b>
    payload = {"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)


@app.route('/mono-webhook', methods=['POST'])
def mono_webhook():
    data = request.json

    # Проверяем, что это реальная выписка о транзакции
    if data and data.get('type') == 'StatementItem':
        item = data['data']['statementItem']
        amount = item['amount']
        description = item['description']

        # Обрабатываем только траты (сумма меньше нуля)
        if amount < 0:
            spent_uah = abs(amount) / 100
            balance_uah = item['balance'] / 100

            message = (
                f"💸 <b>Новая трата:</b> {spent_uah} грн\n"
                f"📝 <b>Детали:</b> {description}\n"
                f"🏦 <b>Остаток:</b> {balance_uah} грн"
            )

            send_to_telegram(message)

    # Обязательно отвечаем Монобанку, что всё хорошо
    return "OK", 200


if __name__ == '__main__':
    # Сервер будет брать порт, который ему даст хостинг, или 5000 по умолчанию
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)