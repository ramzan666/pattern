"""Minute execution and selectable signal candles, with a durable outbox."""
from dataclasses import replace
import sqlite3
import threading
from .engine import Candle, DataGap, Engine, MINUTE
from .market import MarketError
from ..bridge.app import validate


def prime(engine, hours, four_hours):
    """Indicators only. A fresh installation never announces historical entries."""
    if not hours or len(four_hours) < engine.settings.slow:
        raise DataGap("Not enough closed history to warm up indicators")
    four = iter(four_hours)
    pending = next(four, None)
    for candle in hours:
        while pending is not None and pending.end <= candle.end:
            engine.trend_candle(pending)
            pending = next(four, None)
        engine.signal_candle(candle, signals=False)
    if engine.state["trend_count"] < engine.settings.slow or engine.state["atr"] is None:
        raise DataGap("Closed trend/ATR warmup incomplete")
    if engine.state["last_trend"] != engine.state["last_bar"] // engine.settings.trend_ms * engine.settings.trend_ms:
        raise DataGap("Latest closed trend candle missing during warmup")
    engine.state["last_minute"] = engine.state["last_bar"]
    return engine


def advance(engine, minutes, hours, four_hours):
    """All three feeds must reach the same closed-minute horizon."""
    events = []
    hourly = {c.end: c for c in hours}
    four = {c.end: c for c in four_hours}
    for candle in minutes:
        if candle.end <= engine.state["last_minute"]:
            continue
        if candle.end % engine.settings.bar_ms == 0 and candle.end not in hourly:
            raise DataGap("Signal candle not yet available; retry before committing")
        if candle.end % engine.settings.trend_ms == 0 and candle.end not in four:
            raise DataGap("Trend candle not yet available; retry before committing")
        events.extend(engine.minute(candle))
        if candle.end % engine.settings.trend_ms == 0:
            engine.trend_candle(four[candle.end])
        if candle.end % engine.settings.bar_ms == 0:
            events.extend(engine.signal_candle(hourly[candle.end]))
    return events


