import hmac
import html
import json
import math
import os
import re
import threading
import time
import traceback
from collections import OrderedDict
from datetime import datetime, timedelta, timezone

import gspread
import requests
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
from decouple import config
from flask import Flask, abort, jsonify, request

# --- ЧАСОВИЙ ПОЯС ---
try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = timezone(timedelta(hours=3))  # запасний варіант для старих середовищ

# --- НАЛАШТУВАННЯ ---
BOT_TOKEN = config("BOT_TOKEN")
CHAT_ID = str(config("CHAT_ID"))
MONO_TOKEN = config("MONO_TOKEN")
WHITE_CARD_ID = config("WHITE_CARD_ID")

# Секрети (придумайте довгі випадкові рядки: латиниця, цифри, _ і -)
TG_SECRET = config("TG_SECRET")                    # перевіряється в заголовку від Telegram
MONO_WEBHOOK_SECRET = config("MONO_WEBHOOK_SECRET")  # частина URL вебхука Монобанку
CRON_SECRET = config("CRON_SECRET", default="")    # для зовнішнього крону (необов'язково)
CASH_API_KEY = config("CASH_API_KEY", default="")  # для запису готівки з iPhone (Команди)

ENABLE_SCHEDULER = config("ENABLE_SCHEDULER", default=True, cast=bool)
GOOGLE_KEYS_FILE = config("GOOGLE_KEYS_FILE", default="google_keys.json")
SPREADSHEET_NAME = config("SPREADSHEET_NAME", default="MonoExpenses")
INCOME_SHEET_NAME = "Надходження"

HTTP_TIMEOUT = 15
DATE_FMT = "%Y-%m-%d %H:%M:%S"
TYPE_CARD = "Карта"
TYPE_CASH = "Готівка"
SAVINGS_CATEGORY = "Накопичення"

# Типи записів на аркуші "Надходження"
INCOME_TYPE_INCOME = "Надходження"
JAR_AUTO = "Автонакопичення"          # опис = назва банки, напр. "На щось"
JAR_TOPUP = "Поповнення банки"        # "Поповнення «На квартиру»"
JAR_ROUNDING = "Округлення балансу"   # "Округлення балансу «На щось»"
JAR_WITHDRAWAL = "Зняття з банки"     # "Часткове зняття банки «…»", "Виплата банки «…»"
OWN_TRANSFER = "Переказ на свою картку"

# Точні описи переказів на власні картки (порівняння без урахування регістру)
OWN_TRANSFER_DESCRIPTIONS = ("переказ на картку",)

# Чи надсилати в Telegram кожне дрібне автонакопичення й округлення.
# Їх буває 5–10 на день, тому за замовчуванням вони пишуться в таблицю мовчки,
# а сума видно в денному звіті.
NOTIFY_AUTO_SAVINGS = config("NOTIFY_AUTO_SAVINGS", default=False, cast=bool)

MONTHS_UK = ["січень", "лютий", "березень", "квітень", "травень", "червень",
             "липень", "серпень", "вересень", "жовтень", "листопад", "грудень"]

app = Flask(__name__)


# =====================================================================
# КАТЕГОРІЇ
# =====================================================================
FUEL_KEYWORDS = ("окко", "okko", "ukrnafta", "укрнафта", "upg", "wog", "бензин")

# Правила для картки: перевіряються по черзі ДО пошуку за MCC
MONO_RULES = [
    (("любомир л",), "Комунальні послуги"),
    (("олександр б",), "Орендна плата"),
    (FUEL_KEYWORDS, "Бензин"),
]

# Правила для готівки (за ключовим словом в описі)
CASH_RULES = [
    (("продукти",), "Продукти"),
    (("кафе", "ресторан"), "Кафе. Ресторани"),
    (("таксі",), "Таксі"),
    (("аптека",), "Аптеки"),
    (("одяг",), "Одяг"),
    (("розваги",), "Розваги та спорт"),
    (("квіти",), "Флористика"),
    (("олександр",), "Орендна плата"),
    (("любомир",), "Комунальні послуги"),
    (FUEL_KEYWORDS, "Бензин"),
]


