from flask import Flask, request
import requests
import os
from decouple import config
import time
from datetime import datetime
import json
import threading
import traceback
import gspread

app = Flask(__name__)

# Читаем ключи
BOT_TOKEN = config('BOT_TOKEN')
CHAT_ID = config('CHAT_ID')
MONO_TOKEN = config('MONO_TOKEN')
WHITE_CARD_ID = config('WHITE_CARD_ID')
PROCESSED_TX = set()
CASH_FILE = 'cash_data.json'

# --- ПОДКЛЮЧЕНИЕ К GOOGLE ---
try:
    # Робот читает свой файл-пропуск
    gc = gspread.service_account(filename='google_keys.json')
    # Открываем таблицу по имени (ИМЯ ДОЛЖНО СОВПАДАТЬ С ТЕМ, ЧТО В ГУГЛЕ!)
    sheet = gc.open("MonoExpenses").sheet1
    print("✅ Успешно подключились к Google Таблицам!")
except Exception as e:
    print(f"❌ Ошибка подключения к Google: {e}")
    sheet = None

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


def save_cash_transaction(amount, description):
    """Сохраняет трату в Google Таблицу (Готівка)"""
    if sheet is None:
        raise Exception("Таблица не подключена!")

    now = datetime.now()
    date_str = now.strftime("%Y-%m-%d %H:%M:%S")

    # Додаємо 4-й параметр - "Наличные"
    sheet.append_row([date_str, float(amount), description, "Наличные"])


