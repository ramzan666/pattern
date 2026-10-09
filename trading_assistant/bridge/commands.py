"""Read-only Telegram assistant commands using stored Pine snapshots."""
from datetime import datetime
import html
import re
import threading
import time
from zoneinfo import ZoneInfo

from .app import DeliveryError, snapshot_freshness

HELP = "Команды: /btc — сценарий BTC, /eth — сценарий ETH, /status — все наблюдаемые инструменты."
STAGES = {"idle": "Ждём пробой 1H", "waiting_retest": "Ждём ретест пробоя",
          "armed": "Вход подготовлен — ждём активацию", "active": "Позиция активна в модели TradingView",
          "canceled": "Сценарий отменён", "closed": "Сценарий завершён в модели TradingView"}
TRENDS = {"bullish": "бычий", "bearish": "медвежий", "neutral": "нейтральный"}
MSK = ZoneInfo("Europe/Moscow")


def date_text(milliseconds):
    return datetime.fromtimestamp(milliseconds / 1000, MSK).strftime("%d.%m.%Y %H:%M МСК")


def describe(data, now_ms, max_age, compact=False):
    _, stale = snapshot_freshness(data, now_ms, max_age)
    text = [f"<b>{html.escape(data['symbol'])}</b>", f"Тренд 4H: {TRENDS[data['trend']]}", STAGES[data["stage"]]]
    if stale:
        expired = data["expires_at"] is not None and data["stage"] in ("waiting_retest", "armed") and now_ms >= data["expires_at"]
        text.append("⚠️ Срок ожидания истёк; вход не рассматриваем." if expired else "⚠️ Данные устарели; нужен свежий снимок TradingView.")
    if not compact:
        if data["stage"] in ("armed", "active"):
            number = lambda value: format(value, ".10g")
            text.extend([f"{data['direction'].upper()} · вход {number(data['entry'])}",
                         f"SL {number(data['stop'])} · TP {number(data['target'])} · RR 1:{number(data['rr'])}",
                         f"Плановый риск до издержек: {number(data['risk_pct'])}%"])
            if data["stage"] == "active":
                text.append("Биржевой позиции бот не видит; показано состояние симулятора.")
        if data["expires_at"] is not None:
            text.append("Срок ожидания: " + date_text(data["expires_at"]))
        if data["reason"]:
            text.append(html.escape(data["reason"]))
    text.append("Снимок: " + date_text(data["event_time"]))
    return "\n".join(text)


def reply(store, message, now_ms):
    command = message.strip().split(maxsplit=1)[0].lower().split("@")[0] if message.strip() else ""
    if command not in ("/btc", "/eth", "/status"):
        return HELP
    states = store.states()
    if command == "/status":
        if not states:
            return "Пока нет снимков TradingView. Подключите webhook-алерт стратегии.\n" + HELP
        blocks = []
        for data in states[:10]:
            block = describe(data, now_ms, store.state_max_age, compact=True)
            if len("\n\n".join(blocks + [block])) > 3500:
                break
            blocks.append(block)
        text = "\n\n".join(blocks)
        return text + (f"\n\nПоказано {len(blocks)} из {len(states)} инструментов." if len(blocks) < len(states) else "")
    asset = command[1:].upper()
    # Match liquid base pairs, including perpetual .P suffix; don't select BTCUP/BTC3L tokens.
    data = next((data for data in states if re.fullmatch(asset + r"(?:USDT|USD|USDC|BUSD)(?:[._!].*)?", data["symbol"].split(":", 1)[1])), None)
    if data is None:
        return f"Пока нет снимков {asset}. Подключите график {asset}/USD или {asset}/USDT к webhook."
    return describe(data, now_ms, store.state_max_age)


class CommandWorker:
    def __init__(self, store, sender):
        self.store, self.sender = store, sender
        self.stop = threading.Event()

    def poll_once(self, now_ms=None):
        updates = self.sender.get_updates(self.store.command_offset())
        for update in updates:
            if self.stop.is_set():
                break
            update_id = update.get("update_id") if isinstance(update, dict) else None
            if type(update_id) is not int or update_id < self.store.command_offset():
                continue
            message = update.get("message", {})
            chat = message.get("chat", {}) if isinstance(message, dict) else {}
            source = message.get("from", {}) if isinstance(message, dict) else {}
            if isinstance(chat, dict) and isinstance(source, dict) and str(chat.get("id", "")) == str(self.sender.chat_id):
                text = message.get("text")
                if isinstance(text, str) and not source.get("is_bot", False):
                    try:
                        self.sender.send(reply(self.store, text, int(time.time() * 1000) if now_ms is None else now_ms))
                    except DeliveryError as exc:
                        if exc.retryable:
                            raise
                        # Consume permanent rejection, so a blocked/invalid reply cannot poison later commands.
            self.store.save_command_offset(update_id + 1)
        return len(updates)

    def run(self):
        while not self.stop.is_set():
            try:
                if not self.poll_once():
                    self.stop.wait(0.5)
            except DeliveryError as exc:
                self.stop.wait(exc.delay or (5 if exc.retryable else 30))
            except Exception:
                self.stop.wait(5)