def load_mcc_dataset(path="mcc_codes.json"):
    with open(path, encoding="utf-8") as f:
        raw = json.load(f)
    dataset = {}
    for code, value in raw.items():
        if isinstance(value, dict):
            name = value.get("uk") or value.get("ru") or "Невідома категорія"
        else:
            name = str(value)
        dataset[int(code)] = name
    return dataset


MCC_DATASET = load_mcc_dataset()


def match_rules(description, rules):
    desc = (description or "").lower()
    for keywords, category in rules:
        if any(k in desc for k in keywords):
            return category
    return None


def categorize_cash(description):
    return match_rules(description, CASH_RULES) or "Інше"


JAR_NAME_RE = re.compile(r"«(.+?)»")


def classify_jar_operation(amount, description):
    """
    Визначає операцію з банкою за описом.
    Повертає (тип, назва_банки) або (None, None), якщо це не операція з банкою.
    """
    desc = (description or "").strip()
    low = desc.lower()
    quoted = JAR_NAME_RE.search(desc)
    jar_name = quoted.group(1) if quoted else desc

    if amount < 0:
        if low in OWN_TRANSFER_DESCRIPTIONS:
            return OWN_TRANSFER, ""
        if low.startswith("поповнення «"):
            return JAR_TOPUP, jar_name
        if low.startswith("округлення балансу «"):
            return JAR_ROUNDING, jar_name
        if low.startswith("на "):
            return JAR_AUTO, jar_name
    elif amount > 0:
        if "зняття банки «" in low or "виплата банки «" in low:
            return JAR_WITHDRAWAL, jar_name
    return None, None


def is_savings_transfer(description):
    jar_type = classify_jar_operation(-1, description)[0]
    return jar_type is not None and jar_type != OWN_TRANSFER



def categorize_mono(mcc, description):
    if is_savings_transfer(description):
        return SAVINGS_CATEGORY
    return (match_rules(description, MONO_RULES)
            or MCC_DATASET.get(mcc)
            or f"❓ MCC: {mcc}")


# =====================================================================
# GOOGLE ТАБЛИЦІ (підключення ліниве, з повторною спробою)
# =====================================================================
_connect_lock = threading.Lock()
_write_lock = threading.Lock()
_spreadsheet = None


def get_spreadsheet():
    global _spreadsheet
    with _connect_lock:
        if _spreadsheet is None:
            gc = gspread.service_account(filename=GOOGLE_KEYS_FILE)
            _spreadsheet = gc.open(SPREADSHEET_NAME)
            print("✅ Підключилися до Google Таблиць")
        return _spreadsheet


def expenses_sheet():
    return get_spreadsheet().sheet1


def income_sheet():
    ss = get_spreadsheet()
    try:
        return ss.worksheet(INCOME_SHEET_NAME)
    except gspread.WorksheetNotFound:
        ws = ss.add_worksheet(title=INCOME_SHEET_NAME, rows=1000, cols=5)
        ws.append_row(["Дата", "Сума", "Опис", "Тип", "Банка"])
        return ws


def append_row(worksheet, row):
    with _write_lock:  # щоб два потоки не писали одночасно
        worksheet.append_row(row)


def parse_number(value):
    cleaned = value.replace("\xa0", "").replace(" ", "").replace(",", ".")
    return float(cleaned)


def load_cash_transactions(start, end):
    """Готівкові витрати з таблиці в проміжку [start, end]."""
    rows = expenses_sheet().get_all_values()
    result = []
    for row in rows[1:]:
        if len(row) < 2 or not row[0]:
            continue
        if len(row) >= 4 and row[3] == TYPE_CARD:
            continue
        try:
            dt = datetime.strptime(row[0][:19], DATE_FMT).replace(tzinfo=KYIV_TZ)
            amount = parse_number(row[1])
        except ValueError:
            continue
        if start <= dt <= end:
            result.append({
                "amount": amount,
                "category": row[4] if len(row) > 4 and row[4] else "Інше",
            })
    return result


