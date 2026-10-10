"""Closed-candle rules. Paper fills are a model, not exchange orders."""
from dataclasses import asdict, dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
import hashlib
import json
import math

MINUTE = 60_000
HOUR = 60 * MINUTE


class DataGap(ValueError):
    """Do not advance a trading state across missing candles."""


@dataclass(frozen=True)
class Candle:
    start: int
    end: int
    open: float
    high: float
    low: float
    close: float

    def __post_init__(self):
        if type(self.start) is not int or type(self.end) is not int or self.start < 0 or self.end <= self.start:
            raise ValueError("Invalid candle timestamps")
        if any(not math.isfinite(p) or p <= 0 for p in (self.open, self.high, self.low, self.close)):
            raise ValueError("Invalid candle prices")
        if self.low > min(self.open, self.close) or self.high < max(self.open, self.close) or self.low > self.high:
            raise ValueError("Invalid OHLC")


@dataclass(frozen=True)
class Instrument:
    symbol: str
    category: str
    tick: float
    step: float
    minimum_quantity: float
    minimum_notional: float = 0

    def __post_init__(self):
        if self.category not in ("linear", "spot") or not self.symbol.endswith("USDT") or not self.symbol.isalnum():
            raise ValueError("Only Bybit linear USDT and spot USDT pairs are supported")
        if any(not math.isfinite(p) or p <= 0 for p in (self.tick, self.step, self.minimum_quantity)):
            raise ValueError("Invalid instrument precision")
        if not math.isfinite(self.minimum_notional) or self.minimum_notional < 0:
            raise ValueError("Invalid minimum notional")

    @property
    def chart_symbol(self):
        return "BYBIT:" + self.symbol + (".P" if self.category == "linear" else "")


@dataclass(frozen=True)
class Settings:
    timeframe_minutes: int = 60
    trend_minutes: int = 240
    fast: int = 50
    slow: int = 200
    breakout: int = 20
    retest: int = 5
    activation: int = 2
    rr: float = 2
    stop_buffer: float = .25
    obstacle_strength: int = 3
    obstacles: bool = True
    risk_percent: float = .5
    exposure_percent: float = 100
    initial_equity: float = 10_000
    commission: float = .0005
    slippage_ticks: int = 2
    direction: str = "long"

    def __post_init__(self):
        if self.timeframe_minutes not in (15, 60) or self.trend_minutes not in (60, 240) or self.trend_minutes <= self.timeframe_minutes:
            raise ValueError("Use 15m with 1H/4H trend, or 1H with 4H trend")
        for name in ("fast", "slow", "breakout", "retest", "activation", "obstacle_strength"):
            if type(getattr(self, name)) is not int or getattr(self, name) < 1:
                raise ValueError("Invalid " + name)
        if self.fast >= self.slow or self.direction not in ("both", "long", "short"):
            raise ValueError("Invalid EMA periods/direction")
        for name in ("rr", "risk_percent", "exposure_percent", "initial_equity"):
            if not math.isfinite(getattr(self, name)) or getattr(self, name) <= 0:
                raise ValueError("Invalid " + name)
        if self.rr < 1 or self.risk_percent > 100 or self.exposure_percent > 100:
            raise ValueError("Invalid risk/reward/exposure")
        if not math.isfinite(self.stop_buffer) or self.stop_buffer < 0:
            raise ValueError("Invalid stop buffer")
        if not math.isfinite(self.commission) or not 0 <= self.commission < 1:
            raise ValueError("Invalid commission")
        if type(self.slippage_ticks) is not int or self.slippage_ticks < 0:
            raise ValueError("Invalid slippage")

    @property
    def bar_ms(self):
        return self.timeframe_minutes * MINUTE

    @property
    def trend_ms(self):
        return self.trend_minutes * MINUTE