def load_cash_transactions_for_month():
    if sheet is None:
        return []
    """Считывает траты за этот месяц из Google Таблицы"""
    now = datetime.now()
    current_month = now.strftime("%Y-%m")

    all_rows = sheet.get_all_values()
    cash_transactions = []

    for row in all_rows[1:]:
        # ПЕРЕВІРКА: чи є в рядку слово "Карта" (у 4-й колонці)
        is_card = len(row) >= 4 and row[3] == "Карта"

        # Якщо це поточний місяць І ЦЕ НЕ КАРТА
        if len(row) >= 2 and row[0].startswith(current_month) and not is_card:
            try:
                cash_transactions.append({
                    "amount": float(row[1]),
                    "description": row[2] if len(row) > 2 else "Без описания"
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
    """Считает общую статистику: Карта (из Моно) + Наличка (из файла)"""
    try:
        now = datetime.now()
        first_day = datetime(now.year, now.month, 1)
        from_time = int(first_day.timestamp())
        to_time = int(now.timestamp())

        # 1. Запрос в Монобанк
        url = f"https://api.monobank.ua/personal/statement/{WHITE_CARD_ID}/{from_time}/{to_time}"
        headers = {"X-Token": MONO_TOKEN}
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            return f"❌ Ошибка Монобанка: {response.status_code}. Возможно, лимит запросов (1 раз в минуту)."

        transactions = response.json()

        # Проверка: если Моно прислал ошибку в формате JSON
        if isinstance(transactions, dict) and "errorDescription" in transactions:
            return f"❌ Банк ответил: {transactions['errorDescription']}"

        total_spent = 0
        categories_sum = {}

        # 2. Обработка КАРТЫ
        for item in transactions:
            amount = item.get('amount', 0)
            if amount < 0:
                spent_uah = abs(amount) / 100
                total_spent += spent_uah
                mcc = item.get('mcc')
                # Берем название из нашего датасета (уже с учетом ru/uk)
                category_name = MCC_DATASET.get(mcc, f"❓ MCC: {mcc}")
                categories_sum[category_name] = categories_sum.get(category_name, 0) + spent_uah

        # 3. Обработка НАЛИЧКИ
        cash_transactions = load_cash_transactions_for_month()
        cash_total = 0
        for item in cash_transactions:
            amount = item['amount']
            cash_total += amount
            total_spent += amount

            cat_name = "💵 Наличные"
            categories_sum[cat_name] = categories_sum.get(cat_name, 0) + amount

        # 4. ФОРМИРОВАНИЕ ТЕКСТА (Самое важное!)
        if total_spent == 0:
            return "🤷‍♂️ В этом месяце трат пока не зафиксировано."

        message = f"📊 <b>Статистика за месяц:</b>\n"
        message += f"💳 Карта: {total_spent - cash_total:.2f} грн\n"
        message += f"💵 Наличка: {cash_total:.2f} грн\n"
        message += f"💰 <b>ИТОГО:</b> {total_spent:.2f} грн\n\n"
        message += "<b>Детализация:</b>\n"

        # Сортируем категории по сумме
        sorted_cats = sorted(categories_sum.items(), key=lambda x: x[1], reverse=True)
        for cat, summ in sorted_cats:
            message += f"▪️ {cat}: {summ:.2f} грн\n"

        # ОБЯЗАТЕЛЬНО возвращаем результат!
        return message

    except Exception as e:
        # Если что-то пошло не так внутри функции
        print(f"Критическая ошибка в статистике: {e}")
        return f"❌ Произошла ошибка при расчете: {str(e)}"


# --- Дневная статистика ---
def load_cash_transactions_for_today():
    """Считывает траты за СЕГОДНЯ из Google Таблицы"""
    if sheet is None:
        return []

    now = datetime.now()
    current_month = now.strftime("%Y-%m")

    all_rows = sheet.get_all_values()
    cash_transactions = []

    for row in all_rows[1:]:
        # ПЕРЕВІРКА: чи є в рядку слово "Карта" (у 4-й колонці)
        is_card = len(row) >= 4 and row[3] == "Карта"

        # Якщо це поточний місяць І ЦЕ НЕ КАРТА
        if len(row) >= 2 and row[0].startswith(current_month) and not is_card:
            try:
                cash_transactions.append({
                    "amount": float(row[1]),
                    "description": row[2] if len(row) > 2 else "Без описания"
                })
            except ValueError:
                pass

    return cash_transactions


def get_daily_stats():
    """Считает общую статистику строго за СЕГОДНЯ"""
    try:
        now = datetime.now()
        # Начало сегодняшнего дня (00:00:00)
        start_of_day = datetime(now.year, now.month, now.day)

        from_time = int(start_of_day.timestamp())
        to_time = int(now.timestamp())

        # 1. Запрос в Монобанк
        url = f"https://api.monobank.ua/personal/statement/{WHITE_CARD_ID}/{from_time}/{to_time}"
        headers = {"X-Token": MONO_TOKEN}
        response = requests.get(url, headers=headers)

        if response.status_code != 200:
            return f"❌ Ошибка Монобанка: {response.status_code}"

        transactions = response.json()
        if isinstance(transactions, dict) and "errorDescription" in transactions:
            return f"❌ Банк ответил: {transactions['errorDescription']}"

        total_spent = 0
        categories_sum = {}

        # 2. Обработка КАРТЫ
        for item in transactions:
            amount = item.get('amount', 0)
            if amount < 0:
                spent_uah = abs(amount) / 100
                total_spent += spent_uah
                mcc = item.get('mcc')
                category_name = MCC_DATASET.get(mcc, f"❓ MCC: {mcc}")
                categories_sum[category_name] = categories_sum.get(category_name, 0) + spent_uah

        # 3. Обработка НАЛИЧКИ
        cash_transactions = load_cash_transactions_for_today()
        cash_total = 0
        for item in cash_transactions:
            amount = item['amount']
            cash_total += amount
            total_spent += amount
            categories_sum["💵 Наличные"] = categories_sum.get("💵 Наличные", 0) + amount

        # 4. ФОРМИРОВАНИЕ ТЕКСТА
        if total_spent == 0:
            return "🌙 <b>Итоги дня:</b>\nСегодня не было трат! Идеальный день для бюджета 💰"

        message = f"🌙 <b>Итоги дня:</b>\n"
        message += f"💳 Карта: {total_spent - cash_total:.2f} грн\n"
        message += f"💵 Наличка: {cash_total:.2f} грн\n"
        message += f"💰 <b>ВСЕГО ЗА СЕГОДНЯ:</b> {total_spent:.2f} грн\n\n"

        sorted_cats = sorted(categories_sum.items(), key=lambda x: x[1], reverse=True)
        for cat, summ in sorted_cats:
            message += f"▪️ {cat}: {summ:.2f} грн\n"

        return message

    except Exception as e:
        return f"❌ Ошибка в дневном отчете: {str(e)}"


# --- РОУТ ДЛЯ МОНОБАНКА (Остался без изменений) ---
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
            description = item.get('description', 'Неизвестно')

            # --- НОВЕ: ЗАПИСУЄМО КАРТКОВУ ВИТРАТУ В ГУГЛ ТАБЛИЦЮ ---
            if sheet is not None:
                now = datetime.now()
                date_str = now.strftime("%Y-%m-%d %H:%M:%S")
                # Додаємо 4-й параметр - "Карта"
                sheet.append_row([date_str, spent_uah, description, "Карта"])
            # -------------------------------------------------------

            message = (
                f"💸 <b>Новая трата:</b> {spent_uah:.2f} грн\n"
                f"📝 <b>Детали:</b> {description}\n"
                f"🏦 <b>Остаток:</b> {balance_uah:.2f} грн"
            )
            send_to_telegram(message)

    except Exception as e:
        print(f"Ошибка при обработке транзакции: {e}")


# --- ДЕНИЙ ЗВІТ ---
@app.route('/trigger-daily-report', methods=['GET'])
def trigger_daily_report():
    # ВАЖНО: Впиши сюда свой личный ID чата Телеграм (только цифры)
    # Если не знаешь его, перешли любое свое сообщение боту @userinfobot
    ADMIN_CHAT_ID = "912719804"

    # Запускаем сбор отчета в фоне, чтобы сервер быстро ответил будильнику
    def send_report():
        msg = get_daily_stats()
        send_to_telegram(msg, ADMIN_CHAT_ID)

    thread = threading.Thread(target=send_report)
    thread.start()

    return "Отчет запущен!", 200

# --- МІСЯЧНИЙ ЗВІТ ---
@app.route('/trigger-monthly-report', methods=['GET'])
def trigger_monthly_report():
    # ВАЖЛИВО: Сюди встав той самий ID, що і в денному звіті
    ADMIN_CHAT_ID = "912719804"

    def send_report():
        # Беремо нашу готову функцію, яка рахує витрати за місяць
        msg = get_monthly_stats()

        # Додаємо святковий заголовок
        header = "🏆 <b>ФІНАЛЬНИЙ ЗВІТ ЗА МІСЯЦЬ!</b> 🏆\n\n"

        send_to_telegram(header + msg, ADMIN_CHAT_ID)

    thread = threading.Thread(target=send_report)
    thread.start()

    return "Місячний звіт запущено!", 200


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

        # Проверяем первую команду
        if text == "/stats":
            # Запускаем подсчет статистики в отдельном фоновом потоке!
            thread = threading.Thread(target=process_stats_background, args=(chat_id,))
            thread.start()

        # Проверяем вторую команду (Обрати внимание, отступ такой же, как у /stats)
        elif text.startswith("/cash"):
            try:
                # Разделяем строку: /cash 100 продукты -> ['/cash', '100', 'продукты']
                parts = text.split(maxsplit=2)
                amount = parts[1]
                description = parts[2] if len(parts) > 2 else "Без описания"

                save_cash_transaction(amount, description)
                send_to_telegram(f"✅ Записал: {amount} грн на '{description}'", chat_id)
            except Exception as e:
                send_to_telegram("❌ Ошибка! Пиши так: <code>/cash 100 продукты</code>", chat_id)
                print(f"Ошибка сохранения налички: {e}")

    # Не забываем всегда отдавать OK Телеграму в самом конце
    return "OK", 200


if __name__ == '__main__':
    port = int(os.environ.get("PORT", 5000))
    app.run(host='0.0.0.0', port=port)