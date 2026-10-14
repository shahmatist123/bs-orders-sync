"""Мини-синхронизатор заявок: тянет /api/orders, нормализует телефоны, схлопывает дубли."""
import json
import os
import re
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer

SOURCE_URL = os.environ.get("SOURCE_URL", "https://test.ooobs.ru/api/orders")


def normalize_phone(raw):
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":
        return "+7" + digits
    return None


def to_amount(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_time(value):
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def sync(orders):
    best = {}
    invalid = 0
    for order in orders:
        phone = normalize_phone(order.get("phone"))
        if phone is None:
            invalid += 1
            continue
        key = (to_amount(order.get("amount")), to_time(order.get("created_at")))
        if phone not in best or key > best[phone][0]:
            best[phone] = (key, order)

    clients = []
    for phone in sorted(best):
        order = best[phone][1]
        amount = to_amount(order.get("amount"))
        clients.append({
            "name": order.get("name"),
            "phone": phone,
            "email": order.get("email"),
            "amount": int(amount) if amount.is_integer() else amount,
        })

    received = len(orders)
    return {
        "clients": clients,
        "stats": {
            "received": received,
            "unique": len(clients),
            "duplicates_merged": received - invalid - len(clients),
            "invalid_dropped": invalid,
        },
    }


def fetch_orders():
    with urllib.request.urlopen(SOURCE_URL, timeout=10) as resp:
        return json.load(resp)


class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        try:
            status, body = 200, sync(fetch_orders())
        except Exception as exc:  # источник недоступен — честная ошибка, а не пустой ответ
            status, body = 502, {"error": str(exc)}
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


if __name__ == "__main__":
    HTTPServer(("0.0.0.0", int(os.environ.get("PORT", "8000"))), Handler).serve_forever()