class Engine:
    def __init__(self, instrument, settings=Settings()):
        self.instrument, self.settings = instrument, settings
        if instrument.category == "spot" and settings.direction != "long":
            raise ValueError("Spot mode requires direction=long")
        self.config_id = hashlib.sha256(json.dumps({
            "version": 1, "instrument": asdict(instrument), "settings": asdict(settings),
        }, sort_keys=True).encode()).hexdigest()[:16]
        self.state = {
            "phase": "idle", "side": 0, "setup_id": "", "sequence": 0,
            "last_bar": 0, "last_trend": 0, "last_minute": 0, "ended_bar": 0,
            "trend_count": 0, "ema_fast": None, "ema_slow": None, "trend_window": [],
            "trend": 0, "resistance": [], "support": [], "bars": [],
            "atr_count": 0, "atr_sum": 0., "atr": None, "previous_close": None,
            "breakout_end": None, "breakout_level": None, "retest_end": None,
            "expires_at": None, "entry": None, "stop": None, "target": None,
            "quantity": None, "risk_pct": 0., "fill": None,
            "equity": settings.initial_equity, "trades": 0, "wins": 0,
            "gross_profit": 0., "gross_loss": 0., "last_trade": None,
            "reason": f"Ждём закрытый пробой {settings.timeframe_minutes}м",
        }

    def dump(self):
        return json.dumps(self.state, sort_keys=True, allow_nan=False)

    def restore(self, encoded):
        state = json.loads(encoded)
        if state.keys() != self.state.keys():
            raise ValueError("Unsupported checkpoint version")
        self.state = state
        return self

    def trend_candle(self, candle):
        s, p = self.state, self.settings
        if candle.end - candle.start != p.trend_ms:
            raise ValueError("Expected trend timeframe candle")
        if candle.end <= s["last_trend"]:
            return
        if s["last_trend"] and candle.start != s["last_trend"]:
            raise DataGap("Missing trend candle")
        # Native history may precede our signal-candle warmup window. Remove
        # levels swept in that older history too, using only this closed candle.
        s["resistance"] = [level for level in s["resistance"] if candle.high < level]
        s["support"] = [level for level in s["support"] if candle.low > level]
        previous_fast = s["ema_fast"]
        for key, length in (("ema_fast", p.fast), ("ema_slow", p.slow)):
            s[key] = candle.close if s[key] is None else s[key] + 2 / (length + 1) * (candle.close - s[key])
        s["trend_count"] += 1
        s["trend"] = 0
        if s["trend_count"] >= p.slow and previous_fast is not None:
            if s["ema_fast"] > s["ema_slow"] and s["ema_fast"] > previous_fast and candle.close > s["ema_fast"]:
                s["trend"] = 1
            elif s["ema_fast"] < s["ema_slow"] and s["ema_fast"] < previous_fast and candle.close < s["ema_fast"]:
                s["trend"] = -1
        window = s["trend_window"]
        window.append(asdict(candle))
        del window[:max(0, len(window) - (2 * p.obstacle_strength + 1))]
        if len(window) == 2 * p.obstacle_strength + 1:
            k = p.obstacle_strength
            candidate = window[k]
            if all(candidate["high"] >= x["high"] for x in window[:k]) and all(candidate["high"] > x["high"] for x in window[k + 1:]):
                s["resistance"].append(candidate["high"])
            if all(candidate["low"] <= x["low"] for x in window[:k]) and all(candidate["low"] < x["low"] for x in window[k + 1:]):
                s["support"].append(candidate["low"])
            s["resistance"] = s["resistance"][-100:]
            s["support"] = s["support"][-100:]
        s["last_trend"] = candle.end

    def event(self, kind, candle, reason):
        s, p = self.state, self.settings
        s["sequence"] += 1
        s["reason"] = reason
        return {
            "schema_version": 1, "strategy": "trend_retest_v1",
            "event_id": f"python|{self.config_id}|{self.instrument.chart_symbol}|{s['sequence']}|{kind}|{candle.end}",
            "setup_id": s["setup_id"] or f"python|{self.config_id}|{self.instrument.chart_symbol}|idle",
            "sequence": s["sequence"], "event": kind, "stage": s["phase"],
            "symbol": self.instrument.chart_symbol, "timeframe": str(p.timeframe_minutes),
            "trend_timeframe": str(p.trend_minutes),
            "direction": {0: "none", 1: "long", -1: "short"}[s["side"]],
            "trend": {0: "neutral", 1: "bullish", -1: "bearish"}[s["trend"]],
            "event_time": candle.end, "bar_time": candle.start // p.bar_ms * p.bar_ms,
            "entry": s["entry"], "stop": s["stop"], "target": s["target"],
            "rr": p.rr if s["entry"] is not None else None,
            "risk_pct": s["risk_pct"], "expires_at": s["expires_at"], "reason": reason,
        }

    def cancel(self, candle, reason):
        self.state.update(phase="canceled", expires_at=None, ended_bar=((candle.end - 1) // self.settings.bar_ms + 1) * self.settings.bar_ms)
        return self.event("cancel", candle, reason)

    def price(self, value, up):
        tick = Decimal(str(self.instrument.tick))
        return float((Decimal(str(value)) / tick).to_integral_value(rounding=ROUND_CEILING if up else ROUND_FLOOR) * tick)

    def plan(self, candle):
        s, p, i = self.state, self.settings, self.instrument
        long = s["side"] == 1
        tick = Decimal(str(i.tick))
        entry = float(Decimal(str(candle.high if long else candle.low)) + (tick if long else -tick))
        stop = self.price((candle.low if long else candle.high) + (-1 if long else 1) * p.stop_buffer * s["atr"], not long)
        risk = float(abs(Decimal(str(entry)) - Decimal(str(stop))))
        target = self.price(Decimal(str(entry)) + (1 if long else -1) * Decimal(str(risk)) * Decimal(str(p.rr)), long)
        if min(entry, stop, target) <= 0 or risk < i.tick:
            return False, "Некорректные уровни входа/SL/TP"
        quantity = min(s["equity"] * p.risk_percent / 100 / risk,
                       s["equity"] * p.exposure_percent / 100 * .99 / entry)
        step = Decimal(str(i.step))
        quantity = float((Decimal(str(max(0., quantity))) / step).to_integral_value(rounding=ROUND_FLOOR) * step)
        if quantity < i.minimum_quantity or quantity * entry < i.minimum_notional:
            return False, "Размер виртуальной позиции меньше минимума Bybit"
        obstacles = s["resistance"] if long else s["support"]
        if p.obstacles and any(entry < level < target if long else target < level < entry for level in obstacles):
            return False, "Подтверждённый свинг старшего ТФ находится до планового TP"
        s.update(entry=entry, stop=stop, target=target, quantity=quantity,
                 risk_pct=quantity * risk / s["equity"] * 100,
                 retest_end=candle.end, expires_at=candle.end + p.activation * p.bar_ms, phase="armed")
        return True, f"Ретест подтверждён. Вход со следующей свечи {p.timeframe_minutes}м; ожидание {p.activation} свечей."

    def signal_candle(self, candle, signals=True):
        s, p = self.state, self.settings
        if candle.end - candle.start != p.bar_ms:
            raise ValueError("Expected signal timeframe candle")
        if candle.end <= s["last_bar"]:
            return []
        if s["last_bar"] and candle.start != s["last_bar"]:
            raise DataGap("Missing signal candle")
        previous = s["previous_close"]
        tr = candle.high - candle.low if previous is None else max(candle.high - candle.low, abs(candle.high - previous), abs(candle.low - previous))
        s["atr_count"] += 1
        if s["atr"] is None:
            s["atr_sum"] += tr
            if s["atr_count"] == 14:
                s["atr"] = s["atr_sum"] / 14
        else:
            s["atr"] += (tr - s["atr"]) / 14
        s["previous_close"] = candle.close
        s["resistance"] = [level for level in s["resistance"] if candle.high < level]
        s["support"] = [level for level in s["support"] if candle.low > level]
        events = []
        if signals:
            if s["phase"] == "armed" and (candle.low <= s["stop"] if s["side"] == 1 else candle.high >= s["stop"]):
                events.append(self.cancel(candle, "До активации нарушен SL на закрытой сигнальной свече"))
            if s["phase"] in ("waiting_retest", "armed") and s["trend"] != s["side"]:
                events.append(self.cancel(candle, "Закрытый тренд старшего ТФ больше не подтверждает направление"))
            if s["phase"] == "waiting_retest":
                age = (candle.end - s["breakout_end"]) // p.bar_ms
                touched = candle.low <= s["breakout_level"] and candle.close > s["breakout_level"] if s["side"] == 1 else candle.high >= s["breakout_level"] and candle.close < s["breakout_level"]
                if 1 <= age <= p.retest and touched and s["atr"] is not None:
                    valid, reason = self.plan(candle)
                    events.append(self.event("setup", candle, reason) if valid else self.cancel(candle, reason))
                elif age >= p.retest:
                    events.append(self.cancel(candle, f"Ретест не подтвердился за {p.retest} свечей"))
            if s["phase"] == "armed" and candle.end >= s["expires_at"]:
                events.append(self.cancel(candle, f"Вход не активировался за {p.activation} свечей после ретеста"))
            if len(s["bars"]) >= p.breakout and s["phase"] in ("idle", "canceled", "closed") and candle.end > s["ended_bar"]:
                upper = max(x["high"] for x in s["bars"][-p.breakout:])
                lower = min(x["low"] for x in s["bars"][-p.breakout:])
                side = 1 if s["trend"] == 1 and p.direction != "short" and candle.close > upper else -1 if s["trend"] == -1 and p.direction != "long" and candle.close < lower else 0
                if side:
                    s.update(phase="waiting_retest", side=side,
                             setup_id=f"python|{self.config_id}|{self.instrument.chart_symbol}|{candle.end}",
                             breakout_end=candle.end, breakout_level=upper if side == 1 else lower,
                             entry=None, stop=None, target=None, quantity=None, fill=None, risk_pct=0.,
                             expires_at=candle.end + p.retest * p.bar_ms)
                    events.append(self.event("breakout", candle, f"Пробой {p.timeframe_minutes}м подтверждён. Ждём ретест уровня"))
        s["bars"].append(asdict(candle))
        s["bars"] = s["bars"][-p.breakout:]
        s["last_bar"] = candle.end
        return events

    def execution(self, candle):
        """SL first when both extremes could have executed after entry."""
        s, p = self.state, self.settings
        events = []
        entered_at_open = False
        if s["phase"] == "armed" and candle.start >= s["retest_end"] and candle.start < s["expires_at"]:
            long = s["side"] == 1
            if candle.high >= s["entry"] if long else candle.low <= s["entry"]:
                entered_at_open = candle.open >= s["entry"] if long else candle.open <= s["entry"]
                fill = max(s["entry"], candle.open) if long else min(s["entry"], candle.open)
                fill += s["side"] * p.slippage_ticks * self.instrument.tick
                if fill <= 0:
                    return [self.cancel(candle, "Некорректная цена виртуального исполнения")]
                s.update(phase="active", expires_at=None, fill=fill)
                events.append(self.event("trigger", candle, f"Виртуальный вход бота: {fill:.10g}. Это не ордер на бирже"))
        if s["phase"] != "active":
            return events
        long = s["side"] == 1
        at_open = not events or entered_at_open
        open_sl = at_open and (candle.open <= s["stop"] if long else candle.open >= s["stop"])
        open_tp = at_open and (candle.open >= s["target"] if long else candle.open <= s["target"])
        hit_sl = candle.low <= s["stop"] if long else candle.high >= s["stop"]
        hit_tp = candle.high >= s["target"] if long else candle.low <= s["target"]
        if not (open_sl or open_tp or hit_sl or hit_tp):
            return events
        stopped = open_sl or (not open_tp and hit_sl)
        if stopped:
            exit_price = min(s["stop"], candle.open) if open_sl and long else max(s["stop"], candle.open) if open_sl else s["stop"]
            exit_price -= s["side"] * p.slippage_ticks * self.instrument.tick
        else:
            exit_price = max(s["target"], candle.open) if open_tp and long else min(s["target"], candle.open) if open_tp else s["target"]
        exit_price = max(self.instrument.tick, exit_price)
        gross = s["side"] * (exit_price - s["fill"]) * s["quantity"]
        fees = (s["fill"] + exit_price) * s["quantity"] * p.commission
        net = gross - fees
        s["equity"] += net
        s["trades"] += 1
        s["wins"] += int(net > 0)
        s["gross_profit"] += max(net, 0)
        s["gross_loss"] += max(-net, 0)
        s["last_trade"] = {"entry": s["fill"], "exit": exit_price, "net": net, "fees": fees, "result": "SL" if stopped else "TP", "time": candle.end}
        s.update(phase="closed", expires_at=None, ended_bar=((candle.end - 1) // p.bar_ms + 1) * p.bar_ms)
        ambiguity = " Оба уровня внутри свечи: выбран SL." if hit_sl and hit_tp and not open_tp else ""
        events.append(self.event("exit", candle, f"Виртуальная сделка: {'SL' if stopped else 'TP'}, итог {net:.2f} USDT после издержек.{ambiguity}"))
        return events

    def minute(self, candle):
        if candle.end - candle.start != MINUTE:
            raise ValueError("Expected 1m candle")
        if candle.end <= self.state["last_minute"]:
            return []
        if self.state["last_minute"] and candle.start != self.state["last_minute"]:
            raise DataGap("Missing 1m candle")
        events = self.execution(candle)
        self.state["last_minute"] = candle.end
        return events

    def summary(self):
        s = self.state
        return {"symbol": self.instrument.chart_symbol, "timeframe_minutes": self.settings.timeframe_minutes,
                "trend_minutes": self.settings.trend_minutes, "stage": s["phase"], "trend": s["trend"],
                "entry": s["entry"], "stop": s["stop"], "target": s["target"],
                "paper_equity": s["equity"], "closed_trades": s["trades"],
                "win_rate": s["wins"] / s["trades"] if s["trades"] else None,
                "net_pnl": s["equity"] - self.settings.initial_equity}
