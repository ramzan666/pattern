import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from .engine import Candle, DataGap, Engine, HOUR, Instrument, MINUTE, Settings
from .market import Bybit, MarketError
from .runtime import Runner, advance, prime, replay
from .storage import DatabaseLease
from .__main__ import credentials, main
from ..bridge.app import Store, render, validate
from ..bridge.commands import CommandWorker, reply
from ..bridge.test_commands import CommandSender, update

BASE = 1_700_000_000_000 // (4 * HOUR) * (4 * HOUR)
PAIR = Instrument("BTCUSDT", "spot", .1, .001, .001, 5)


def hour(start, open=100, high=101, low=99, close=100, duration=HOUR):
    return Candle(start, start + duration, open, high, low, close)


def prepared(settings=None, instrument=PAIR):
    engine = Engine(instrument, settings or Settings(obstacles=False))
    for k in range(250):
        close = 100 + k * .04
        engine.trend_candle(Candle(BASE + k * engine.settings.trend_ms, BASE + (k + 1) * engine.settings.trend_ms, close, close + 1, close - 1, close))
    start = engine.state["last_trend"]
    for k in range(20):
        engine.signal_candle(hour(start + k * engine.settings.bar_ms, duration=engine.settings.bar_ms), signals=False)
    return engine


def armed(settings=None):
    engine = prepared(settings)
    events = engine.signal_candle(hour(engine.state["last_bar"], 100, 103, 100, 102, duration=engine.settings.bar_ms))
    assert events[0]["event"] == "breakout"
    events = engine.signal_candle(hour(engine.state["last_bar"], 102, 102.5, 100.5, 101.5, duration=engine.settings.bar_ms))
    assert events[0]["event"] == "setup"
    engine.state["last_minute"] = engine.state["last_bar"]
    return engine


