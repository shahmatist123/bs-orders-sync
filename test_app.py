import io
import json
import random
import threading
import unittest
import urllib.error
import urllib.request
from http.server import HTTPServer
from pathlib import Path
from unittest import mock

import app

# Снимок /api/orders на 14.10.2026 и ожидаемый ответ — тесты не ходят в сеть.
FIXTURES = Path(__file__).parent / "fixtures"
ORDERS = json.loads((FIXTURES / "orders.json").read_text(encoding="utf-8"))
EXPECTED = json.loads((FIXTURES / "expected.json").read_text(encoding="utf-8"))


def order(phone, amount=100, created_at="2026-10-01T00:00:00Z", name="X", email=""):
    return {"name": name, "phone": phone, "email": email, "amount": amount, "created_at": created_at}


class NormalizePhoneTest(unittest.TestCase):
    def test_valid_formats(self):
        cases = {
            "+7 999 123-45-67": "+79991234567",
            "8 (999) 123 45 67": "+79991234567",
            "79991234567": "+79991234567",
            "8-916-000-00-01": "+79160000001",
            "+7 (916) 000 00 01": "+79160000001",
            "+79219998877": "+79219998877",
            "+7-926-123-45-67": "+79261234567",
            "9005551212": "+79005551212",
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(app.normalize_phone(raw), expected)

    def test_invalid_formats(self):
        for raw in ["123", "", "не указан", "+7 999", None, "19991234567", "8999123456789", "1234567890"]:
            with self.subTest(raw=raw):
                self.assertIsNone(app.normalize_phone(raw))


class DedupTest(unittest.TestCase):
    def test_keeps_max_amount(self):
        result = app.sync([
            order("+79991234567", 1000, name="A"),
            order("89991234567", 5000, name="B"),
            order("79991234567", 3000, name="C"),
        ])
        self.assertEqual([c["name"] for c in result["clients"]], ["B"])

    def test_equal_amount_keeps_later(self):
        result = app.sync([
            order("+79160000001", 200, "2026-10-05T12:00:00Z", name="later"),
            order("89160000001", 200, "2026-10-01T12:00:00Z", name="earlier"),
        ])
        self.assertEqual(result["clients"][0]["name"], "later")

    def test_amount_beats_date(self):
        result = app.sync([
            order("+79219998877", 0, "2026-10-02T14:00:00Z", name="big"),
            order("+79219998877", 1500, "2026-10-01T00:00:00Z", name="small"),
        ])
        self.assertEqual(result["clients"][0]["name"], "small")

    def test_string_amount_compared_as_number(self):
        result = app.sync([order("+79990000000", "900", name="str"), order("+79990000000", 1000, name="int")])
        self.assertEqual(result["clients"][0]["name"], "int")

    def test_full_tie_resolved_by_id(self):
        first = {**order("+79990000000", name="old"), "id": 1}
        second = {**order("+79990000000", name="new"), "id": 2}
        self.assertEqual(app.sync([first, second])["clients"][0]["name"], "new")
        self.assertEqual(app.sync([second, first])["clients"][0]["name"], "new")

    def test_bad_amount_and_date_do_not_crash(self):
        result = app.sync([order("+79990000000", None, "вчера", name="bad"), order("+79990000000", 1, name="ok")])
        self.assertEqual(result["clients"][0]["name"], "ok")

    def test_client_has_only_schema_fields(self):
        client = app.sync([order("+79990000000")])["clients"][0]
        self.assertEqual(set(client), {"name", "phone", "email", "amount"})


class SyncOnSnapshotTest(unittest.TestCase):
    def test_full_result(self):
        self.assertEqual(app.sync(ORDERS), EXPECTED)

    def test_stats_add_up(self):
        s = app.sync(ORDERS)["stats"]
        self.assertEqual(s["received"], s["unique"] + s["duplicates_merged"] + s["invalid_dropped"])

    def test_idempotent(self):
        self.assertEqual(app.sync(ORDERS), app.sync(ORDERS))

    def test_order_independent(self):
        shuffled = ORDERS[:]
        for seed in range(5):
            random.Random(seed).shuffle(shuffled)
            self.assertEqual(app.sync(shuffled), EXPECTED)

    def test_empty_input(self):
        self.assertEqual(app.sync([]), {
            "clients": [],
            "stats": {"received": 0, "unique": 0, "duplicates_merged": 0, "invalid_dropped": 0},
        })


class HttpTest(unittest.TestCase):
    def setUp(self):
        self.server = HTTPServer(("127.0.0.1", 0), app.Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_port}/"

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()

    def test_returns_json(self):
        with mock.patch.object(app, "fetch_orders", return_value=ORDERS):
            with urllib.request.urlopen(self.url) as resp:
                self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")
                self.assertEqual(json.load(resp), EXPECTED)

    def test_query_string_ignored(self):
        with mock.patch.object(app, "fetch_orders", return_value=ORDERS):
            with urllib.request.urlopen(self.url + "?run=2") as resp:
                self.assertEqual(json.load(resp), EXPECTED)

    def test_head_has_headers_without_body(self):
        with mock.patch.object(app, "fetch_orders", return_value=ORDERS):
            with urllib.request.urlopen(urllib.request.Request(self.url, method="HEAD")) as resp:
                self.assertEqual(resp.headers["Content-Type"], "application/json; charset=utf-8")
                self.assertEqual(resp.read(), b"")

    def test_health(self):
        with urllib.request.urlopen(self.url + "health") as resp:
            self.assertEqual(json.load(resp), {"status": "ok"})

    def test_unknown_path_404(self):
        self.assert_status(self.url + "nope", 404)

    def test_source_down_gives_502(self):
        with mock.patch.object(app, "fetch_orders", side_effect=app.SourceError("boom")):
            self.assert_status(self.url, 502)

    def assert_status(self, url, code):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            urllib.request.urlopen(url)
        self.assertEqual(ctx.exception.code, code)


class FetchOrdersTest(unittest.TestCase):
    def fake_response(self, payload):
        resp = mock.MagicMock()
        resp.__enter__.return_value = io.BytesIO(payload)
        return resp

    def test_parses_list(self):
        with mock.patch("urllib.request.urlopen", return_value=self.fake_response(json.dumps(ORDERS).encode())):
            self.assertEqual(app.fetch_orders(), ORDERS)

    def test_rejects_non_list(self):
        for payload in [b'{"orders": []}', b"[1, 2]", b"not json"]:
            with self.subTest(payload=payload):
                with mock.patch("urllib.request.urlopen", return_value=self.fake_response(payload)):
                    with self.assertRaises(app.SourceError):
                        app.fetch_orders()

    def test_network_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=OSError("timeout")):
            with self.assertRaises(app.SourceError):
                app.fetch_orders()


if __name__ == "__main__":
    unittest.main()
