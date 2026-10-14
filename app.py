"""Мини-синхронизатор заявок: тянет /api/orders, нормализует телефоны, схлопывает дубли.

Сервис без состояния: каждый GET заново забирает заявки и детерминированно считает
результат, поэтому повторная синхронизация всегда даёт тот же ответ.
"""
from __future__ import annotations

import json
import logging
import os
import re
import urllib.request
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import urlsplit

SOURCE_URL = os.environ.get("SOURCE_URL", "https://test.ooobs.ru/api/orders")
SOURCE_TIMEOUT = float(os.environ.get("SOURCE_TIMEOUT", "10"))

log = logging.getLogger("bs-orders-sync")


class SourceError(Exception):
    """Источник заявок недоступен или вернул не то."""


def normalize_phone(raw: Any) -> str | None:
    """Приводит телефон к +7XXXXXXXXXX или возвращает None, если номер невалиден."""
    digits = re.sub(r"\D", "", str(raw or ""))
    if len(digits) == 11 and digits[0] in "78":
        return "+7" + digits[1:]
    if len(digits) == 10 and digits[0] == "9":  # номер без кода страны
        return "+7" + digits
    return None


def to_number(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def to_timestamp(value: Any) -> float:
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def priority(order: dict) -> tuple[float, float, float]:
    """Чем больше, тем приоритетнее: amount, затем created_at, затем id — для полной детерминированности."""
    return to_number(order.get("amount")), to_timestamp(order.get("created_at")), to_number(order.get("id"))


def sync(orders: list[dict]) -> dict:
    best: dict[str, dict] = {}
    invalid = 0
    for order in orders:
        phone = normalize_phone(order.get("phone"))
        if phone is None:
            invalid += 1
            continue
        if phone not in best or priority(order) > priority(best[phone]):
            best[phone] = order

    clients = []
    for phone in sorted(best):
        order = best[phone]
        amount = to_number(order.get("amount"))
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


def fetch_orders() -> list[dict]:
    request = urllib.request.Request(SOURCE_URL, headers={"Accept": "application/json", "User-Agent": "bs-orders-sync"})
    try:
        with urllib.request.urlopen(request, timeout=SOURCE_TIMEOUT) as resp:
            data = json.load(resp)
    except (OSError, ValueError) as exc:
        raise SourceError(f"не удалось получить заявки: {exc}") from exc
    if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
        raise SourceError("источник вернул не массив объектов")
    return data


class Handler(BaseHTTPRequestHandler):
    server_version = "bs-orders-sync"

    def do_GET(self) -> None:
        self.route(with_body=True)

    def do_HEAD(self) -> None:
        self.route(with_body=False)

    def route(self, with_body: bool) -> None:
        self.with_body = with_body
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json(200, {"status": "ok"})
        elif path == "/":
            try:
                self.send_json(200, sync(fetch_orders()))
            except SourceError as exc:
                log.error("%s", exc)
                self.send_json(502, {"error": str(exc)})
        else:
            self.send_json(404, {"error": "not found"})

    def send_json(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if self.with_body:
            self.wfile.write(data)

    def log_message(self, format: str, *args: Any) -> None:
        log.info("%s %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    port = int(os.environ.get("PORT", "8000"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    log.info("слушаю порт %s, источник %s", port, SOURCE_URL)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
