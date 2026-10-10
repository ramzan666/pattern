# Crypto Trend Retest Assistant

Стратегия «тренд → пробой → ретест → активация» в двух вариантах:
самостоятельный бот Bybit → Telegram и стратегия TradingView.

- [Самостоятельный бот: запуск с нуля, без домена и TradingView](trading_assistant/autonomous/README.md)
- [Начать с TradingView](trading_assistant/README.md)
- [Pine Script v6](trading_assistant/trend_retest.pine)
- [Telegram-мост: запуск и API](trading_assistant/bridge/README.md)

Самостоятельный бот получает публичные свечи Bybit Spot для BTCUSDT и ETHUSDT,
ищет только LONG и ведёт виртуальные сделки. Режимы: 1H/4H и 15м/1H;
переключение через `/tf1h` и `/tf15`. Нужны свой Telegram-бот и постоянно
работающий компьютер или сервер с исходящим HTTPS. Домен не требуется.

Код не размещает реальные биржевые ордера. Результаты торговли и винрейт
пока не измерены. Вариант с уведомлениями из TradingView использует отдельный
HTTPS-мост, описанный по ссылке выше.

Проверка алгоритма и доставки без отправки сообщений:

```bash
python -m unittest trading_assistant.autonomous.test_autonomous trading_assistant.bridge.test_bridge trading_assistant.bridge.test_commands -v
```
