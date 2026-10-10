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
        self.markups = []
        self.recipients = []

    def send(self, message, reply_markup=None, chat_id=None):
        super().send(message)
        self.markups.append(reply_markup)
        self.recipients.append(self.chat_id if chat_id is None else chat_id)

    def get_updates(self, offset):
        self.offsets.append(offset)
        return [update for update in self.updates if update["update_id"] >= offset]


def update(update_id, command="/btc", chat=123, bot=False):
    return {"update_id": update_id, "message": {"chat": {"id": chat, "type": "private"}, "from": {"is_bot": bot}, "text": command}}


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

    def test_second_profile_receives_own_replies_and_unknown_only_own_id(self):
        sender = CommandSender([update(1, "/status", chat=456), update(2, "/tf15", chat=999),
                                update(3, "/start", chat=999), update(4, "/id", chat=456),
                                update(5, "/status", chat=123), update(6, "/start", chat=777, bot=True)])
        seen = []
        CommandWorker(self.store, sender, command_handler=lambda text: seen.append(text),
                      allowed_chat_ids=[456]).poll_once(NOW)
        self.assertEqual(seen, ["/status", "/status"])
        self.assertEqual(sender.recipients, ["456", "999", "456", "123"])
        self.assertIn("Этот профиль пока не подключён", sender.messages[1])
        self.assertNotIn("TradingView", sender.messages[1])
        self.assertIsNone(sender.markups[1])
        self.assertEqual(sender.chat_id, "123")
        self.assertEqual(self.store.command_offset(), 7)
        with self.assertRaises(ValueError):
            CommandWorker(self.store, sender, allowed_chat_ids=[-123])

    def test_public_read_access_routes_replies_without_granting_mode_control(self):
        self.store.source = "exchange"
        group = update(7, "/start", chat=-999)
        group["message"]["chat"]["type"] = "group"
        sender = CommandSender([update(1, "/start", chat=456), update(2, "/status", chat=456),
                                update(3, "⏱ 15м", chat=456), update(4, "Какой риск?", chat=789),
                                update(5, "/tf15", chat=123), update(6, "/start", chat=888, bot=True), group])
        seen = []
        def handle(text):
            seen.append(text)
            return "Режим изменён"
        CommandWorker(self.store, sender, command_handler=handle, public=True).poll_once(NOW)
        self.assertEqual(seen, ["/tf15"])
        self.assertEqual(sender.recipients, ["456", "456", "456", "789", "123"])
        self.assertIn("Меню бота", sender.messages[0])
        self.assertNotIn("/tf15", sender.messages[0])
        self.assertIn("меняет владелец", sender.messages[2])
        self.assertNotIn(["⏱ 15м", "⏱ 1H"], sender.markups[0]["keyboard"])
        self.assertIn(["⏱ 15м", "⏱ 1H"], sender.markups[-1]["keyboard"])
        self.assertEqual(sender.chat_id, "123")

    def test_command_reply_destination_does_not_change_alert_recipient(self):
        sender = TelegramSender("fake-not-a-token", "123")
        with patch.object(sender, "call") as call:
            sender.send("Ответ", chat_id="456")
            self.assertEqual(call.call_args.args[1]["chat_id"], "456")
            sender.send("Сигнал")
            self.assertEqual(call.call_args.args[1]["chat_id"], "123")

    def test_no_data_and_command_help(self):
        self.assertIn("Пока нет снимков BTC", reply(self.store, "/btc", NOW))
        self.assertIn("Пока нет снимков TradingView", reply(self.store, "/status", NOW))
        self.assertIn("Команды:", reply(self.store, "что по BTC", NOW))

    def test_menu_buttons_route_through_authorized_command_handler(self):
        self.store.source = "exchange"
        sender = CommandSender([update(1, "⏱ 15м", chat=999), update(2, "⏱ 15м"),
                                update(3, "₿ BTC"), update(4, "/menu")])
        seen = []
        def handle(command):
            seen.append(command)
            return "Выбран 15м" if command == "/tf15" else None
        CommandWorker(self.store, sender, command_handler=handle).poll_once(NOW)
        self.assertEqual(seen, ["/tf15", "/btc", "/menu"])
        self.assertEqual(sender.messages[0], "Выбран 15м")
        self.assertIn("Пока нет данных BTC", sender.messages[1])
        self.assertIn("Меню бота", sender.messages[2])
        self.assertIn(["⏱ 15м", "⏱ 1H"], sender.markups[0]["keyboard"])
        self.assertTrue(sender.markups[0]["is_persistent"])

    def test_telegram_send_serializes_menu_and_preserves_plain_alerts(self):
        sender = TelegramSender("fake-not-a-token", "123")
        markup = {"keyboard": [["₿ BTC"]], "resize_keyboard": True}
        with patch.object(sender, "call") as call:
            sender.send("Меню", reply_markup=markup)
            self.assertEqual(call.call_args.args[1]["reply_markup"], markup)
            sender.send("Сигнал")
            self.assertNotIn("reply_markup", call.call_args.args[1])

    def test_market_overview_explains_filter_and_flags_stale_data(self):
        self.store.source = "exchange"
        self.store.ingest(event(event_id="python|btc", event="context", stage="idle",
                                entry=None, stop=None, target=None, rr=None, risk_pct=0,
                                expires_at=None, trend="bearish"), NOW)
        overview = reply(self.store, "🌍 Обзор рынка", NOW)
        self.assertIn("фильтр стратегии не разрешает LONG", overview)
        self.assertIn("новости и другие монеты не учитываются", overview)
        stale = reply(self.store, "/market", NOW + 5401000)
        self.assertIn("Данные устарели", stale)
        self.assertNotIn("Сейчас фильтр", stale)

    def test_assistant_explains_named_pair_and_never_uses_stale_plan(self):
        self.store.source = "exchange"
        self.store.ingest(event(event_id="python|btc"), NOW)
        self.store.ingest(event(event_id="python|eth", symbol="BYBIT:ETHUSDT", setup_id="eth|1",
                                event="context", stage="idle", entry=None, stop=None, target=None,
                                rr=None, risk_pct=0, expires_at=None, trend="bearish"), NOW)
        response = reply(self.store, "Почему нет входа по эфиру?", NOW)
        self.assertIn("BYBIT:ETHUSDT", response)
        self.assertNotIn("BTCUSDT", response)
        self.assertIn("фильтр старшего ТФ", response)
        ready = reply(self.store, "Что по BTC?", NOW)
        self.assertIn("ещё не активирован", ready)
        stale = reply(self.store, "Что по BTC?", NOW + 5401000)
        self.assertIn("Текущий вход по нему рассматривать нельзя", stale)
        self.assertNotIn("ещё не активирован", stale)

    def test_assistant_limits_and_questions_do_not_switch_timeframe(self):
        self.store.source = "exchange"
        self.assertIn("предыдущих 20", reply(self.store, "Как работает стратегия?", NOW))
        self.assertIn("0,5%", reply(self.store, "Какой риск?", NOW))
        self.assertIn("определить нельзя", reply(self.store, "BTC вырастет завтра?", NOW))
        self.assertIn("пока не распознан", reply(self.store, "Переключи таймфрейм на 15м", NOW))
        self.assertIn("Можно написать", reply(self.store, "💬 Ассистент", NOW))
        seen = []
        sender = CommandSender([update(1, "Как работает стратегия?", chat=999),
                                update(2, "Как работает стратегия?")])
        CommandWorker(self.store, sender, command_handler=lambda text: seen.append(text)).poll_once(NOW)
        self.assertEqual(seen, ["Как работает стратегия?"])
        self.assertIn("Как работает стратегия", sender.messages[0])

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