# =====================================================================
# TELEGRAM
# =====================================================================
def esc(text):
    return html.escape(str(text or ""))


def send_to_telegram(text, chat_id=CHAT_ID):
    url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {"chat_id": chat_id, "text": text, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=HTTP_TIMEOUT)
        if not r.ok:
            print(f"Telegram помилка {r.status_code}: {r.text}")
    except requests.RequestException as e:
        print(f"Telegram недоступний: {e}")


def run_in_background(func, *args):
    def wrapper():
        try:
            func(*args)
        except Exception:
            print(traceback.format_exc())
    threading.Thread(target=wrapper, daemon=True).start()


# =====================================================================
# МОНОБАНК: ВИПИСКА (з урахуванням ліміту 1 запит / 60 с)
# =====================================================================
MONO_MIN_INTERVAL = 61
MONO_PAGE_SIZE = 500
_mono_lock = threading.Lock()
_last_mono_call = 0.0


def mono_wait_seconds():
    return max(0.0, MONO_MIN_INTERVAL - (time.time() - _last_mono_call))


def fetch_statement(from_ts, to_ts):
    """Повертає всі транзакції за період. Якщо їх понад 500, догружає частинами."""
    global _last_mono_call
    items, seen = [], set()
    with _mono_lock:
        while True:
            wait = mono_wait_seconds()
            if wait > 0:
                time.sleep(wait)

            url = f"https://api.monobank.ua/personal/statement/{WHITE_CARD_ID}/{from_ts}/{to_ts}"
            r = requests.get(url, headers={"X-Token": MONO_TOKEN}, timeout=HTTP_TIMEOUT)
            _last_mono_call = time.time()

            if r.status_code != 200:
                raise RuntimeError(f"Монобанк відповів {r.status_code}: {r.text[:200]}")

            batch = r.json()
            for item in batch:
                if item.get("id") not in seen:
                    seen.add(item.get("id"))
                    items.append(item)

            if len(batch) < MONO_PAGE_SIZE:
                return items
            to_ts = min(item["time"] for item in batch)  # виписка йде від нових до старих


# =====================================================================
# СТАТИСТИКА
# =====================================================================
def period_bounds(kind, now=None):
    now = now or datetime.now(KYIV_TZ)
    midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
    if kind == "today":
        return midnight, now
    if kind == "month":
        return midnight.replace(day=1), now
    if kind == "prev_month":
        end = midnight.replace(day=1) - timedelta(seconds=1)
        return end.replace(day=1, hour=0, minute=0, second=0), end
    raise ValueError(f"Невідомий період: {kind}")


