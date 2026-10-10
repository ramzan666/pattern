"""Authenticated HTTP ingestion, SQLite outbox, and Telegram delivery."""
import argparse
from contextlib import contextmanager
import hashlib
import hmac
import html
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import math
import os
import re
import sqlite3
import threading
import time
from urllib import error, parse, request

EVENTS = {"context", "breakout", "setup", "trigger", "cancel", "exit"}
STAGES = ("idle", "waiting_retest", "armed", "active", "canceled", "closed")
SYMBOL = re.compile(r"[A-Za-z0-9_]+:[A-Za-z0-9_.!/-]+\Z")
MAX_BODY = 16384


class InvalidEvent(ValueError):
    pass


def snapshot_freshness(data, now_ms, max_age):
    age = max(0, (now_ms - data["event_time"]) / 1000) if data else None
    expired = bool(data and data["stage"] in ("waiting_retest", "armed") and
                   data["expires_at"] is not None and now_ms >= data["expires_at"])
    return age, (expired or age > max_age) if age is not None else True


def validate(data, now_ms):
    if not isinstance(data, dict):
        raise InvalidEvent("JSON object required")
    for name in ("event_id", "setup_id", "symbol", "reason"):
        if not isinstance(data.get(name), str) or len(data[name]) > 500:
            raise InvalidEvent("Invalid " + name)
    if not data["event_id"] or not data["setup_id"] or not SYMBOL.fullmatch(data["symbol"]):
        raise InvalidEvent("Invalid event identity")
    if type(data.get("schema_version")) is not int or data["schema_version"] != 1:
        raise InvalidEvent("Unsupported schema")
    if data.get("strategy") != "trend_retest_v1" or data.get("timeframe") not in ("60", "15"):
        raise InvalidEvent("Unsupported strategy/timeframe")
    if "trend_timeframe" in data and data["trend_timeframe"] not in ("60", "240"):
        raise InvalidEvent("Unsupported trend timeframe")
    if not isinstance(data.get("event"), str) or data["event"] not in EVENTS or data.get("direction") not in ("long", "short", "none"):
        raise InvalidEvent("Invalid event/direction")
    if data.get("stage") not in STAGES:
        raise InvalidEvent("Invalid stage")
    expected_stage = {"breakout": "waiting_retest", "setup": "armed", "trigger": "active", "cancel": "canceled", "exit": "closed"}
    if data["event"] in expected_stage and data["stage"] != expected_stage[data["event"]]:
        raise InvalidEvent("Event/stage mismatch")
    if data.get("trend") not in ("bullish", "bearish", "neutral"):
        raise InvalidEvent("Invalid trend")
    for name in ("event_time", "bar_time", "sequence"):
        if type(data.get(name)) is not int or data[name] < (0 if name == "sequence" else 1):
            raise InvalidEvent("Invalid " + name)
    if max(data["event_time"], data["bar_time"]) > now_ms + 60000:
        raise InvalidEvent("Future timestamp")
    if "expires_at" not in data or (data["expires_at"] is not None and
            (type(data["expires_at"]) is not int or not 1 <= data["expires_at"] <= data["event_time"] + 604800000)):
        raise InvalidEvent("Invalid expires_at")
    for name in ("entry", "stop", "target", "rr", "risk_pct"):
        if name not in data:
            raise InvalidEvent("Missing " + name)
        value = data[name]
        if value is None and name != "risk_pct":
            continue
        if type(value) not in (int, float):
            raise InvalidEvent("Invalid " + name)
        minimum_ok = value >= 0 if name == "risk_pct" else value > 0
        if not minimum_ok or not value < 1e100 or not math.isfinite(value):
            raise InvalidEvent("Invalid " + name)
    if data["risk_pct"] > 100:
        raise InvalidEvent("Invalid risk_pct")
    if data["stage"] in ("armed", "active") and data["risk_pct"] <= 0:
        raise InvalidEvent("Positive plan risk required")
    prices = [data[name] for name in ("entry", "stop", "target")]
    if data["stage"] in ("armed", "active") and (None in prices or data["rr"] is None):
        raise InvalidEvent("Trade plan required")
    if None not in prices:
        entry, stop, target = prices
        if not ((data["direction"] == "long" and stop < entry < target) or
                (data["direction"] == "short" and target < entry < stop)):
            raise InvalidEvent("Invalid price order")
    allowed = {"schema_version", "strategy", "event_id", "setup_id", "sequence", "event", "stage",
               "symbol", "timeframe", "direction", "trend", "event_time", "bar_time", "entry",
               "stop", "target", "rr", "risk_pct", "expires_at", "reason", "secret", "trend_timeframe"}
    if data.keys() - allowed:
        raise InvalidEvent("Unexpected fields")
    return {key: value for key, value in data.items() if key != "secret"}