class EngineTests(unittest.TestCase):
    def test_closed_four_hour_ema_and_warmup(self):
        e = Engine(PAIR, Settings(fast=2, slow=3))
        for k, close in enumerate((100, 102)):
            e.trend_candle(Candle(BASE + k * 4 * HOUR, BASE + (k + 1) * 4 * HOUR, close, close + 1, close - 1, close))
        self.assertEqual(e.state["trend"], 0)
        e.trend_candle(Candle(BASE + 8 * HOUR, BASE + 12 * HOUR, 104, 105, 103, 104))
        self.assertEqual(e.state["trend"], 1)
        self.assertAlmostEqual(e.state["ema_fast"], 103.11111111111111)
        before = e.dump()
        e.trend_candle(Candle(BASE + 8 * HOUR, BASE + 12 * HOUR, 104, 105, 103, 104))
        self.assertEqual(e.dump(), before)
        with self.assertRaises(DataGap):
            e.trend_candle(Candle(BASE + 16 * HOUR, BASE + 20 * HOUR, 105, 106, 104, 105))

    def test_pivot_known_only_after_right_candle_and_touch_removes_it(self):
        e = Engine(PAIR, Settings(fast=2, slow=3, obstacle_strength=1))
        for k, high in enumerate((102, 105)):
            e.trend_candle(Candle(BASE + k * 4 * HOUR, BASE + (k + 1) * 4 * HOUR, 100, high, 99, 100))
        self.assertEqual(e.state["resistance"], [])
        e.trend_candle(Candle(BASE + 8 * HOUR, BASE + 12 * HOUR, 100, 103, 99, 100))
        self.assertEqual(e.state["resistance"], [105])
        e.signal_candle(hour(BASE + 12 * HOUR, 100, 105, 99, 100), signals=False)
        self.assertEqual(e.state["resistance"], [])

    def test_native_history_removes_already_swept_obstacles_before_signal_warmup(self):
        e = Engine(PAIR, Settings(fast=2, slow=3, obstacle_strength=1))
        for k, high in enumerate((102, 105, 103)):
            e.trend_candle(Candle(BASE + k * 4 * HOUR, BASE + (k + 1) * 4 * HOUR, 100, high, 99, 100))
        self.assertEqual(e.state["resistance"], [105])
        e.trend_candle(Candle(BASE + 12 * HOUR, BASE + 16 * HOUR, 100, 106, 99, 100))
        self.assertNotIn(105, e.state["resistance"])

    def test_prior_range_retest_and_frozen_two_r_plan(self):
        e = armed()
        s = e.state
        self.assertEqual(s["entry"], 102.6)
        self.assertAlmostEqual(s["target"] - s["entry"], 2 * (s["entry"] - s["stop"]))
        self.assertLessEqual(s["risk_pct"], .5)
        self.assertGreaterEqual(s["quantity"], PAIR.minimum_quantity)
        frozen = (s["entry"], s["stop"], s["target"], s["quantity"])
        e.signal_candle(hour(s["last_bar"], 101.5, 102, 101, 101.5))
        self.assertEqual(frozen, tuple(e.state[k] for k in ("entry", "stop", "target", "quantity")))

    def test_last_fifth_retest_is_valid_and_missing_retest_cancels(self):
        for touches in (True, False):
            e = prepared()
            e.signal_candle(hour(e.state["last_bar"], 100, 103, 100, 102))
            for k in range(5):
                c = hour(e.state["last_bar"], 101.5, 102, 100.5 if touches and k == 4 else 101.1, 101.5)
                events = e.signal_candle(c)
            self.assertEqual(events[0]["event"], "setup" if touches else "cancel")

    def test_entry_next_candle_duplicate_minute_and_net_fees(self):
        e = armed()
        s = e.state
        before = e.dump()
        old = Candle(s["retest_end"] - MINUTE, s["retest_end"], 102, 110, 100, 103)
        self.assertEqual(e.minute(old), [])
        self.assertEqual(before, e.dump())
        entry = s["entry"]
        c = Candle(s["retest_end"], s["retest_end"] + MINUTE, entry - .1, entry + .4, entry - .2, entry + .1)
        self.assertEqual([x["event"] for x in e.minute(c)], ["trigger"])
        self.assertEqual(e.minute(c), [])
        self.assertAlmostEqual(s["fill"], entry + .2)
        target = s["target"]
        c = Candle(c.end, c.end + MINUTE, entry + .5, target + .1, entry + .2, target)
        events = e.minute(c)
        self.assertEqual([x["event"] for x in events], ["exit"])
        trade = s["last_trade"]
        self.assertAlmostEqual(trade["net"], (trade["exit"] - trade["entry"]) * s["quantity"] - trade["fees"])
        self.assertEqual(s["wins"], 1)
        for event in events:
            validate(event, c.end + 1000)

    def test_same_minute_roundtrip_ambiguous_sl_first(self):
        e = armed()
        s = e.state
        c = Candle(s["retest_end"], s["retest_end"] + MINUTE, s["entry"] - .1,
                   s["target"] + 1, s["stop"] - 1, s["entry"])
        events = e.minute(c)
        self.assertEqual([x["event"] for x in events], ["trigger", "exit"])
        self.assertEqual(s["last_trade"]["result"], "SL")
        self.assertLess(s["last_trade"]["net"], 0)
        self.assertIn("Оба уровня", events[-1]["reason"])

    def test_fill_on_final_allowed_minute_before_expiry(self):
        e = armed()
        s = e.state
        start = s["expires_at"] - MINUTE
        c = Candle(start, start + MINUTE, s["entry"] - .1, s["entry"] + .4, s["entry"] - .2, s["entry"])
        self.assertEqual(e.execution(c)[0]["event"], "trigger")
        self.assertIsNone(s["expires_at"])
        e = armed()
        s = e.state
        c = Candle(s["expires_at"], s["expires_at"] + MINUTE, s["entry"], s["entry"] + 1, s["entry"] - .2, s["entry"])
        self.assertEqual(e.execution(c), [])

    def test_pending_deadline_trend_and_closed_hour_stop_cancellation(self):
        for reason in ("expiry", "trend", "stop"):
            e = armed()
            if reason == "trend":
                e.state["trend"] = 0
            c = hour(e.state["last_bar"], 101.5, 102, e.state["stop"] - .1 if reason == "stop" else 101, 101.5)
            events = e.signal_candle(c)
            if reason == "expiry":
                self.assertEqual(events, [])
                events = e.signal_candle(hour(e.state["last_bar"], 101.5, 102, 101, 101.5))
            self.assertEqual(events[0]["event"], "cancel")
            self.assertEqual(e.state["phase"], "canceled")

    def test_obstacle_and_exchange_minimum_skip_setup(self):
        e = prepared(Settings(obstacles=True))
        e.signal_candle(hour(e.state["last_bar"], 100, 103, 100, 102))
        e.state["resistance"] = [105]
        self.assertIn("свинг", e.signal_candle(hour(e.state["last_bar"], 102, 102.5, 100.5, 101.5))[0]["reason"])
        e = prepared(instrument=Instrument("BTCUSDT", "spot", .1, .001, .001, 1_000_000))
        e.signal_candle(hour(e.state["last_bar"], 100, 103, 100, 102))
        self.assertIn("минимума", e.signal_candle(hour(e.state["last_bar"], 102, 102.5, 100.5, 101.5))[0]["reason"])

    def test_short_is_disallowed_on_spot(self):
        with self.assertRaises(ValueError):
            Engine(PAIR, Settings(direction="both"))
        e = prepared()
        e.state["trend"] = -1
        self.assertEqual(e.signal_candle(hour(e.state["last_bar"], 100, 100, 95, 96)), [])

    def test_hour_atr_wilder_seed_and_exposure_cap(self):
        e = Engine(PAIR)
        for k in range(14):
            e.signal_candle(hour(BASE + k * HOUR), signals=False)
        self.assertEqual(e.state["atr"], 2)
        e.signal_candle(hour(BASE + 14 * HOUR, 100, 104, 100, 103), signals=False)
        self.assertAlmostEqual(e.state["atr"], 2 + 2 / 14)
        e = prepared(Settings(stop_buffer=0, risk_percent=100, obstacles=False))
        e.signal_candle(hour(e.state["last_bar"], 100, 103, 100, 102))
        e.signal_candle(hour(e.state["last_bar"], 102, 102.5, 100.5, 101.5))
        self.assertLessEqual(e.state["quantity"] * e.state["entry"], e.state["equity"] * .99)

    def test_fifteen_minute_deadlines_minute_entry_and_timeframe_messages(self):
        e = armed(Settings(timeframe_minutes=15, trend_minutes=60, obstacles=False))
        self.assertEqual(e.state["expires_at"] - e.state["retest_end"], 30 * MINUTE)
        c = Candle(e.state["retest_end"], e.state["retest_end"] + MINUTE,
                   e.state["entry"] - .1, e.state["entry"] + .4, e.state["entry"] - .2, e.state["entry"])
        event = e.minute(c)[0]
        self.assertEqual(event["timeframe"], "15")
        self.assertEqual(event["trend_timeframe"], "60")
        validate(event, c.end)
        message = render(event)
        self.assertIn("Тренд 1H", message)
        self.assertIn("сигналы 15м", message)
        self.assertIn("interval=15", message)

    def test_fifteen_minute_final_retest_and_activation_expiry(self):
        e = prepared(Settings(timeframe_minutes=15, trend_minutes=60, obstacles=False))
        events = e.signal_candle(hour(e.state["last_bar"], 100, 103, 100, 102, duration=15 * MINUTE))
        self.assertEqual(events[0]["expires_at"] - events[0]["event_time"], 75 * MINUTE)
        for k in range(5):
            events = e.signal_candle(hour(e.state["last_bar"], 101.5, 102, 100.5 if k == 4 else 101.1, 101.5, duration=15 * MINUTE))
        self.assertEqual(events[0]["event"], "setup")
        for k in range(2):
            events = e.signal_candle(hour(e.state["last_bar"], 101.5, 101.8, 101.1, 101.5, duration=15 * MINUTE))
        self.assertEqual(events[0]["event"], "cancel")


