"""
Виводить сирі дані транзакцій з API Монобанку для аналізу.

Приклади запуску:
    python inspect_transactions.py                    # останні 7 днів, коротко
    python inspect_transactions.py --days 30          # останні 30 днів
    python inspect_transactions.py --search okko      # тільки де в описі є "okko"
    python inspect_transactions.py --mcc 5411         # тільки з MCC 5411
    python inspect_transactions.py --id AbCdEf123     # одна транзакція за ID
    python inspect_transactions.py --search wog --raw # повний JSON від банку
    python inspect_transactions.py --save dump.json   # зберегти все у файл

Увага: Монобанк дозволяє 1 запит на 60 секунд і період до 31 дня.
"""
import argparse
import json
import sys
import time
from datetime import datetime, timezone, timedelta

import requests
from decouple import config

try:
    from zoneinfo import ZoneInfo
    KYIV_TZ = ZoneInfo("Europe/Kyiv")
except Exception:
    KYIV_TZ = timezone(timedelta(hours=3))

MONO_TOKEN = config("MONO_TOKEN")
WHITE_CARD_ID = config("WHITE_CARD_ID")

# Щоб кирилиця й емодзі коректно виводились у консоль Windows
sys.stdout.reconfigure(encoding="utf-8")


def fetch_statement(account, days):
    to_ts = int(time.time())
    from_ts = to_ts - days * 24 * 3600
    url = f"https://api.monobank.ua/personal/statement/{account}/{from_ts}/{to_ts}"
    r = requests.get(url, headers={"X-Token": MONO_TOKEN}, timeout=30)

    if r.status_code == 429:
        sys.exit("❌ Забагато запитів. Зачекайте хвилину і спробуйте знову.")
    if r.status_code != 200:
        sys.exit(f"❌ Монобанк відповів {r.status_code}: {r.text}")
    return r.json()


def matches(item, args):
    if args.id and item.get("id") != args.id:
        return False
    if args.search and args.search.lower() not in (item.get("description") or "").lower():
        return False
    if args.mcc is not None and item.get("mcc") != args.mcc:
        return False
    return True


def short_line(item):
    dt = datetime.fromtimestamp(item["time"], tz=KYIV_TZ).strftime("%Y-%m-%d %H:%M")
    amount = item.get("amount", 0) / 100
    return (f"{dt} | {amount:>10.2f} | MCC {item.get('mcc', '-'):>4} | "
            f"{item.get('description', '')} | id={item.get('id')}")


def main():
    parser = argparse.ArgumentParser(description="Аналіз транзакцій Монобанку")
    parser.add_argument("--days", type=int, default=7, help="за скільки днів (макс. 31)")
    parser.add_argument("--search", help="пошук за текстом в описі")
    parser.add_argument("--mcc", type=int, help="фільтр за MCC")
    parser.add_argument("--id", help="ID конкретної транзакції")
    parser.add_argument("--raw", action="store_true", help="показати повний JSON")
    parser.add_argument("--save", help="зберегти результат у JSON-файл")
    parser.add_argument("--account", default=WHITE_CARD_ID, help="ID рахунку")
    args = parser.parse_args()

    days = max(1, min(args.days, 31))
    transactions = fetch_statement(args.account, days)
    found = [t for t in transactions if matches(t, args)]

    print(f"Отримано від банку: {len(transactions)}, після фільтрів: {len(found)}\n")

    for item in found:
        print(short_line(item))
        if args.raw:
            print(json.dumps(item, ensure_ascii=False, indent=2))
            print("-" * 60)

    if args.save:
        with open(args.save, "w", encoding="utf-8") as f:
            json.dump(found, f, ensure_ascii=False, indent=2)
        print(f"\n💾 Збережено у {args.save}")


if __name__ == "__main__":
    main()