class Store:
    def __init__(self, path, max_age=300, send_context=False, send_breakout=False, state_max_age=5400, source="tradingview"):
        self.lock = threading.RLock()
        if source not in ("tradingview", "exchange"):
            raise ValueError("Invalid state source")
        self.source = source
        self.max_age = max_age
        self.state_max_age = state_max_age
        self.quiet = ({"context"} if not send_context else set()) | ({"breakout"} if not send_breakout else set())
        self.db = sqlite3.connect(path, timeout=0.5, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA journal_mode=WAL;
            PRAGMA synchronous=FULL;
            CREATE TABLE IF NOT EXISTS events (
                event_id TEXT PRIMARY KEY, digest TEXT, data TEXT, status TEXT, received INTEGER,
                stream TEXT, setup_id TEXT, event TEXT);
            CREATE INDEX IF NOT EXISTS events_setup ON events(stream, setup_id, status, event);
            CREATE TABLE IF NOT EXISTS state (
                stream TEXT PRIMARY KEY, event_time INTEGER, sequence INTEGER, data TEXT);
            CREATE TABLE IF NOT EXISTS outbox (
                event_id TEXT PRIMARY KEY, status TEXT, attempts INTEGER, next_at REAL, error TEXT);
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value INTEGER);
        """)
        if path != ":memory:":
            os.chmod(path, 0o600)

    @contextmanager
    def transaction(self):
        with self.lock:
            if self.db.in_transaction:
                # A surrounding autonomous checkpoint transaction owns the commit.
                yield
                return
            self.db.execute("BEGIN IMMEDIATE")
            try:
                yield
                self.db.commit()
            except Exception:
                self.db.rollback()
                raise

    def ingest(self, data, now_ms):
        encoded = json.dumps(data, sort_keys=True, separators=(",", ":"))
        identity = {k: v for k, v in data.items() if k not in ("event_time", "sequence")}
        digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
        stream = data["strategy"] + "|" + data["symbol"]
        with self.transaction():
            previous = self.db.execute("SELECT digest, status FROM events WHERE event_id=?",
                                       (data["event_id"],)).fetchone()
            if previous:
                if previous["digest"] != digest:
                    raise InvalidEvent("Conflicting event_id")
                return "duplicate"
            latest = self.db.execute("SELECT * FROM state WHERE stream=?", (stream,)).fetchone()
            status = "accepted"
            if now_ms - data["event_time"] > self.max_age * 1000:
                status = "suppressed_late"
            elif data["event"] in ("setup", "trigger") and data["expires_at"] is not None and now_ms >= data["expires_at"]:
                status = "suppressed_expired"
            elif latest and (data["event_time"], data["sequence"]) <= (latest["event_time"], latest["sequence"]):
                status = "suppressed_order"
            if status == "accepted":
                prior = self.db.execute("SELECT event FROM events WHERE stream=? AND setup_id=? "
                                        "AND status='accepted' AND event IN ('trigger','cancel','exit') ORDER BY rowid DESC LIMIT 1",
                                        (stream, data["setup_id"])).fetchone()
                resurrect = data["event"] in ("breakout", "setup", "trigger") or (
                    data["event"] == "context" and (data["stage"] in ("waiting_retest", "armed") or
                                                    (data["stage"] == "active" and prior and prior["event"] != "trigger")))
                if prior and resurrect:
                    status = "suppressed_closed"
            self.db.execute("INSERT INTO events VALUES (?,?,?,?,?,?,?,?)",
                            (data["event_id"], digest, encoded, status, now_ms, stream, data["setup_id"], data["event"]))
            if status == "accepted":
                self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?,?)",
                                (stream, data["event_time"], data["sequence"], encoded))
                if data["event"] not in self.quiet:
                    self.db.execute("INSERT INTO outbox VALUES (?, 'pending', 0, ?, '')",
                                    (data["event_id"], now_ms / 1000))
            return status

    def state(self, symbol):
        with self.lock:
            row = self.db.execute("SELECT data FROM state WHERE stream=?",
                                  ("trend_retest_v1|" + symbol,)).fetchone()
            return json.loads(row[0]) if row else None

    def states(self):
        with self.lock:
            return [json.loads(row[0]) for row in self.db.execute("SELECT data FROM state ORDER BY event_time DESC")]

    def command_offset(self):
        with self.lock:
            row = self.db.execute("SELECT value FROM metadata WHERE key='command_offset'").fetchone()
            return row[0] if row else 0

    def save_command_offset(self, value):
        with self.transaction():
            self.db.execute("INSERT INTO metadata VALUES ('command_offset',?) "
                            "ON CONFLICT(key) DO UPDATE SET value=MAX(value,excluded.value)", (value,))

    def health(self):
        with self.lock:
            return dict(self.db.execute("SELECT status, COUNT(*) FROM outbox GROUP BY status").fetchall())

    def next_job(self, now):
        with self.transaction():
            row = self.db.execute("SELECT o.*, e.data FROM outbox o JOIN events e USING(event_id) "
                                  "WHERE o.status='pending' AND o.next_at<=? ORDER BY e.received, o.rowid LIMIT 1",
                                  (now,)).fetchone()
            if not row:
                return None
            data = json.loads(row["data"])
            latest = self.state(data["symbol"]) or data
            expired = data["event"] in ("setup", "trigger") and data["expires_at"] is not None and now * 1000 >= data["expires_at"]
            late = data["event"] not in ("cancel", "exit") and now * 1000 - data["event_time"] > self.max_age * 1000
            superseded = latest["event_id"] != data["event_id"] and (
                (data["event"] in ("context", "breakout", "setup") and
                 (latest["setup_id"] != data["setup_id"] or latest["stage"] != data["stage"])) or
                (data["event"] == "trigger" and latest["setup_id"] == data["setup_id"] and latest["stage"] in ("canceled", "closed")))
            if expired or superseded or late:
                self.db.execute("UPDATE outbox SET status='suppressed', error=? WHERE event_id=?",
                                ("expired" if expired else "late" if late else "superseded", data["event_id"]))
                return {"skip": True}
            self.db.execute("UPDATE outbox SET attempts=attempts+1, next_at=? WHERE event_id=?",
                            (now + 60, data["event_id"]))
            return {"data": data, "attempt": row["attempts"] + 1}

    def finish(self, event_id, status, next_at=0, reason=""):
        with self.transaction():
            self.db.execute("UPDATE outbox SET status=?, next_at=?, error=? WHERE event_id=?",
                            (status, next_at, reason, event_id))


def timeframe_label(value):
    return {"15": "15м", "60": "1H", "240": "4H"}[value]


def render(data):
    titles = {"context": "Обновление тренда", "breakout": "Пробой — ждём ретест",
              "setup": "Вход подготовлен", "trigger": "Условие входа выполнено",
              "cancel": "Сценарий отменён", "exit": "Условие выхода выполнено"}
    trends = {"bullish": "бычий", "bearish": "медвежий", "neutral": "нейтральный"}
    direction = {"long": "LONG", "short": "SHORT", "none": "НАБЛЮДЕНИЕ"}[data["direction"]]
    # Keep original Pine payloads compatible; Python includes its selected trend TF.
    trend_tf = timeframe_label(data.get("trend_timeframe", "240"))
    signal_tf = timeframe_label(data["timeframe"])
    parts = [
        f"<b>{html.escape(data['symbol'])} · {direction}</b>",
        titles[data["event"]],
        f"Тренд {trend_tf}: {trends[data['trend']]} · сигналы {signal_tf}",
    ]
    if data["entry"] is not None:
        number = lambda value: format(value, ".10g") if value is not None else "—"
        parts.extend([f"Вход: <b>{number(data['entry'])}</b>",
                      f"SL: {number(data['stop'])} · TP: {number(data['target'])}",
                      f"RR: 1:{number(data['rr'])} · риск: {number(data['risk_pct'])}%"])
    if data["event"] == "setup":
        parts.append("Ждём достижения уровня. Позиция ещё не открыта.")
    if data["event"] == "trigger":
        parts.append("Сигнал стратегии; фактическое исполнение на бирже не подтверждено.")
    if data["reason"]:
        parts.append(html.escape(data["reason"]))
    parts.append(f'<a href="https://www.tradingview.com/chart/?symbol={parse.quote(data["symbol"], safe="")}&amp;interval={data["timeframe"]}">Открыть график</a>')
    return "\n".join(parts)


class DeliveryError(Exception):
    def __init__(self, retryable=True, delay=None):
        self.retryable, self.delay = retryable, delay


class TelegramSender:
    def __init__(self, token, chat_id):
        self.url = "https://api.telegram.org/bot" + token + "/"
        self.chat_id = chat_id

    def send(self, message, reply_markup=None):
        payload = {"chat_id": self.chat_id, "text": message, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        self.call("sendMessage", payload)

    def get_updates(self, offset):
        result = self.call("getUpdates", {"offset": offset, "timeout": 10, "limit": 10,
                                           "allowed_updates": ["message"]}, timeout=15)
        if not isinstance(result.get("result"), list):
            raise DeliveryError()
        return result["result"]

    def call(self, method, payload, timeout=10):
        req = request.Request(self.url + method, json.dumps(payload).encode(), {"Content-Type": "application/json"})
        try:
            with request.urlopen(req, timeout=timeout) as response:
                result = json.loads(response.read(1048576))
        except error.HTTPError as exc:
            delay = None
            if exc.code == 429:
                try:
                    delay = max(1, int(json.loads(exc.read(16384))["parameters"]["retry_after"]))
                except (ValueError, KeyError, TypeError):
                    delay = 60
            raise DeliveryError(exc.code == 429 or exc.code >= 500, delay) from None
        except (error.URLError, OSError, ValueError):
            raise DeliveryError() from None
        if not isinstance(result, dict):
            raise DeliveryError()
        if not result.get("ok"):
            code = result.get("error_code", 500)
            delay = result.get("parameters", {}).get("retry_after")
            raise DeliveryError(code == 429 or code >= 500, delay)
        return result


class Worker:
    def __init__(self, store, sender):
        self.store, self.sender = store, sender
        self.stop = threading.Event()

    def run_once(self, now=None):
        if self.sender is None:
            return False
        now = time.time() if now is None else now
        job = self.store.next_job(now)
        if not job:
            return False
        if job.get("skip"):
            return True
        event_id = job["data"]["event_id"]
        try:
            self.sender.send(render(job["data"]))
        except DeliveryError as exc:
            delay = exc.delay or min(300, 2 ** min(job["attempt"], 8))
            self.store.finish(event_id, "pending" if exc.retryable else "failed", now + delay,
                              "delivery_retry" if exc.retryable else "delivery_rejected")
        except Exception:
            self.store.finish(event_id, "pending", now + 30, "delivery_retry")
        else:
            self.store.finish(event_id, "sent")
        return True

    def run(self):
        while not self.stop.is_set():
            if not self.run_once():
                self.stop.wait(0.5)


def handler(store, secret, configured=False):
    class Handler(BaseHTTPRequestHandler):
        def setup(self):
            super().setup()
            self.connection.settimeout(5)

        def log_message(self, *args):
            pass  # Never log webhook bodies, query strings, or credentials.

        def respond(self, code, body):
            encoded = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self):
            url = parse.urlsplit(self.path)
            if url.path == "/health":
                return self.respond(200, {"ok": True, "telegram_configured": configured, "outbox": store.health()})
            if url.path != "/state":
                return self.respond(404, {"error": "Not found"})
            if not hmac.compare_digest(self.headers.get("X-Webhook-Secret", "").encode(), secret.encode()):
                return self.respond(401, {"error": "Unauthorized"})
            symbols = parse.parse_qs(url.query).get("symbol", [])
            if len(symbols) != 1 or not SYMBOL.fullmatch(symbols[0]):
                return self.respond(400, {"error": "Invalid symbol"})
            result = store.state(symbols[0])
            age, stale = snapshot_freshness(result, time.time() * 1000, store.state_max_age)
            self.respond(200 if result else 404, {"state": result, "age_seconds": age,
                                                 "stale": stale})

        def do_POST(self):
            if self.path != "/webhook":
                return self.respond(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or self.headers.get("Transfer-Encoding"):
                    return self.respond(400, {"error": "Content-Length required"})
                if length > MAX_BODY:
                    return self.respond(413, {"error": "Payload too large"})
                def strict_object(pairs):
                    result = dict(pairs)
                    if len(result) != len(pairs):
                        raise ValueError("Duplicate fields")
                    return result
                def invalid_constant(value):
                    raise ValueError("Non-finite number")
                data = json.loads(self.rfile.read(length), parse_constant=invalid_constant, object_pairs_hook=strict_object)
                supplied = data.get("secret") if isinstance(data, dict) else None
                if not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), secret.encode()):
                    return self.respond(401, {"error": "Unauthorized"})
                clean = validate(data, int(time.time() * 1000))
                status = store.ingest(clean, int(time.time() * 1000))
                self.respond(202, {"status": status})
            except (ValueError, UnicodeError, OverflowError, RecursionError) as exc:
                message = str(exc) if isinstance(exc, InvalidEvent) else "Invalid JSON/request"
                self.respond(400, {"error": message})
            except sqlite3.Error:
                self.respond(503, {"error": "Storage temporarily unavailable"})
    return Handler


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--database", default="trading_assistant.sqlite3")
    args = parser.parse_args()
    secret = os.environ.get("WEBHOOK_SECRET", "")
    if len(secret) < 24:
        parser.error("Set WEBHOOK_SECRET to at least 24 random characters")
    token, chat_id = os.environ.get("TELEGRAM_BOT_TOKEN"), os.environ.get("TELEGRAM_CHAT_ID")
    if bool(token) != bool(chat_id):
        parser.error("Set both TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID, or neither")
    commands_enabled = os.environ.get("ENABLE_COMMANDS", "true").lower() == "true"
    if token and commands_enabled and not re.fullmatch(r"-?\d+", chat_id):
        parser.error("Telegram commands require a numeric TELEGRAM_CHAT_ID")
    try:
        max_age = int(os.environ.get("MAX_EVENT_AGE_SECONDS", "300"))
        if not 1 <= max_age <= 86400:
            raise ValueError()
    except ValueError:
        parser.error("MAX_EVENT_AGE_SECONDS must be an integer between 1 and 86400")
    try:
        state_max_age = int(os.environ.get("MAX_STATE_AGE_SECONDS", "5400"))
        if not 1 <= state_max_age <= 604800:
            raise ValueError()
    except ValueError:
        parser.error("MAX_STATE_AGE_SECONDS must be an integer between 1 and 604800")
    os.umask(0o077)
    store = Store(args.database, max_age=max_age,
                  send_context=os.environ.get("SEND_CONTEXT", "false").lower() == "true",
                  send_breakout=os.environ.get("SEND_BREAKOUT", "false").lower() == "true", state_max_age=state_max_age)
    sender = TelegramSender(token, chat_id) if token else None
    worker = Worker(store, sender)
    thread = threading.Thread(target=worker.run, daemon=True)
    thread.start()
    from .commands import CommandWorker
    commands = CommandWorker(store, sender) if sender and commands_enabled else None
    command_thread = threading.Thread(target=commands.run, daemon=True) if commands else None
    if command_thread:
        command_thread.start()
    server = ThreadingHTTPServer((args.host, args.port), handler(store, secret, bool(sender)))
    print("Bridge ready. Telegram " + ("configured." if sender else "disabled; events retained in SQLite."))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        worker.stop.set()
        if commands:
            commands.stop.set()
            command_thread.join(timeout=17)
        thread.join(timeout=11)
        store.db.close()
