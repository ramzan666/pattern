"""python -m trading_assistant.autonomous --help"""
import argparse
from contextlib import ExitStack
from dataclasses import replace
import getpass
import json
import os
from pathlib import Path
import re
import signal
import sqlite3
import sys
import threading
import warnings

from .engine import DataGap, Settings
from .market import Bybit, MarketError
from .runtime import Runner, replay
from .storage import DatabaseLease
from ..bridge.app import DeliveryError, Store, TelegramSender, Worker
from ..bridge.commands import CommandWorker


def configure(path):
    """Interactive local setup. It reads Telegram updates, never sends messages."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("Токен BotFather (скрытый ввод): ").strip()
    if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]{20,}", token):
        raise ValueError("Некорректный формат токена")
    try:
        updates = TelegramSender(token, "0").get_updates(0)
    except DeliveryError:
        raise ValueError("Не удалось прочитать Telegram. Проверьте токен, сеть и /start") from None
    ids = {str(message["chat"]["id"]) for update in updates
           if isinstance(update, dict)
           for message in [update.get("message", {})]
           if isinstance(message, dict) and isinstance(message.get("chat"), dict)
           and message["chat"].get("type") == "private"}
    if len(ids) == 1:
        chat_id = ids.pop()
    else:
        chat_id = input("Числовой ID вашего личного чата: ").strip()
    if not re.fullmatch(r"[0-9]+", chat_id):
        raise ValueError("Нужен положительный числовой ID личного чата")
    path = Path(path).expanduser()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as output:
        output.write(f"TELEGRAM_BOT_TOKEN={token}\nTELEGRAM_CHAT_ID={chat_id}\n")
    print(f"Настройки сохранены: {path}. Содержимое не выводится.")


def credentials(path=None):
    values = {}
    if path:
        for line in Path(path).expanduser().read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            key, separator, value = line.partition("=")
            if not separator or key not in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_CHAT_ID"):
                raise ValueError("В env-файле разрешены только TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            values[key] = value
    token = values.get("TELEGRAM_BOT_TOKEN", os.getenv("TELEGRAM_BOT_TOKEN"))
    chat = values.get("TELEGRAM_CHAT_ID", os.getenv("TELEGRAM_CHAT_ID"))
    if bool(token) != bool(chat):
        raise ValueError("Задайте вместе TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID")
    if token and not re.fullmatch(r"[0-9]+", chat):
        raise ValueError("Нужен числовой ID личного Telegram-чата")
    return token, chat


def main():
    parser = argparse.ArgumentParser(description="Bybit Spot LONG: самостоятельные сигналы и виртуальные сделки")
    parser.add_argument("--market", choices=("spot", "linear"), default="spot")
    parser.add_argument("--timeframe", choices=("1h", "15m"), help="По умолчанию 1H; при рестарте восстанавливается сохранённый режим")
    parser.add_argument("--symbols", nargs="+", default=["BTCUSDT", "ETHUSDT"])
    parser.add_argument("--direction", choices=("long", "short", "both"))
    parser.add_argument("--rr", type=float, default=2)
    parser.add_argument("--risk-percent", type=float, default=.5)
    parser.add_argument("--paper-equity", type=float, default=10_000, help="Виртуальный капитал отдельно для каждой пары")
    parser.add_argument("--commission-percent", type=float, default=.05, help="Комиссия на одну сторону, в процентах")
    parser.add_argument("--slippage-ticks", type=int, default=2)
    parser.add_argument("--poll-seconds", type=int, default=15)
    parser.add_argument("--database", default="bybit_assistant.sqlite3")
    parser.add_argument("--env-file", help="Явно загрузить файл с двумя Telegram-параметрами")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--configure", action="store_true", help="Скрыто запросить токен и сохранить env-файл")
    modes.add_argument("--once", action="store_true", help="Однократная проверка источника в памяти, без Telegram")
    modes.add_argument("--replay", action="store_true", help="Историческая симуляция выбранного ТФ; без Telegram и записи базы")
    args = parser.parse_args()
    try:
        if args.configure:
            configure(args.env_file or "~/.config/trading-assistant/bybit.env")
            return
        if not 5 <= args.poll_seconds <= 300:
            raise ValueError("Интервал проверки: 5–300 секунд")
        if len(set(args.symbols)) != len(args.symbols) or not 1 <= len(args.symbols) <= 10:
            raise ValueError("Нужны 1–10 разных USDT-пар")
        timeframe = 15 if args.timeframe == "15m" else 60
        settings = Settings(timeframe_minutes=timeframe, trend_minutes=4 * timeframe,
                            direction=args.direction or ("long" if args.market == "spot" else "both"),
                            rr=args.rr, risk_percent=args.risk_percent, initial_equity=args.paper_equity,
                            commission=args.commission_percent / 100, slippage_ticks=args.slippage_ticks)
        client = Bybit(args.market)
        instruments = [client.instrument(symbol) for symbol in args.symbols]
        if args.replay:
            horizon = client.now() - 2000
            for instrument in instruments:
                result = replay(instrument, settings,
                                client.candles(instrument.symbol, str(settings.timeframe_minutes), horizon),
                                client.candles(instrument.symbol, str(settings.trend_minutes), horizon))
                print(json.dumps(result, ensure_ascii=False, allow_nan=False))
            return
        token, chat = (None, None) if args.once else credentials(args.env_file)
        os.umask(0o077)
        with ExitStack() as cleanup:
            if not args.once:
                cleanup.enter_context(DatabaseLease(args.database))
            store = Store(":memory:" if args.once else args.database, source="exchange", state_max_age=180)
            cleanup.callback(store.db.close)
            if args.timeframe is None:
                selected = store.db.execute("SELECT value FROM metadata WHERE key='autonomous_timeframe'").fetchone()
                if selected:
                    settings = replace(settings, timeframe_minutes=selected[0], trend_minutes=4 * selected[0])
            runner = Runner(store, client, instruments, settings)
            if args.once:
                print(json.dumps(runner.poll(), ensure_ascii=False, allow_nan=False))
                return
            sender = TelegramSender(token, chat) if token else None
            worker = Worker(store, sender)
            commands = CommandWorker(store, sender, command_handler=runner.command) if sender else None
            threads = [threading.Thread(target=worker.run, daemon=True)]
            if commands:
                threads.append(threading.Thread(target=commands.run, daemon=True))
            for thread in threads:
                thread.start()
            stop = threading.Event()
            previous_sigterm = signal.signal(signal.SIGTERM, lambda *_: stop.set())
            print(f"Bybit {args.market}: {', '.join(args.symbols)}. Telegram {'включён' if sender else 'не настроен'}.")
            try:
                while not stop.is_set():
                    try:
                        changes = runner.poll()
                        if changes:
                            print(json.dumps(changes, ensure_ascii=False, allow_nan=False), flush=True)
                        if runner.last_errors:
                            print("Неполные обновления пропущены: " + ", ".join(runner.last_errors), file=sys.stderr, flush=True)
                    except (MarketError, DataGap, ValueError, OSError, sqlite3.Error):
                        print("Свечи/хранилище недоступны или неполны: обновление не выполнено, повтор после паузы.", file=sys.stderr, flush=True)
                    stop.wait(args.poll_seconds)
            except KeyboardInterrupt:
                pass
            finally:
                signal.signal(signal.SIGTERM, previous_sigterm)
                worker.stop.set()
                if commands:
                    commands.stop.set()
                for thread in threads:
                    thread.join(timeout=17)
    except (MarketError, DataGap, ValueError, OSError, sqlite3.Error, getpass.GetPassWarning) as exc:
        parser.error(str(exc))


if __name__ == "__main__":
    main()
