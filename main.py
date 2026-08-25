from flask import Flask, request
import requests
import os
from decouple import config
import time
from datetime import datetime, timezone, timedelta
import json
import threading
import traceback
import gspread

# Налаштування Київського часового поясу (з урахуванням літнього/зимового часу)
try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    # Запасний варіант для старих середовищ
    KYIV_TZ = timezone(timedelta(hours=3))

app = Flask(__name__)

# Читаємо ключі
BOT_TOKEN = config('BOT_TOKEN')
CHAT_ID = config('CHAT_ID')
MONO_TOKEN = config('MONO_TOKEN')
WHITE_CARD_ID = config('WHITE_CARD_ID')
PROCESSED_TX = set()
CASH_FILE = 'cash_data.json'

# --- ПІДКЛЮЧЕННЯ ДО GOOGLE ТАБЛИЦЬ ---
try:
    gc = gspread.service_account(filename='google_keys.json')
    sheet = gc.open("MonoExpenses").sheet1
    print("✅ Успішно підключилися до Google Таблиць!")
except Exception as e:
    print(f"❌ Помилка підключення до Google: {e}")
    sheet = None

# Читаємо датасет MCC
with open('mcc_codes.json', 'r', encoding='utf-8') as file:
    raw_data = json.load(file)
    MCC_DATASET = {}
    for k, v in raw_data.items():
        if isinstance(v, dict):
            category_name = v.get('uk', v.get('ru', 'Невідома категорія'))
        else:
            category_name = str(v)
        MCC_DATASET[int(k)] = category_name

CASH_CATEGORIES = {
    "продукти": "Продукти",
    "кафе": "Кафе. Ресторани",
    "ресторан": "Кафе. Ресторани",
    "таксі": "Таксі",
    "аптека": "Аптеки",
    "одяг": "Одяг",
    "розваги": "Розваги та спорт",
    "квіти": "Флористика"
}

def categorize_cash(description):
    """Шукає ключове слово в описі і повертає категорію"""
    desc_lower = description.lower()
    for key, category_name in CASH_CATEGORIES.items():
        if key in desc_lower:
            return category_name
    return "Інше"


def save_cash_transaction(amount, description):
    if sheet is None:
        raise Exception("Таблиця не підключена!")

    # Фіксуємо точний київський час
    now = datetime.now(KYIV_TZ)
    date_str = now.strftime("%Y-%m-%d %H:%M:%S")

    category = categorize_cash(description)

    # 5 колонок: Дата, Сума, Опис, Тип, Категорія
    sheet.append_row([date_str, float(amount), description, "Наличные", category])


def load_cash_transactions_for_month():
    if sheet is None:
        return []

    now = datetime.now(KYIV_TZ)
    current_month = now.strftime("%Y-%m")
    all_rows = sheet.get_all_values()
    cash_transactions = []

    for row in all_rows[1:]:
        is_card = len(row) >= 4 and row[3] == "Карта"

        if len(row) >= 2 and row[0].startswith(current_month) and not is_card:
            try:
                cash_transactions.append({
                    "amount": float(row[1]),
                    "description": row[2] if len(row) > 2 else "Без опису",
                    "category": row[4] if len(row) > 4 else "Інше"
                })
            except ValueError:
                pass

    return cash_transactions


def load_cash_transactions_for_today():
    """Зчитує витрати готівкою строго за СЬОГОДНІ"""
    if sheet is None:
        return []

    now = datetime.now(KYIV_TZ)
    current_day = now.strftime("%Y-%m-%d")

    all_rows = sheet.get_all_values()
    cash_transactions = []

    for row in all_rows[1:]:
        is_card = len(row) >= 4 and row[3] == "Карта"

        if len(row) >= 2 and row[0].startswith(current_day) and not is_card:
            try:
                cash_transactions.append({
                    "amount": float(row[1]),
                    "description": row[2] if len(row) > 2 else "Без опису",
                    "category": row[4] if len(row) > 4 else "Інше"
                })
            except ValueError:
                pass

    return cash_transactions


