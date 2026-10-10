"""Bybit V5 public market data. No API keys or private trading endpoints."""
import json
from urllib import error, parse, request

from .engine import Candle, HOUR, Instrument, MINUTE


class MarketError(RuntimeError):
    pass


class Bybit:
    def __init__(self, category="spot"):
        if category not in ("spot", "linear"):
            raise ValueError("Unsupported category")
        self.category = category

    def _get(self, path, parameters=None):
        url = "https://api.bybit.com" + path
        if parameters:
            url += "?" + parse.urlencode(parameters)
        req = request.Request(url, headers={"User-Agent": "CryptoTrendAssistant/1"})
        try:
            with request.urlopen(req, timeout=15) as response:
                encoded = response.read(4_194_305)
            if len(encoded) > 4_194_304:
                raise MarketError("Bybit response too large")
            result = json.loads(encoded)
        except error.HTTPError as exc:
            raise MarketError(f"Bybit HTTP {exc.code}") from None
        except (error.URLError, OSError, ValueError):
            raise MarketError("Bybit connection/JSON unavailable") from None
        if not isinstance(result, dict) or type(result.get("retCode")) is not int or result["retCode"] != 0:
            raise MarketError("Bybit rejected public market request")
        if not isinstance(result.get("result"), dict):
            raise MarketError("Bybit result missing")
        return result["result"]

    def now(self):
        try:
            now = int(self._get("/v5/market/time")["timeNano"]) // 1_000_000
            if now <= 0:
                raise ValueError()
            return now
        except (KeyError, TypeError, ValueError):
            raise MarketError("Invalid Bybit server time") from None

    def instrument(self, symbol):
        if not symbol.isalnum() or not symbol.endswith("USDT"):
            raise ValueError("Invalid USDT symbol")
        data = self._get("/v5/market/instruments-info", {"category": self.category, "symbol": symbol})
        try:
            item = next(x for x in data["list"] if x["symbol"] == symbol)
            if item["status"] != "Trading" or item["quoteCoin"] != "USDT":
                raise ValueError()
            if self.category == "linear" and (item.get("settleCoin") != "USDT" or item.get("contractType") != "LinearPerpetual"):
                raise ValueError()
            lot = item["lotSizeFilter"]
            step = float(lot["basePrecision"] if self.category == "spot" else lot["qtyStep"])
            return Instrument(symbol, self.category, float(item["priceFilter"]["tickSize"]),
                              step, max(step, float(lot.get("minOrderQty") or step)),
                              float((lot.get("minOrderAmt") if self.category == "spot" else lot.get("minNotionalValue")) or 0))
        except (KeyError, StopIteration, TypeError, ValueError):
            raise MarketError("Unsupported Bybit instrument/filter format") from None

    def candles(self, symbol, interval, now_ms, start_ms=None, limit=1000):
        duration = {"1": MINUTE, "15": 15 * MINUTE, "60": HOUR, "240": 4 * HOUR}.get(interval)
        if duration is None or not 1 <= limit <= 1000:
            raise ValueError("Invalid candle request")
        candles = {}
        end = now_ms - 1
        for _ in range(200):
            parameters = {"category": self.category, "symbol": symbol,
                          "interval": interval, "limit": limit, "end": end}
            if start_ms is not None:
                parameters["start"] = start_ms
            data = self._get("/v5/market/kline", parameters)
            try:
                rows = data["list"]
                if not isinstance(rows, list):
                    raise ValueError()
                starts = []
                for row in rows:
                    start = int(row[0])
                    if start % duration:
                        raise ValueError()
                    starts.append(start)
                    candle = Candle(start, start + duration, *map(float, row[1:5]))
                    if candle.end <= now_ms and (start_ms is None or start >= start_ms):
                        previous = candles.get(start)
                        if previous is not None and previous != candle:
                            raise ValueError()
                        candles[start] = candle
            except (KeyError, TypeError, ValueError, IndexError):
                raise MarketError("Invalid Bybit OHLC response") from None
            if not starts or start_ms is None or min(starts) <= start_ms or len(rows) < limit:
                break
            next_end = min(starts) - 1
            if next_end >= end:
                raise MarketError("Bybit pagination did not advance")
            end = next_end
        else:
            raise MarketError("Catch-up exceeds 200 pages; finish replay before going live")
        return [candles[key] for key in sorted(candles)]