class FakeMarket:
    category = "spot"
    def __init__(self):
        self.time = BASE + 842 * HOUR + 2 * MINUTE + 5000
        self.missing_hour = False
    def now(self):
        return self.time
    def instrument(self, symbol):
        return PAIR
    def candles(self, symbol, interval, now_ms, start_ms=None, limit=1000):
        if interval == "240":
            rows = [Candle(BASE + k * 4 * HOUR, BASE + (k + 1) * 4 * HOUR, 100 + k * .04,
                           101 + k * .04, 99 + k * .04, 100 + k * .04) for k in range(250)]
        elif interval == "60":
            rows = [hour(BASE + k * HOUR) for k in range(900)]
            if self.missing_hour:
                rows = [c for c in rows if c.end != BASE + 843 * HOUR]
        elif interval == "15":
            rows = [hour(BASE + k * 15 * MINUTE, duration=15 * MINUTE) for k in range(3600)]
        else:
            first = start_ms if start_ms is not None else BASE + 842 * HOUR
            rows = [Candle(k, k + MINUTE, 100, 101, 99, 100) for k in range(first, now_ms // MINUTE * MINUTE, MINUTE)]
        selected = [c for c in rows if c.end <= now_ms and (start_ms is None or c.start >= start_ms)]
        return selected[-limit:] if start_ms is None else selected


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        self.store = Store(":memory:", source="exchange", state_max_age=180)
        self.market = FakeMarket()
        self.runner = Runner(self.store, self.market, [PAIR], Settings())
    def tearDown(self):
        self.store.db.close()

    def test_cold_start_warmup_snapshot_no_historical_trades_and_dedup(self):
        self.runner.poll()
        engine = self.runner.engines["BTCUSDT"]
        self.assertEqual(engine.state["phase"], "idle")
        self.assertEqual(engine.state["trades"], 0)
        self.assertGreaterEqual(engine.state["trend_count"], 200)
        self.assertLessEqual(engine.state["last_trend"], engine.state["last_bar"])
        self.assertEqual(self.store.health(), {})
        self.assertIn("BYBIT:BTCUSDT", reply(self.store, "/btc", self.market.now()))
        self.assertEqual(self.runner.poll(), [])

    def test_missing_boundary_hour_preserves_checkpoint_and_events(self):
        self.runner.poll()
        before = self.runner.engines["BTCUSDT"].dump()
        events = self.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        self.market.missing_hour = True
        self.market.time = BASE + 843 * HOUR + MINUTE + 5000
        with self.assertRaises(DataGap):
            self.runner.poll()
        self.assertEqual(before, self.runner.engines["BTCUSDT"].dump())
        self.assertEqual(events, self.store.db.execute("SELECT count(*) FROM events").fetchone()[0])

    def test_atomic_checkpoint_outbox_rollback(self):
        self.runner.poll()
        before = self.runner.engines["BTCUSDT"].dump()
        count = self.store.db.execute("SELECT count(*) FROM events").fetchone()[0]
        original_ingest = self.store.ingest
        def fail_after_insert(event, now):
            original_ingest(event, now)
            raise ValueError("simulated checkpoint failure")
        self.market.time += MINUTE
        with patch.object(self.store, "ingest", side_effect=fail_after_insert):
            with self.assertRaises(ValueError):
                self.runner.poll()
        self.assertEqual(count, self.store.db.execute("SELECT count(*) FROM events").fetchone()[0])
        self.assertEqual(before, self.runner.engines["BTCUSDT"].dump())
        self.assertEqual(before, self.store.db.execute("SELECT state FROM autonomous_engines").fetchone()[0])
        self.runner.poll()
        self.assertGreater(self.runner.engines["BTCUSDT"].state["last_minute"], json.loads(before)["last_minute"])

    def test_restart_restores_cursor_and_changed_config_rejected(self):
        self.runner.poll()
        before = self.runner.engines["BTCUSDT"].dump()
        restored = Runner(self.store, self.market, [PAIR], Settings())
        self.assertEqual(before, restored.engines["BTCUSDT"].dump())
        self.assertEqual(restored.poll(), [])
        with self.assertRaises(ValueError):
            Runner(self.store, self.market, [PAIR], Settings(rr=3))

    def test_bybit_commands_no_tradingview_claim_and_stale_data(self):
        self.assertNotIn("webhook", reply(self.store, "/status", self.market.now()))
        self.runner.poll()
        engine = self.runner.engines["BTCUSDT"]
        engine.state.update(phase="active", side=1, entry=100, stop=99, target=102, risk_pct=.5)
        c = Candle(engine.state["last_minute"], engine.state["last_minute"] + MINUTE, 100, 101, 99, 100)
        self.store.ingest(validate(engine.event("context", c, "Виртуальная позиция"), c.end), c.end)
        text = reply(self.store, "/btc", c.end)
        self.assertIn("Виртуальная позиция", text)
        self.assertNotIn("TradingView", text)
        self.assertIn("Bybit", reply(self.store, "/btc", c.end + 181000))

    def test_replay_and_once_never_instantiate_telegram_sender(self):
        market = self.market
        with patch("trading_assistant.autonomous.__main__.Bybit", return_value=market), \
             patch("trading_assistant.autonomous.__main__.TelegramSender", side_effect=AssertionError("no Telegram")), \
             patch("builtins.print"), patch.dict("os.environ", {"TELEGRAM_BOT_TOKEN": "unused", "TELEGRAM_CHAT_ID": "1"}):
            for flag in ("--replay", "--once"):
                with patch("sys.argv", ["bot", flag, "--symbols", "BTCUSDT"]):
                    main()

    def test_safe_timeframe_switch_preserves_balance_persists_and_warms_again(self):
        self.runner.poll()
        e = self.runner.engines["BTCUSDT"]
        e.state.update(equity=10_100, trades=2, wins=1)
        answer = self.runner.command("/tf15")
        self.assertIn("Выбран ТФ 15м", answer)
        self.assertIsNone(self.store.state("BYBIT:BTCUSDT"))
        self.assertEqual(self.runner.settings.trend_minutes, 60)
        self.runner.poll()
        e = self.runner.engines["BTCUSDT"]
        self.assertEqual(e.state["equity"], 10_100)
        self.assertEqual(e.state["trades"], 2)
        self.assertGreaterEqual(e.state["trend_count"], 200)
        saved = self.store.db.execute("SELECT value FROM metadata WHERE key='autonomous_timeframe'").fetchone()[0]
        self.assertEqual(saved, 15)
        restored = Runner(self.store, self.market, [PAIR], Settings(timeframe_minutes=15, trend_minutes=60))
        self.assertEqual(restored.poll(), [])
        self.assertIn("ТФ сигналов: 15м", reply(self.store, "/btc", self.market.now()))
        self.assertIn("Выбран ТФ 60м", self.runner.command("/tf1h"))

    def test_switch_refused_while_waiting_armed_or_active(self):
        self.runner.poll()
        for phase in ("waiting_retest", "armed", "active"):
            self.runner.engines["BTCUSDT"].state["phase"] = phase
            answer = self.runner.command("/tf15")
            self.assertIn("ТФ не изменён", answer)
            self.assertEqual(self.runner.settings.timeframe_minutes, 60)

    def test_timeframe_command_only_from_configured_chat(self):
        self.runner.poll()
        sender = CommandSender([update(1, "/tf15", chat=999), update(2, "/tf15", chat=123)])
        CommandWorker(self.store, sender, command_handler=self.runner.command).poll_once(self.market.now())
        self.assertEqual(len(sender.messages), 1)
        self.assertEqual(self.runner.settings.timeframe_minutes, 15)
        self.assertEqual(self.store.command_offset(), 3)

    def test_switch_during_download_discards_old_mode_candidate(self):
        self.runner.poll()
        self.market.time += MINUTE
        candles = self.market.candles
        switched = False
        def change_during_download(symbol, interval, now, start_ms=None, limit=1000):
            nonlocal switched
            result = candles(symbol, interval, now, start_ms, limit)
            if interval == "1" and not switched:
                switched = True
                self.runner.command("/tf15")
            return result
        with patch.object(self.market, "candles", side_effect=change_during_download):
            self.assertEqual(self.runner.poll(), [])
        self.assertEqual(self.runner.settings.timeframe_minutes, 15)
        self.assertEqual(self.runner.engines["BTCUSDT"].state["last_bar"], 0)
        self.runner.poll()
        self.assertIn("ТФ сигналов: 15м", reply(self.store, "/btc", self.market.now()))

    def test_one_broken_pair_does_not_block_the_other(self):
        eth = Instrument("ETHUSDT", "spot", .1, .001, .001, 5)
        runner = Runner(self.store, self.market, [PAIR, eth], Settings())
        runner.poll()
        btc_cursor = runner.engines["BTCUSDT"].state["last_minute"]
        self.market.time += MINUTE
        original = self.market.candles
        def broken_btc(symbol, *args, **kwargs):
            if symbol == "BTCUSDT":
                raise MarketError("unavailable")
            return original(symbol, *args, **kwargs)
        with patch.object(self.market, "candles", side_effect=broken_btc):
            changes = runner.poll()
        self.assertEqual([change["symbol"] for change in changes], ["BYBIT:ETHUSDT"])
        self.assertEqual(runner.engines["BTCUSDT"].state["last_minute"], btc_cursor)
        self.assertGreater(runner.engines["ETHUSDT"].state["last_minute"], btc_cursor)
        self.assertEqual(runner.last_errors, {"BTCUSDT": "MarketError"})

    def test_stale_runner_cannot_overwrite_newer_checkpoint_or_switch_mode(self):
        stale = Runner(self.store, self.market, [PAIR], Settings())
        self.market.time += MINUTE
        self.runner.poll()
        latest = self.runner.engines["BTCUSDT"].dump()
        with self.assertRaises(DataGap):
            stale.poll(self.market.time - MINUTE)
        self.assertEqual(latest, self.store.db.execute("SELECT state FROM autonomous_engines").fetchone()[0])
        self.assertIn("ТФ не изменён", stale.command("/tf15"))
        self.assertEqual(latest, self.store.db.execute("SELECT state FROM autonomous_engines").fetchone()[0])

    def test_missing_latest_native_trend_bar_rejects_fresh_start(self):
        original = self.market.candles
        def missing_latest(symbol, interval, now, start_ms=None, limit=1000):
            rows = original(symbol, interval, now, start_ms, limit)
            return [c for c in rows if interval != "240" or c.end != BASE + 840 * HOUR]
        with patch.object(self.market, "candles", side_effect=missing_latest):
            with self.assertRaises(DataGap):
                self.runner.poll()
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM state").fetchone()[0], 0)
        self.assertEqual(self.runner.engines["BTCUSDT"].state["last_minute"], 0)


class MarketTests(unittest.TestCase):
    def test_reverse_candles_forming_bar_exclusion_and_backward_pagination(self):
        client = Bybit()
        rows = [[str(BASE + k * MINUTE), "100", "101", "99", "100", "0", "0"] for k in range(5)]
        def response(path, params):
            selected = [x for x in rows if int(x[0]) <= params["end"] and int(x[0]) >= params["start"]]
            return {"list": list(reversed(selected[-params["limit"]:]))}
        with patch.object(client, "_get", side_effect=response) as mocked:
            candles = client.candles("BTCUSDT", "1", BASE + 4 * MINUTE + 2000, BASE, limit=2)
        self.assertEqual([c.start for c in candles], [BASE + k * MINUTE for k in range(4)])
        self.assertGreater(mocked.call_count, 1)
        self.assertTrue(all(call.args[0] == "/v5/market/kline" for call in mocked.call_args_list))

    def test_public_instrument_filter_and_rejected_api_error(self):
        client = Bybit()
        record = {"symbol": "BTCUSDT", "status": "Trading", "quoteCoin": "USDT",
                  "priceFilter": {"tickSize": "0.1"}, "lotSizeFilter": {
                      "basePrecision": ".001", "minOrderQty": ".001", "minOrderAmt": "5"}}
        with patch.object(client, "_get", return_value={"list": [record]}) as mocked:
            self.assertEqual(client.instrument("BTCUSDT"), PAIR)
        self.assertEqual(mocked.call_args.args[0], "/v5/market/instruments-info")
        with patch.object(client, "_get", return_value={"list": [["bad"]]}):
            with self.assertRaises(MarketError):
                client.candles("BTCUSDT", "1", BASE + MINUTE)

    def test_explicit_env_file_without_shell_expansion(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bot.env"
            path.write_text("TELEGRAM_BOT_TOKEN=literal-$TOKEN\nTELEGRAM_CHAT_ID=123\n")
            self.assertEqual(credentials(path), ("literal-$TOKEN", "123"))
            path.write_text("UNEXPECTED=123\n")
            with self.assertRaises(ValueError):
                credentials(path)


class StorageTests(unittest.TestCase):
    def test_second_process_refused_and_lock_released_after_exit(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "state.sqlite3")
            command = [sys.executable, "-c",
                       "import sys; from trading_assistant.autonomous.storage import DatabaseLease; "
                       "lease=DatabaseLease(sys.argv[1]); lease.__enter__(); lease.__exit__()",
                       path]
            with DatabaseLease(path):
                result = subprocess.run(command, text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("другой копией", result.stderr)
            result = subprocess.run(command, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0)
            if os.name == "posix":
                self.assertEqual(Path(path + "-lock").stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