def build_stats(kind):
    start, end = period_bounds(kind)
    transactions = fetch_statement(int(start.timestamp()), int(end.timestamp()))

    card_total = savings_total = withdrawn_total = own_transfer_total = cash_total = 0.0
    categories = {}

    for item in transactions:
        amount = item.get("amount", 0)
        if amount > 0:
            jar_type, _ = classify_jar_operation(amount, item.get("description", ""))
            if jar_type == JAR_WITHDRAWAL:
                withdrawn_total += amount / 100
            continue
        if amount == 0:
            continue
        spent = abs(amount) / 100
        if classify_jar_operation(amount, item.get("description", ""))[0] == OWN_TRANSFER:
            own_transfer_total += spent  # гроші лишаються у вас
            continue
        category = categorize_mono(item.get("mcc"), item.get("description", ""))
        if category == SAVINGS_CATEGORY:
            savings_total += spent  # накопичення не вважаємо витратами
            continue
        card_total += spent
        categories[category] = categories.get(category, 0) + spent

    for item in load_cash_transactions(start, end):
        cash_total += item["amount"]
        key = f"💵 {item['category']} (готівка)"
        categories[key] = categories.get(key, 0) + item["amount"]

    total = card_total + cash_total

    if kind == "today":
        title = "🌙 <b>Підсумки дня</b>"
    elif kind == "month":
        title = "📊 <b>Статистика за місяць</b>"
    else:
        title = f"🏆 <b>ФІНАЛЬНИЙ ЗВІТ: {MONTHS_UK[start.month - 1]} {start.year}</b> 🏆"

    if total == 0 and not (savings_total or withdrawn_total or own_transfer_total):
        return f"{title}\nВитрат не зафіксовано 💰"

    lines = [
        title,
        f"💳 Картка: {card_total:.2f} грн",
        f"💵 Готівка: {cash_total:.2f} грн",
        f"💰 <b>РАЗОМ:</b> {total:.2f} грн",
    ]
    if savings_total:
        lines.append(f"🐷 Відкладено в банки: {savings_total:.2f} грн")
    if withdrawn_total:
        lines.append(f"🔓 Знято з банок: {withdrawn_total:.2f} грн")
    if own_transfer_total:
        lines.append(f"🔁 Переказано на свої картки: {own_transfer_total:.2f} грн")

    if categories:
        lines.append("\n<b>Деталізація:</b>")
        for cat, amount in sorted(categories.items(), key=lambda x: x[1], reverse=True):
            lines.append(f"▪️ {esc(cat)}: {amount:.2f} грн")

    return "\n".join(lines)


def send_stats(kind, chat_id=CHAT_ID):
    if mono_wait_seconds() > 5:
        send_to_telegram("⏳ Монобанк дозволяє 1 запит на хвилину, рахую трохи згодом...", chat_id)
    try:
        send_to_telegram(build_stats(kind), chat_id)
    except Exception as e:
        send_to_telegram(f"❌ <b>Помилка розрахунку:</b>\n{esc(e)}", chat_id)
        raise


# =====================================================================
# ОБРОБКА ТРАНЗАКЦІЙ МОНОБАНКУ
# =====================================================================
MAX_PROCESSED = 1000
_processed_tx = OrderedDict()
_processed_lock = threading.Lock()


def already_processed(tx_id):
    with _processed_lock:
        if tx_id in _processed_tx:
            return True
        _processed_tx[tx_id] = True
        if len(_processed_tx) > MAX_PROCESSED:
            _processed_tx.popitem(last=False)  # видаляємо найстаріший
        return False