class Runner:
    def __init__(self, store, client, instruments, settings):
        self.store, self.client, self.settings = store, client, settings
        self.lock = threading.RLock()
        self.engines = {}
        self.checkpoints = {}
        self.last_errors = {}
        with store.transaction():
            if store.db.execute("SELECT 1 FROM events WHERE event_id NOT LIKE 'python|%' LIMIT 1").fetchone():
                raise ValueError("This database contains TradingView events; use a separate autonomous database")
            store.db.execute("CREATE TABLE IF NOT EXISTS autonomous_engines "
                             "(symbol TEXT PRIMARY KEY, config_id TEXT NOT NULL, state TEXT NOT NULL)")
            existing = {row[0] for row in store.db.execute("SELECT symbol FROM autonomous_engines")}
            requested = {i.chart_symbol for i in instruments}
            if existing - requested:
                raise ValueError("Database contains other instruments; use the original symbols or a separate database")
            selected = store.db.execute("SELECT value FROM metadata WHERE key='autonomous_timeframe'").fetchone()
            if selected and selected[0] != settings.timeframe_minutes:
                raise ValueError("Saved timeframe differs: use /tf15 or /tf1h, or a separate database")
            store.db.execute("INSERT OR IGNORE INTO metadata VALUES ('autonomous_timeframe',?)",
                             (settings.timeframe_minutes,))
        for instrument in instruments:
            engine = Engine(instrument, settings)
            with store.lock:
                saved = store.db.execute("SELECT config_id,state FROM autonomous_engines WHERE symbol=?",
                                         (instrument.chart_symbol,)).fetchone()
            if saved:
                if saved["config_id"] != engine.config_id:
                    raise ValueError("Trading settings/market changed: use a separate database; do not reinterpret saved positions")
                engine.restore(saved["state"])
            self.engines[instrument.symbol] = engine
            self.checkpoints[instrument.symbol] = saved["state"] if saved else None

    def command(self, message):
        command = message.strip().split(maxsplit=1)[0].lower().split("@")[0] if message.strip() else ""
        if command not in ("/tf15", "/tf1h"):
            return None
        timeframe = 15 if command == "/tf15" else 60
        with self.lock:
            if timeframe == self.settings.timeframe_minutes:
                return f"Уже выбран ТФ {timeframe}м, тренд {self.settings.trend_minutes}м."
            if any(e.state["phase"] in ("waiting_retest", "armed", "active") for e in self.engines.values()):
                return "ТФ не изменён: есть ожидающий сценарий или виртуальная позиция. Дождитесь отмены/завершения и повторите команду."
            settings = replace(self.settings, timeframe_minutes=timeframe, trend_minutes=4 * timeframe)
            engines = {}
            with self.store.transaction():
                for symbol, old in self.engines.items():
                    row = self.store.db.execute("SELECT state FROM autonomous_engines WHERE symbol=?",
                                                (old.instrument.chart_symbol,)).fetchone()
                    if (row["state"] if row else None) != self.checkpoints[symbol]:
                        return "ТФ не изменён: состояние базы обновлено другой копией. Перезапустите бота после её остановки."
                for symbol, old in self.engines.items():
                    new = Engine(old.instrument, settings)
                    for key in ("equity", "trades", "wins", "gross_profit", "gross_loss", "last_trade"):
                        new.state[key] = old.state[key]
                    # Event sequencing continues even while indicators are warmed again.
                    new.state["sequence"] = old.state["sequence"]
                    self.store.db.execute("INSERT OR REPLACE INTO autonomous_engines VALUES (?,?,?)",
                                          (new.instrument.chart_symbol, new.config_id, new.dump()))
                    stream = "trend_retest_v1|" + new.instrument.chart_symbol
                    self.store.db.execute("UPDATE outbox SET status='suppressed',error='timeframe_changed' "
                                          "WHERE status='pending' AND event_id IN "
                                          "(SELECT event_id FROM events WHERE stream=? AND event IN ('context','breakout','setup','trigger'))",
                                          (stream,))
                    self.store.db.execute("DELETE FROM state WHERE stream=?", (stream,))
                    engines[symbol] = new
                self.store.db.execute("UPDATE metadata SET value=? WHERE key='autonomous_timeframe'", (timeframe,))
            self.settings, self.engines = settings, engines
            self.checkpoints = {symbol: engine.dump() for symbol, engine in engines.items()}
            return f"Выбран ТФ {timeframe}м, тренд {4 * timeframe}м. Индикаторы обновятся на следующей проверке; новые сигналы — после закрытия новой свечи. Режим сохранён."

    def commit(self, engine, events, now_ms):
        # The outbox, latest state and cursor are committed or rolled back together.
        with self.store.transaction():
            row = self.store.db.execute("SELECT state FROM autonomous_engines WHERE symbol=?",
                                        (engine.instrument.chart_symbol,)).fetchone()
            current = row["state"] if row else None
            if current != self.checkpoints[engine.instrument.symbol]:
                raise DataGap("Checkpoint changed by another runner; refusing to overwrite it")
            for event in events:
                self.store.ingest(validate(event, now_ms), now_ms)
            self.store.db.execute("INSERT OR REPLACE INTO autonomous_engines VALUES (?,?,?)",
                                  (engine.instrument.chart_symbol, engine.config_id, engine.dump()))

    def poll(self, now_ms=None):
        now_ms = self.client.now() if now_ms is None else now_ms
        horizon = now_ms - 2000  # Allow the source to publish a just-closed candle.
        target_minute = horizon // MINUTE * MINUTE
        changed = []
        self.last_errors = {}
        with self.lock:
            items, settings = list(self.engines.items()), self.settings
        for symbol, original in items:
            if original.state["last_minute"] >= target_minute:
                continue
            try:
                summary = self.poll_symbol(symbol, original, settings, horizon, target_minute, now_ms)
                if summary is not None:
                    changed.append(summary)
            except (MarketError, DataGap, ValueError, OSError, sqlite3.Error) as exc:
                # A broken BTC feed must not prevent independent ETH updates.
                # Never expose raw network/credential errors in a Telegram reply.
                self.last_errors[symbol] = type(exc).__name__
        if self.last_errors and not changed:
            raise DataGap("Public candles/checkpoint unavailable for: " + ", ".join(self.last_errors))
        return changed

    def poll_symbol(self, symbol, original, settings, horizon, target_minute, now_ms):
        engine = Engine(original.instrument, settings).restore(original.dump())
        if not engine.state["last_bar"]:
            hours = self.client.candles(symbol, str(settings.timeframe_minutes), horizon)
            four = self.client.candles(symbol, str(settings.trend_minutes), horizon)
            prime(engine, hours, four)
        hours = self.client.candles(symbol, str(settings.timeframe_minutes), horizon, engine.state["last_bar"])
        four = self.client.candles(symbol, str(settings.trend_minutes), horizon, engine.state["last_trend"])
        minutes = self.client.candles(symbol, "1", horizon, engine.state["last_minute"])
        if minutes and minutes[-1].end != target_minute:
            raise DataGap("Latest closed 1m candle unavailable; no signals committed")
        if not minutes and engine.state["last_minute"] != target_minute:
            raise DataGap("Latest closed 1m candle unavailable; no signals committed")
        events = advance(engine, minutes, hours, four)
        if engine.state["last_minute"] != target_minute:
            raise DataGap("Minute cursor did not reach the source time")
        if engine.state["last_trend"] != target_minute // settings.trend_ms * settings.trend_ms:
            raise DataGap("Latest closed trend candle unavailable")
        if engine.state["last_bar"] != target_minute // settings.bar_ms * settings.bar_ms:
            raise DataGap("Latest closed signal candle unavailable")
        snapshot = minutes[-1] if minutes else Candle(**engine.state["bars"][-1])
        events.append(engine.event("context", snapshot, engine.state["reason"]))
        with self.lock:
            if self.engines.get(symbol) is not original:
                return None  # A Telegram timeframe switch invalidated this download.
            self.commit(engine, events, now_ms)
            self.engines[symbol] = engine
            self.checkpoints[symbol] = engine.dump()
            return engine.summary()


def replay(instrument, settings, hours, four_hours):
    """Finite historical OHLC simulation. Does not use Telegram or SQLite."""
    engine = Engine(instrument, settings)
    four = iter(four_hours)
    pending = next(four, None)
    for candle in hours:
        events = engine.execution(candle)
        while pending is not None and pending.end <= candle.end:
            engine.trend_candle(pending)
            pending = next(four, None)
        events.extend(engine.signal_candle(candle))
    if not hours or engine.state["trend_count"] < settings.slow:
        raise DataGap("Not enough historical data")
    summary = engine.summary()
    summary.update(start=hours[0].start, end=hours[-1].end,
                   execution_model=f"{settings.timeframe_minutes}m OHLC; ambiguous SL first; fees/slippage included",
                   open_position_not_marked_to_market=engine.state["phase"] == "active")
    return summary
