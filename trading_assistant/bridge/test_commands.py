import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from unittest.mock import patch

from .app import DeliveryError, Store, TelegramSender
from .commands import CommandWorker, date_text, reply
from .test_bridge import NOW, event, FakeSender


class CommandSender(FakeSender):
    chat_id = "123"

    def __init__(self, updates, errors=()):
        super().__init__(errors)
        self.updates, self.offsets = updates, []

    def get_updates(self, offset):
        self.offsets.append(offset)
        return [update for update in self.updates if update["update_id"] >= offset]


def update(update_id, command="/btc", chat=123, bot=False):
    return {"update_id": update_id, "message": {"chat": {"id": chat}, "from": {"is_bot": bot}, "text": command}}


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:")

    def tearDown(self):
        self.store.db.close()

    def test_unknown_chat_and_bot_are_skipped_without_reply(self):
        sender = CommandSender([update(10, chat=999), update(11, bot=True),
                                {"update_id": 12, "message": {"chat": [], "from": []}}])
        CommandWorker(self.store, sender).poll_once(NOW)
        self.assertEqual(sender.messages, [])
        self.assertEqual(self.store.command_offset(), 13)

    def test_no_data_and_command_help(self):
        self.assertIn("Пока нет снимков BTC", reply(self.store, "/btc", NOW))
        self.assertIn("Пока нет снимков TradingView", reply(self.store, "/status", NOW))
        self.assertIn("Команды:", reply(self.store, "что по BTC", NOW))

    def test_latest_btc_and_status_come_only_from_stored_states(self):
        self.store.ingest(event(symbol="BYBIT:BTCUSDT.P", event_id="old", event_time=NOW - 1000), NOW)
        self.store.ingest(event(event_id="current", entry=200, stop=199, target=202), NOW)
        self.store.ingest(event(symbol="BINANCE:BTCUPUSDT", event_id="leveraged", event_time=NOW + 1), NOW)
        self.store.ingest(event(symbol="BINANCE:ETHUSDT", event_id="eth", setup_id="ETH|1"), NOW)
        result = reply(self.store, "/btc@my_test_bot", NOW)
        self.assertIn("BINANCE:BTCUSDT", result)
        self.assertIn("вход 200", result)
        self.assertNotIn("BTCUP", result)
        self.assertIn("МСК", result)
        self.assertIn("ETHUSDT", reply(self.store, "/eth", NOW))
        status = reply(self.store, "/status", NOW)
        self.assertIn("BINANCE:ETHUSDT", status)
        self.assertIn("BYBIT:BTCUSDT.P", status)

    def test_stale_expiry_and_active_execution_wording(self):
        self.store.ingest(event(expires_at=NOW + 1000), NOW)
        self.assertIn("Срок ожидания истёк", reply(self.store, "/btc", NOW + 2000))
        self.assertIn("Срок ожидания: " + date_text(NOW + 1000), reply(self.store, "/btc", NOW))
        self.store.ingest(event(event_id="active", event="trigger", stage="active", sequence=2, expires_at=None), NOW)
        self.assertIn("Биржевой позиции бот не видит", reply(self.store, "/btc", NOW))
        self.assertIn("Данные устарели", reply(self.store, "/btc", NOW + 5401000))
        self.assertEqual(date_text(0), "01.01.1970 03:00 МСК")

    def test_offset_survives_restart_and_failed_reply_is_retried(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            store = Store(path)
            sender = CommandSender([update(50)], errors=[DeliveryError()])
            worker = CommandWorker(store, sender)
            with self.assertRaises(DeliveryError):
                worker.poll_once(NOW)
            self.assertEqual(store.command_offset(), 0)
            worker.poll_once(NOW)
            self.assertEqual(store.command_offset(), 51)
            store.db.close()
            store = Store(path)
            CommandWorker(store, sender).poll_once(NOW)
            self.assertEqual(sender.offsets, [0, 0, 51])
            self.assertEqual(len(sender.messages), 1)
            store.db.close()

    def test_get_updates_uses_bounded_long_poll_and_fixed_api_method(self):
        response = BytesIO(b'{"ok":true,"result":[]}')
        with patch("trading_assistant.bridge.app.request.urlopen", return_value=response) as mocked:
            self.assertEqual(TelegramSender("fake-not-a-token", "123").get_updates(51), [])
        req = mocked.call_args.args[0]
        self.assertTrue(req.full_url.endswith("/getUpdates"))
        self.assertEqual(json.loads(req.data)["offset"], 51)
        self.assertEqual(json.loads(req.data)["allowed_updates"], ["message"])
        self.assertEqual(mocked.call_args.kwargs["timeout"], 15)

    def test_permanent_reply_error_consumes_update_and_later_commands_continue(self):
        sender = CommandSender([update(80), update(81, "/eth")], errors=[DeliveryError(retryable=False)])
        CommandWorker(self.store, sender).poll_once(NOW)
        self.assertEqual(self.store.command_offset(), 82)
        self.assertEqual(len(sender.messages), 1)

    def test_large_valid_updates_response_and_bounded_status(self):
        updates = [update(i, "🙂" * 4096, chat=999) for i in range(10)]
        encoded = json.dumps({"ok": True, "result": updates}).encode()
        self.assertGreater(len(encoded), 262144)
        with patch("trading_assistant.bridge.app.request.urlopen", return_value=BytesIO(encoded)):
            self.assertEqual(len(TelegramSender("fake-not-a-token", "123").get_updates(0)), 10)
        for i in range(10):
            self.store.ingest(event(symbol="EXCHANGE:" + "A" * 400 + str(i), event_id=str(i)), NOW)
        self.assertLess(len(reply(self.store, "/status", NOW)), 4096)


if __name__ == "__main__":
    unittest.main()