def process_mono_transaction(data):
    payload = data.get("data") or {}
    if payload.get("account") != WHITE_CARD_ID:
        return  # подія з іншої картки чи банки

    item = payload.get("statementItem") or {}
    tx_id = item.get("id")
    if not tx_id or already_processed(tx_id):
        return

    amount = item.get("amount", 0)
    description = item.get("description") or "Без опису"
    balance = item.get("balance", 0) / 100
    tx_time = item.get("time")
    dt = datetime.fromtimestamp(tx_time, tz=KYIV_TZ) if tx_time else datetime.now(KYIV_TZ)
    date_str = dt.strftime(DATE_FMT)

    sheet_warning = ""

    jar_type, jar_name = classify_jar_operation(amount, description)

    if jar_type is not None:
        # Операція з банкою чи переказ на свою картку: аркуш "Надходження", не витрати
        value = abs(amount) / 100
        try:
            append_row(income_sheet(), [date_str, value, description, jar_type, jar_name])
        except Exception as e:
            print(f"Не вдалося записати внутрішню операцію: {e}")
            sheet_warning = "\n⚠️ Не вдалося записати в таблицю"

        is_minor = jar_type in (JAR_AUTO, JAR_ROUNDING)
        if not is_minor or NOTIFY_AUTO_SAVINGS or sheet_warning:
            icon = {JAR_WITHDRAWAL: "🔓", OWN_TRANSFER: "🔁"}.get(jar_type, "🐷")
            sign = "+" if jar_type == JAR_WITHDRAWAL else ""
            jar_line = f"🫙 <b>Банка:</b> {esc(jar_name)}\n" if jar_name else ""
            send_to_telegram(
                f"{icon} <b>{jar_type}:</b> {sign}{value:.2f} грн\n"
                f"{jar_line}"
                f"🏦 <b>Залишок на картці:</b> {balance:.2f} грн"
                f"{sheet_warning}"
            )

    elif amount < 0:
        spent = abs(amount) / 100
        category = categorize_mono(item.get("mcc"), description)
        try:
            append_row(expenses_sheet(), [date_str, spent, description, TYPE_CARD, category])
        except Exception as e:
            print(f"Не вдалося записати витрату: {e}")
            sheet_warning = "\n⚠️ Не вдалося записати в таблицю"

        send_to_telegram(
            f"💸 <b>Нова витрата:</b> {spent:.2f} грн\n"
            f"🏷 <b>Категорія:</b> {esc(category)}\n"
            f"📝 <b>Деталі:</b> {esc(description)}\n"
            f"🏦 <b>Залишок:</b> {balance:.2f} грн"
            f"{sheet_warning}"
        )

    elif amount > 0:
        income = amount / 100
        try:
            append_row(income_sheet(), [date_str, income, description, INCOME_TYPE_INCOME])
        except Exception as e:
            print(f"Не вдалося записати надходження: {e}")
            sheet_warning = "\n⚠️ Не вдалося записати в таблицю"

        send_to_telegram(
            f"💰 <b>Надходження:</b> +{income:.2f} грн\n"
            f"📝 <b>Деталі:</b> {esc(description)}\n"
            f"🏦 <b>Залишок:</b> {balance:.2f} грн"
            f"{sheet_warning}"
        )


# =====================================================================
# ГОТІВКА
# =====================================================================
def parse_cash_args(args):
    parts = args.split(maxsplit=1)
    if not parts:
        raise ValueError("немає суми")
    amount = float(parts[0].replace(",", "."))
    if not math.isfinite(amount) or amount <= 0 or amount > 1_000_000:
        raise ValueError("некоректна сума")
    description = parts[1].strip() if len(parts) > 1 else "Без опису"
    return round(amount, 2), description


def record_cash(amount, description, category=None):
    """Записує готівкову витрату в таблицю. Повертає категорію."""
    category = (category or "").strip()[:50] or categorize_cash(description)
    date_str = datetime.now(KYIV_TZ).strftime(DATE_FMT)
    append_row(expenses_sheet(), [date_str, amount, description, TYPE_CASH, category])
    return category


def handle_cash(args, chat_id):
    try:
        amount, description = parse_cash_args(args)
    except ValueError:
        send_to_telegram("❌ Пиши так: <code>/cash 100 продукти</code>", chat_id)
        return

    try:
        category = record_cash(amount, description)
    except Exception as e:
        send_to_telegram(f"❌ Не вдалося записати в таблицю: {esc(e)}", chat_id)
        raise

    send_to_telegram(f"✅ Записано: {amount:.2f} грн ({esc(category)}) — {esc(description)}", chat_id)


HELP_TEXT = (
    "🤖 <b>Команди:</b>\n"
    "/today — витрати за сьогодні\n"
    "/stats — витрати за місяць\n"
    "/cash 100 продукти — записати готівку"
)


# =====================================================================
# ВЕБХУКИ
# =====================================================================
def secrets_equal(a, b):
    return bool(b) and hmac.compare_digest(str(a or ""), str(b))


@app.route("/", methods=["GET"])
def home():
    return "Bot is alive and working!", 200


@app.route("/mono-webhook/<secret>", methods=["GET", "POST"])
def mono_webhook(secret):
    if not secrets_equal(secret, MONO_WEBHOOK_SECRET):
        abort(404)
    if request.method == "GET":
        return "OK", 200  # Монобанк перевіряє адресу GET-запитом

    data = request.get_json(silent=True) or {}
    if data.get("type") == "StatementItem":
        run_in_background(process_mono_transaction, data)
    return "OK", 200


