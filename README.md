# Crypto Trend Retest Assistant

Стратегия «тренд 4H → пробой 1H → ретест → активация» с разметкой
TradingView, историческим тестером и Telegram-уведомлениями.

- [Начать с TradingView](trading_assistant/README.md)
- [Pine Script v6](trading_assistant/trend_retest.pine)
- [Telegram-мост: запуск и API](trading_assistant/bridge/README.md)

Код создаёт торговые сценарии и моделирует сделки в TradingView.
Для живых уведомлений Telegram нужны собственный бот и постоянно
работающий сервер с HTTPS. Результаты торговли и винрейт пока не измерены.

Проверка моста без отправки сообщений:

```bash
python -m unittest trading_assistant.bridge.test_bridge trading_assistant.bridge.test_commands -v
```