def send_to_telegram(text, chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    requests.post(url, json=payload)


def process_stats_background(chat_id):
    try:
        stats_message = get_monthly_stats()
        send_to_telegram(stats_message, chat_id)
    except Exception as e:
        error_msg = f"❌ <b>Помилка розрахунку:</b>\n{str(e)}"
        send_to_telegram(error_msg, chat_id)
        print(traceback.format_exc())


def get_monthly_stats():
    """Рахує місячну статистику з урахуванням категорій"""
    try:
        now = datetime.now(KYIV_TZ)
        first_day = datetime(now.year, now.month, 1, tzinfo=KYIV_TZ)
        from_time = int(first_day.timestamp())
        to_time = int(now.timestamp())

        # 1. Запит у Монобанк
        url = f"https://api.monobank.ua/personal/statement/{WHITE_CARD_ID}/{from_time}/{to_time}"
        headers = {"X-Token": MONO_TOKEN}
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            return f"❌ Помилка Монобанку: {response.status_code}. Ліміт: 1 запит на хвилину."

        transactions = response.json()
        if isinstance(transactions, dict) and "errorDescription" in transactions:
            return f"❌ Банк відповів: {transactions['errorDescription']}"

        total_spent = 0
        categories_sum = {}

        # 2. Обробка КАРТКИ
        for item in transactions:
            amount = item.get('amount', 0)
            if amount < 0:
                spent_uah = abs(amount) / 100
                total_spent += spent_uah
                mcc = item.get('mcc')
                category_name = MCC_DATASET.get(mcc, f"❓ MCC: {mcc}")
                categories_sum[category_name] = categories_sum.get(category_name, 0) + spent_uah

        # 3. Обробка ГОТІВКИ
        cash_transactions = load_cash_transactions_for_month()
        cash_total = 0
        for item in cash_transactions:
            amount = item['amount']
            cat_name = item['category']

            cash_total += amount
            total_spent += amount

            display_name = f"💵 {cat_name} (Готівка)"
            categories_sum[display_name] = categories_sum.get(display_name, 0) + amount

        # 4. Формування тексту
        if total_spent == 0:
            return "🤷‍♂️ У цьому місяці витрат поки не зафіксовано."

        message = f"📊 <b>Статистика за місяць:</b>\n"
        message += f"💳 Картка: {total_spent - cash_total:.2f} грн\n"
        message += f"💵 Готівка: {cash_total:.2f} грн\n"
        message += f"💰 <b>РАЗОМ:</b> {total_spent:.2f} грн\n\n"
        message += "<b>Деталізація:</b>\n"

        sorted_cats = sorted(categories_sum.items(), key=lambda x: x[1], reverse=True)
        for cat, summ in sorted_cats:
            message += f"▪️ {cat}: {summ:.2f} грн\n"

        return message

    except Exception as e:
        print(f"Критична помилка в статистиці: {e}")
        return f"❌ Сталася помилка при розрахунку: {str(e)}"


def get_daily_stats():
    """Рахує статистику строго за СЬОГОДНІ"""
    try:
        now = datetime.now(KYIV_TZ)
        start_of_day = datetime(now.year, now.month, now.day, tzinfo=KYIV_TZ)

        from_time = int(start_of_day.timestamp())
        to_time = int(now.timestamp())

        # 1. Запит у Монобанк
        url = f"https://api.monobank.ua/personal/statement/{WHITE_CARD_ID}/{from_time}/{to_time}"
        headers = {"X-Token": MONO_TOKEN}
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            return f"❌ Помилка Монобанку: {response.status_code}"

        transactions = response.json()
        if isinstance(transactions, dict) and "errorDescription" in transactions:
            return f"❌ Банк відповів: {transactions['errorDescription']}"

        total_spent = 0
        categories_sum = {}

        # 2. Обробка КАРТКИ
        for item in transactions:
            amount = item.get('amount', 0)
            if amount < 0:
                spent_uah = abs(amount) / 100
                total_spent += spent_uah
                mcc = item.get('mcc')
                category_name = MCC_DATASET.get(mcc, f"❓ MCC: {mcc}")
                categories_sum[category_name] = categories_sum.get(category_name, 0) + spent_uah

        # 3. Обробка ГОТІВКИ (за сьогодні)
        cash_transactions = load_cash_transactions_for_today()
        cash_total = 0
        for item in cash_transactions:
            amount = item['amount']
            cat_name = item['category']

            cash_total += amount
            total_spent += amount

            display_name = f"💵 {cat_name} (Готівка)"
            categories_sum[display_name] = categories_sum.get(display_name, 0) + amount

        # 4. Формування тексту
        if total_spent == 0:
            return "🌙 <b>Підсумки дня:</b>\nСьогодні не було витрат! Ідеальний день для бюджету 💰"

        message = f"🌙 <b>Підсумки дня:</b>\n"
        message += f"💳 Картка: {total_spent - cash_total:.2f} грн\n"
        message += f"💵 Готівка: {cash_total:.2f} грн\n"
        message += f"💰 <b>ВСЬОГО ЗА СЬОГОДНІ:</b> {total_spent:.2f} грн\n\n"

        sorted_cats = sorted(categories_sum.items(), key=lambda x: x[1], reverse=True)
        for cat, summ in sorted_cats:
            message += f"▪️ {cat}: {summ:.2f} грн\n"

        return message

    except Exception as e:
        return f"❌ Помилка в денному звіті: {str(e)}"


# --- ОБРОБКА ТРАНЗАКЦІЇ МОНОБАНКУ ---
def process_mono_background(data):
    try:
        item = data['data']['statementItem']
        tx_id = item.get('id')

        if tx_id in PROCESSED_TX:
            return

        PROCESSED_TX.add(tx_id)
        if len(PROCESSED_TX) > 1000:
            PROCESSED_TX.clear()

        amount = item.get('amount', 0)

        # Якщо сума від'ємна — це витрата
        if amount < 0:
            spent_uah = abs(amount) / 100
            balance_uah = item.get('balance', 0) / 100
            description = item.get('description', 'Невідомо')
            mcc = item.get('mcc')
            category_name = MCC_DATASET.get(mcc, f"❓ MCC: {mcc}")

            # 1. Беремо точний час транзакції з Монобанку і конвертуємо в Київський час
            tx_time = item.get('time')
            if tx_time:
                date_str = datetime.fromtimestamp(tx_time, tz=KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S")
            else:
                date_str = datetime.now(KYIV_TZ).strftime("%Y-%m-%d %H:%M:%S")

            # 2. Записуємо в таблицю всі 5 параметрів (включно з категорією)
            if sheet is not None:
                sheet.append_row([date_str, spent_uah, description, "Карта", category_name])

            # 3. Надсилаємо повідомлення
            message = (
                f"💸 <b>Нова витрата:</b> {spent_uah:.2f} грн\n"
                f"🏷 <b>Категорія:</b> {category_name}\n"
                f"📝 <b>Деталі:</b> {description}\n"
                f"🏦 <b>Залишок:</b> {balance_uah:.2f} грн"
            )
            send_to_telegram(message)

    except Exception as e:
        print(f"Помилка при обробці транзакції Монобанку: {e}")


# --- ДЕННИЙ ЗВІТ ---
@app.route('/trigger-daily-report', methods=['GET'])
def trigger_daily_report():
    ADMIN_CHAT_ID = "912719804"

    def send_report():
        msg = get_daily_stats()
        send_to_telegram(msg, ADMIN_CHAT_ID)

    thread = threading.Thread(target=send_report)
    thread.start()

    return "Звіт запущено!", 200


# --- МІСЯЧНИЙ ЗВІТ ---
@app.route('/trigger-monthly-report', methods=['GET'])
def trigger_monthly_report():
    ADMIN_CHAT_ID = "912719804"

    def send_report():
        msg = get_monthly_stats()
        header = "🏆 <b>ФІНАЛЬНИЙ ЗВІТ ЗА МІСЯЦЬ!</b> 🏆\n\n"
        send_to_telegram(header + msg, ADMIN_CHAT_ID)

    thread = threading.Thread(target=send_report)
    thread.start()

    return "Місячний звіт запущено!", 200


# --- ВЕБХУК ДЛЯ МОНОБАНКУ (GET + POST) ---
@app.route('/mono-webhook', methods=['GET', 'POST'])
def mono_webhook():
    if request.method == 'GET':
        return "OK", 200

    data = request.json
    if data and data.get('type') == 'StatementItem':
        thread = threading.Thread(target=process_mono_background, args=(data,))
        thread.start()

    return "OK", 200


# --- ВЕБХУК ДЛЯ ТЕЛЕГРАМУ ---
@app.route(f'/tg-{BOT_TOKEN}', methods=['POST'])
def telegram_webhook():
    data = request.json

    if "message" in data and "text" in data["message"]:
        text = data["message"]["text"]
        chat_id = data["message"]["chat"]["id"]

        if text == "/stats":
            thread = threading.Thread(target=process_stats_background, args=(chat_id,))
            thread.start()

        elif text.startswith("/cash"):
            try:
                parts = text.split(maxsplit=2)
                amount = parts[1]
                description = parts[2] if len(parts) > 2 else "Без опису"

                save_cash_transaction(amount, description)
                category = categorize_cash(description)
                send_to_telegram(f"✅ Записано: {amount} грн ({category}) на '{description}'", chat_id)
            except Exception as e:
                send_to_telegram("❌ Помилка! Пиши так: <code>/cash 100 продукти</code>", chat_id)
                print(f"Помилка збереження готівки: {e}")

    return "OK", 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)