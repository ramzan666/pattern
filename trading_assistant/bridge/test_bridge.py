import http.client
import json
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from io import BytesIO

from .app import DeliveryError, InvalidEvent, Store, TelegramSender, Worker, handler, render, validate

NOW = 1800000000000
SECRET = "test-secret-no-real-credentials-12345"


def event(**changes):
    data = dict(schema_version=1, strategy="trend_retest_v1", event_id="BTC|1|setup",
                setup_id="BTC|1", sequence=1, event="setup", stage="armed", symbol="BINANCE:BTCUSDT",
                timeframe="60", direction="long", trend="bullish", event_time=NOW, bar_time=NOW - 3600000,
                entry=100, stop=99, target=102, rr=2, risk_pct=0.5, expires_at=NOW + 3600000,
                reason="Ретест подтверждён")
    data.update(changes)
    return data


class FakeSender:
    def __init__(self, errors=()):
        self.errors, self.messages = list(errors), []

    def send(self, message):
        if self.errors:
            raise self.errors.pop(0)
        self.messages.append(message)


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.db.close()

    def test_invalid_schema_numbers_and_price_order(self):
        for change in ({"entry": float("nan")}, {"stop": float("inf")}, {"risk_pct": True},
                       {"entry": 0}, {"stop": 101}, {"symbol": "bad"}, {"stage": "active"},
                       {"event": []}, {"entry": 10 ** 1000}, {"event_time": NOW + 61000},
                       {"schema_version": True}, {"risk_pct": 0}, {"risk_pct": None}, {"entry": "100"},
                       {"expires_at": 10 ** 50},
                       {"token": "secret-must-not-be-persisted"}):
            with self.subTest(change=change), self.assertRaises(InvalidEvent):
                validate(event(**change), NOW)
        self.assertEqual(validate(event(direction="short", stop=101, target=98), NOW)["direction"], "short")

    def test_pine_preparation_states_have_zero_risk_until_plan_exists(self):
        for kind, stage in (("context", "idle"), ("breakout", "waiting_retest"), ("cancel", "canceled")):
            data = event(event=kind, stage=stage, entry=None, stop=None, target=None, rr=None, risk_pct=0)
            self.assertEqual(validate(data, NOW)["risk_pct"], 0)

    def test_duplicate_is_atomic_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "outbox.sqlite3")
            store = Store(path)
            self.assertEqual(store.ingest(event(), NOW), "accepted")
            store.db.close()
            store = Store(path)
            self.assertEqual(store.ingest(event(), NOW + 400000), "duplicate")
            self.assertEqual(store.health(), {"pending": 1})
            with self.assertRaises(InvalidEvent):
                store.ingest(event(target=103), NOW)
            store.db.close()

    def test_late_expired_and_ordered_events_do_not_replace_state(self):
        self.assertEqual(self.store.ingest(event(), NOW), "accepted")
        cases = [(event(event_id="older", event_time=NOW - 1, sequence=200), "suppressed_order"),
                 (event(event_id="expired", event_time=NOW + 1, expires_at=NOW), "suppressed_expired"),
                 (event(event_id="late", event_time=NOW - 400000), "suppressed_late")]
        for data, status in cases:
            self.assertEqual(self.store.ingest(data, NOW), status)
        self.assertEqual(self.store.state("BINANCE:BTCUSDT")["event_id"], "BTC|1|setup")
        # A new Pine runtime can restart sequence; timestamp remains authoritative.
        self.assertEqual(self.store.ingest(event(event_id="fresh", sequence=0, event_time=NOW + 1), NOW), "accepted")
        self.assertEqual(self.store.ingest(event(event_id="ETH", symbol="BINANCE:ETHUSDT", event_time=NOW - 1), NOW), "accepted")

    def test_terminal_setup_cannot_be_rearmed(self):
        self.store.ingest(event(), NOW)
        self.store.ingest(event(event_id="cancel", event="cancel", stage="canceled", sequence=2), NOW)
        self.assertEqual(self.store.ingest(event(event_id="resurrect", sequence=3), NOW), "suppressed_closed")
        self.assertEqual(self.store.ingest(event(event_id="context-resurrect", event="context", sequence=4), NOW), "suppressed_closed")
        self.assertEqual(self.store.state("BINANCE:BTCUSDT")["stage"], "canceled")

    def test_old_trigger_is_not_sent_after_network_outage(self):
        self.store.ingest(event(event_id="trigger", event="trigger", stage="active", expires_at=None), NOW)
        sender = FakeSender([DeliveryError(delay=301)])
        worker = Worker(self.store, sender)
        worker.run_once(NOW / 1000)
        worker.run_once(NOW / 1000 + 301)
        self.assertEqual(sender.messages, [])
        self.assertEqual(self.store.health(), {"suppressed": 1})

    def test_context_updates_silently_and_preserves_plan(self):
        self.store.ingest(event(), NOW)
        self.store.ingest(event(event_id="context", event="context", sequence=2), NOW)
        self.assertEqual(self.store.health(), {"pending": 1})
        self.assertEqual(self.store.state("BINANCE:BTCUSDT")["stage"], "armed")
        sender = FakeSender()
        self.assertTrue(Worker(self.store, sender).run_once(NOW / 1000))
        self.assertEqual(len(sender.messages), 1)

    def test_worker_rate_limit_then_success_and_permanent_failure(self):
        self.store.ingest(event(), NOW)
        sender = FakeSender([DeliveryError(delay=7)])
        worker = Worker(self.store, sender)
        worker.run_once(NOW / 1000)
        self.assertFalse(worker.run_once(NOW / 1000 + 6))
        worker.run_once(NOW / 1000 + 7)
        self.assertEqual(self.store.health(), {"sent": 1})
        self.assertEqual(len(sender.messages), 1)
        self.store.ingest(event(event_id="new", setup_id="new", sequence=2), NOW)
        Worker(self.store, FakeSender([DeliveryError(retryable=False)])).run_once(NOW / 1000)
        self.assertEqual(self.store.health(), {"failed": 1, "sent": 1})

    def test_delayed_queue_drops_expired_and_superseded_entries(self):
        self.store.ingest(event(), NOW)
        sender = FakeSender()
        Worker(self.store, sender).run_once(NOW / 1000 + 3600)
        self.assertEqual(sender.messages, [])
        self.assertEqual(self.store.health(), {"suppressed": 1})
        self.store.ingest(event(event_id="second", setup_id="two", sequence=2), NOW)
        self.store.ingest(event(event_id="two-cancel", setup_id="two", event="cancel", stage="canceled", sequence=3), NOW)
        worker = Worker(self.store, sender)
        worker.run_once(NOW / 1000)
        worker.run_once(NOW / 1000)
        self.assertEqual(len(sender.messages), 1)
        self.assertIn("Сценарий отменён", sender.messages[0])

    def test_telegram_429_response_has_retry_delay(self):
        exc = HTTPError("https://redacted.invalid", 429, "rate limited", {},
                        BytesIO(b'{"parameters":{"retry_after":11}}'))
        with patch("trading_assistant.bridge.app.request.urlopen", side_effect=exc):
            with self.assertRaises(DeliveryError) as caught:
                TelegramSender("fake-not-a-token", "fake-chat").send("test")
        self.assertTrue(caught.exception.retryable)
        self.assertEqual(caught.exception.delay, 11)

    def test_russian_message_and_html_escape(self):
        message = render(event(reason="<script> & bad"))
        self.assertIn("Вход подготовлен", message)
        self.assertIn("SL: 99\n💰 TP: 102", message)
        self.assertIn("1:2", message)
        self.assertIn("&lt;script&gt; &amp; bad", message)
        self.assertIn("symbol=BINANCE%3ABTCUSDT", message)
        self.assertIn("фактическое исполнение", render(event(event="trigger", stage="active")))


class HTTPTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler(self.store, SECRET))
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.store.db.close()

    def call(self, method, path, body=None, headers=None):
        connection = http.client.HTTPConnection(*self.server.server_address, timeout=2)
        connection.request(method, path, body, headers or {})
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    def test_auth_json_validation_fast_ack_and_state_redaction(self):
        self.assertEqual(self.call("GET", "/health")[0], 200)
        self.assertEqual(self.call("GET", "/state?symbol=BINANCE%3ABTCUSDT")[0], 401)
        data = event(event_time=int(time.time() * 1000), bar_time=int(time.time() * 1000), secret="wrong")
        self.assertEqual(self.call("POST", "/webhook", json.dumps(data))[0], 401)
        data["secret"] = SECRET
        start = time.monotonic()
        data["expires_at"] = data["event_time"] + 3600000
        self.assertEqual(self.call("POST", "/webhook", json.dumps(data)), (202, {"status": "accepted"}))
        self.assertLess(time.monotonic() - start, 2)
        state = self.call("GET", "/state?symbol=BINANCE%3ABTCUSDT", headers={"X-Webhook-Secret": SECRET})
        self.assertEqual(state[0], 200)
        self.assertNotIn("secret", state[1]["state"])
        self.assertFalse(state[1]["stale"])
        self.assertEqual(self.call("POST", "/webhook", "{")[0], 400)
        bad = json.dumps(data).replace('"entry": 100', '"entry": NaN')
        self.assertEqual(self.call("POST", "/webhook", bad)[0], 400)
        self.assertEqual(self.call("POST", "/webhook", '{"secret":"x","secret":"y"}')[0], 400)
        nested = '{"secret":' + json.dumps(SECRET) + ',"reason":' + "[" * 1100 + "]" * 1100 + "}"
        self.assertEqual(self.call("POST", "/webhook", nested)[0], 400)
        self.assertEqual(self.call("POST", "/webhook", "x" * 16385)[0], 413)

    def test_state_freshness_is_separate_from_alert_age_and_observes_expiry(self):
        self.store.ingest(event(), NOW)
        query = lambda: self.call("GET", "/state?symbol=BINANCE%3ABTCUSDT", headers={"X-Webhook-Secret": SECRET})[1]
        with patch("trading_assistant.bridge.app.time.time", return_value=NOW / 1000 + 600):
            self.assertFalse(query()["stale"])  # 10 minutes: old for alerts, fresh for 1H state.
        self.store.ingest(event(event_id="short-ttl", setup_id="two", sequence=2, expires_at=NOW + 60000), NOW)
        with patch("trading_assistant.bridge.app.time.time", return_value=NOW / 1000 + 61):
            self.assertTrue(query()["stale"])  # Deadline reached despite a recently updated state.
        self.store.ingest(event(event_id="active", setup_id="three", event="trigger", stage="active", sequence=3, expires_at=None), NOW)
        with patch("trading_assistant.bridge.app.time.time", return_value=NOW / 1000 + 5401):
            self.assertTrue(query()["stale"])


if __name__ == "__main__":
    unittest.main()