@app.route("/tg-webhook", methods=["POST"])
def telegram_webhook():
    if not secrets_equal(request.headers.get("X-Telegram-Bot-Api-Secret-Token"), TG_SECRET):
        abort(403)

    data = request.get_json(silent=True) or {}
    message = data.get("message") or {}
    text = (message.get("text") or "").strip()
    chat_id = str((message.get("chat") or {}).get("id", ""))

    if not text or chat_id != CHAT_ID:
        return "OK", 200  # ігноруємо чужих

    command, _, args = text.partition(" ")
    command = command.split("@")[0].lower()  # /stats@MyBot -> /stats

    if command == "/stats":
        run_in_background(send_stats, "month", chat_id)
    elif command == "/today":
        run_in_background(send_stats, "today", chat_id)
    elif command == "/cash":
        run_in_background(handle_cash, args, chat_id)
    elif command in ("/start", "/help"):
        send_to_telegram(HELP_TEXT, chat_id)

    return "OK", 200


# Запис готівки з iPhone (застосунок «Команди»).
# POST JSON: {"amount": 150, "description": "кава", "category": "Кафе. Ресторани"}
# Заголовок: X-Api-Key: <CASH_API_KEY>. Поле category необов'язкове.
@app.route("/api/cash", methods=["GET", "POST"])
def api_cash():
    # Завжди відповідаємо JSON, щоб «Команди» могли показати зрозуміле повідомлення
    if request.method != "POST":
        return jsonify(ok=False, message="❌ Потрібен метод POST"), 405
    if not CASH_API_KEY:
        return jsonify(ok=False, message="❌ На сервері не задано CASH_API_KEY"), 500
    if not secrets_equal(request.headers.get("X-Api-Key"), CASH_API_KEY):
        return jsonify(ok=False, message="❌ Невірний ключ X-Api-Key"), 403

    data = request.get_json(silent=True) or {}
    amount_raw = str(data.get("amount", "")).strip()
    description = str(data.get("description", "")).strip()[:200]
    try:
        amount, description = parse_cash_args(f"{amount_raw} {description}")
    except ValueError:
        return jsonify(ok=False, message="❌ Некоректна сума"), 400

    try:
        category = record_cash(amount, description, data.get("category"))
    except Exception as e:
        print(traceback.format_exc())
        return jsonify(ok=False, message=f"❌ Не вдалося записати: {e}"), 500

    message = f"✅ {amount:.2f} грн — {category}"
    run_in_background(send_to_telegram,
                      f"📱 <b>Готівка з iPhone:</b> {amount:.2f} грн\n"
                      f"🏷 {esc(category)} — {esc(description)}")
    return jsonify(ok=True, message=message), 200


# Запасний шлях для зовнішнього крону (наприклад, cron-job.org),
# якщо безкоштовний Render «заснув» і внутрішній планувальник не спрацював.
@app.route("/cron/<job>", methods=["GET", "POST"])
def cron(job):
    if not secrets_equal(request.args.get("key"), CRON_SECRET):
        abort(403)
    if job == "daily":
        run_in_background(send_stats, "today")
    elif job == "monthly":
        run_in_background(send_stats, "prev_month")
    else:
        abort(404)
    return "OK", 200


# =====================================================================
# ПЛАНУВАЛЬНИК
# =====================================================================
# Увага: при gunicorn з кількома воркерами планувальник стартує в кожному.
# Запускайте з -w 1 або вимкніть його (ENABLE_SCHEDULER=False) і використовуйте /cron.
if ENABLE_SCHEDULER:
    scheduler = BackgroundScheduler(timezone=KYIV_TZ)
    scheduler.add_job(send_stats, CronTrigger(hour=23, minute=55), args=["today"])
    # Звіт за минулий місяць 1-го числа, щоб не губити останні хвилини місяця
    scheduler.add_job(send_stats, CronTrigger(day=1, hour=0, minute=5), args=["prev_month"])
    scheduler.start()


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)