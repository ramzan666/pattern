# Запуск с нуля: TradingView и Telegram

Начните с графика TradingView. Сервер и Telegram можно подключить после
проверки отображения и уведомлений. Эта версия показывает сценарии и сделки
**модели TradingView**; она не отправляет ордера на биржу.

## 1. Проверить стратегию без сервера

1. Откройте обычные свечи BTC/USDT на выбранной бирже, период **1H**.
2. В Pine Editor выделите весь прежний код и вставьте содержимое
   [trend_retest.txt](trend_retest.txt). Сохраните и добавьте на график.
   Если появляется требование Premium для детализации баров, откройте
   **Настройки стратегии → Свойства** и выключите **Bar Magnifier**.
   В новой версии он выключен по умолчанию, но старый флажок мог сохраниться.
3. В настройках скрипта оставьте **Формат уведомлений → TradingView**.
4. Посмотрите уровни входа, SL/TP и результаты тестера. Затем создайте alert
   по этой стратегии: **только вызовы `alert()`**. Название пункта интерфейса
   может быть «Any alert() function call» или «alert() function calls only».
   Включите push-уведомления приложения TradingView.
5. Для ETH повторите на отдельном графике 1H и создайте отдельный alert.

Пробой и ретест должны сформироваться по правилам стратегии: уведомления
не обязаны появиться сразу. Сигналы прошлой истории не отправляются заново.
После изменения кода, параметров или инструмента удаляйте старый alert
и создавайте новый: alert хранит снимок настроек в момент создания.

Подробные торговые правила и ограничения: [README.md](README.md).

## 2. Создать личного Telegram-бота

1. В Telegram найдите официальный **[@BotFather](https://t.me/BotFather)**.
2. Отправьте `/newbot`, задайте имя и свободный username, заканчивающийся
   на `bot`. BotFather выдаст токен вида `123456789:...`.
3. Сохраните токен в менеджере паролей. Откройте ссылку своего нового бота,
   нажмите **Start** или отправьте `/start` в личном чате.

Токен даёт управление ботом. Не присылайте его в чат с помощником,
не вставляйте в Pine, Git или команды оболочки. Если токен раскрыт,
перевыпустите его через BotFather. Этому проекту не нужен Telegram webhook:
команды бот получает через `getUpdates`. Для нового бота ничего отключать
не требуется; другие процессы, читающие `getUpdates` того же бота, не запускайте.

## 3. Подготовить сервер и домен

Подойдёт собственный VPS с **Ubuntu 24.04 LTS**, публичным IPv4 и SSH-доступом.
Нужен домен или поддомен, например `signals.example.com`. VPS оплачивается
регулярно; отдельно учитывайте домен и возможности своего тарифа TradingView
для webhook/количества alerts. Актуальные цены проверьте у выбранных провайдеров.

В DNS создайте запись **A** для своего поддомена на IPv4 VPS. Если есть запись
AAAA, она тоже должна вести на этот сервер с работающим IPv6; иначе удалите её.
В панели провайдера разрешите входящие TCP **22** для SSH, **80** и **443**
для Caddy. Если используется firewall самой Ubuntu, разрешите те же порты,
сохранив доступ по SSH. **8080 наружу не открывайте**: мост слушает localhost.

Все следующие команды выполняйте по SSH на VPS под своим пользователем
с доступом `sudo`. Заменяйте `signals.example.com` своим доменом.

```bash
sudo apt update
sudo apt install -y python3 git caddy curl ca-certificates tzdata
sudo useradd --system --user-group --no-create-home --home-dir /var/lib/trading-assistant --shell /usr/sbin/nologin trading-assistant
sudo git clone --branch feature/crypto-trend-assistant --single-branch https://github.com/ramzan666/pattern.git /opt/trading-assistant
```

Команды рассчитаны на новый сервер: если пользователь или каталог уже есть,
проверьте их вместо повторного создания. Код остаётся в `/opt/trading-assistant`,
принадлежит root и читается сервисным пользователем. Python-библиотеки
устанавливать через `pip` не нужно. `tzdata` нужна для дат по Москве в сообщениях.

## 4. Получить личный chat ID и сохранить настройки

После `/start` выполните этот блок в интерактивном SSH-терминале. Он запросит
токен скрытым вводом и покажет только числовой ID личного чата. Токен не входит
в командную строку или shell history; сырые сетевые ошибки не печатаются.
Пока мост не запущен, других читателей `getUpdates` быть не должно.

```bash
python3 - <<'PY'
import getpass
import json
import sys
import urllib.parse
import urllib.request
import warnings

try:
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        token = getpass.getpass("Токен нового бота (скрытый ввод): ").strip()
    if not token:
        raise ValueError("empty token")
    body = urllib.parse.urlencode({
        "timeout": 0,
        "limit": 100,
        "allowed_updates": json.dumps(["message"]),
    }).encode()
    request = urllib.request.Request(
        "https://api.telegram.org/bot" + token + "/getUpdates",
        data=body,
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        result = json.load(response)
    if not result.get("ok"):
        raise ValueError("Telegram refused request")
    chat_ids = {
        update["message"]["chat"]["id"]
        for update in result.get("result", [])
        if update.get("message", {}).get("chat", {}).get("type") == "private"
        and not update.get("message", {}).get("from", {}).get("is_bot", False)
    }
    if len(chat_ids) == 1:
        print("Личный TELEGRAM_CHAT_ID:", chat_ids.pop())
    else:
        print("Не найден единственный личный чат. Отправьте /start своему новому боту и повторите.")
except KeyboardInterrupt:
    print("Отменено.")
    sys.exit(1)
except Exception:
    print("Не удалось получить ID. Проверьте токен, /start, сеть и отсутствие другого getUpdates-процесса.")
    sys.exit(1)
PY
```

Для webhook нужен **другой секрет**, не токен Telegram. Создайте его:

```bash
python3 -c 'import secrets; print(secrets.token_urlsafe(32))'
```

Сохраните значение для сервера и настроек Pine. Создайте файл окружения
с правами только для root и откройте редактор:

```bash
sudo touch /etc/trading-assistant.env
sudo chown root:root /etc/trading-assistant.env
sudo chmod 600 /etc/trading-assistant.env
sudoedit /etc/trading-assistant.env
```

Вставьте, заменив три первых значения. Настоящие значения вводите **в редакторе**,
а не командами `echo`/`export` в терминале:

```ini
WEBHOOK_SECRET=ВСТАВЬТЕ_СГЕНЕРИРОВАННЫЙ_WEBHOOK_СЕКРЕТ
TELEGRAM_BOT_TOKEN=ВСТАВЬТЕ_ТОКЕН_BOTFATHER
TELEGRAM_CHAT_ID=ВСТАВЬТЕ_ЧИСЛОВОЙ_ID
MAX_EVENT_AGE_SECONDS=300
MAX_STATE_AGE_SECONDS=5400
SEND_CONTEXT=false
SEND_BREAKOUT=false
ENABLE_COMMANDS=true
```

Проверьте права, не выводя содержимое файла:

```bash
sudo stat -c '%U %G %a %n' /etc/trading-assistant.env
```

Ожидается `root root 600 /etc/trading-assistant.env`. Файл загрузит systemd;
сам Python-мост `.env` автоматически не читает.

## 5. Запустить постоянный сервис

Откройте файл unit:

```bash
sudoedit /etc/systemd/system/trading-assistant.service
```

Вставьте:

```ini
[Unit]
Description=TradingView Telegram assistant
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
User=trading-assistant
Group=trading-assistant
WorkingDirectory=/opt/trading-assistant
EnvironmentFile=/etc/trading-assistant.env
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
StateDirectory=trading-assistant
StateDirectoryMode=0700
UMask=0077
ExecStart=/usr/bin/python3 -m trading_assistant.bridge --port 8080 --database /var/lib/trading-assistant/state.sqlite3
Restart=on-failure
RestartSec=5
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true

[Install]
WantedBy=multi-user.target
```

`StateDirectory` создаёт каталог SQLite и разрешает запись в него сервису
при `ProtectSystem=strict`. Код и секреты сервис менять не может. systemd
читает root-файл окружения до запуска процесса от отдельного пользователя.

```bash
sudo systemd-analyze verify /etc/systemd/system/trading-assistant.service
sudo systemctl daemon-reload
sudo systemctl enable --now trading-assistant
sudo systemctl status trading-assistant --no-pager
curl --fail --silent --show-error http://127.0.0.1:8080/health
```

Ответ `/health` должен содержать `"ok": true` и `"telegram_configured": true`.
Последнее означает, что параметры заданы; работоспособность доставки проверяет
ответ бота. Отправьте своему боту `/status`: до подключения TradingView
ожидается ответ **«Пока нет снимков TradingView»**.

Для диагностики и после изменения файла окружения:

```bash
sudo journalctl -u trading-assistant -n 50 --no-pager
sudo systemctl restart trading-assistant
```

Если менялся сам unit, перед restart нужен `sudo systemctl daemon-reload`.
База и offset команд переживают перезапуск. Сохраняйте резервные копии данных
при остановленном сервисе; не удаляйте SQLite, чтобы «починить» уведомления.

## 6. Включить публичный HTTPS

DNS должен уже указывать на VPS, порты 80/443 — быть доступны. Откройте
конфигурацию Caddy:

```bash
sudoedit /etc/caddy/Caddyfile
```

На новом сервере замените пример содержимым ниже. На сервере с другими сайтами
добавьте отдельный блок, сохранив существующие:

```caddyfile
signals.example.com {
    request_body {
        max_size 16KB
    }
    reverse_proxy 127.0.0.1:8080
}
```

```bash
sudo caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
sudo systemctl enable --now caddy
sudo systemctl reload caddy
curl --fail --silent --show-error https://signals.example.com/health
```

Caddy получает и обновляет HTTPS-сертификат автоматически. Публичный `/health`
должен вернуть тот же JSON, что локальный. Секрет в этот запрос не требуется.
Если HTTPS не работает, проверьте DNS, firewall и журнал
`sudo journalctl -u caddy -n 50 --no-pager`. HTTP access logging в примере
не включён; не добавляйте журналирование тел webhook с секретом.

## 7. Подключить TradingView и проверить весь путь

1. Включите двухфакторную аутентификацию TradingView. Убедитесь, что тариф
   и доступный лимит alerts позволяют webhook.
2. На обычном графике BTC/USDT **1H** откройте настройки скрипта:
   **Формат уведомлений → Webhook / Telegram**.
3. В поле **Webhook-секрет (не токен Telegram)** вставьте точно значение
   `WEBHOOK_SECRET` из `/etc/trading-assistant.env`. Это параметр скрипта,
   а не строка исходного кода. Не публикуйте настройки и скриншоты с секретом.
4. Удалите прежний alert для этого инструмента. Создайте новый с условием
   **только вызовы `alert()`**, включите Webhook URL:
   `https://signals.example.com/webhook`. Сам URL секрета не содержит.
   Тело JSON формирует Pine через `alert()`; вручную JSON не вставляйте.
5. Для ETH повторите на его графике 1H. Оставьте по одному работающему alert
   на инструмент и эту версию стратегии.
6. Проверьте `/status`, `/btc` и `/eth` в Telegram. Первый live-снимок приходит
   при первом вычислении активного alert на обновлении цены. Затем состояние
   обновляется на закрытых часовых свечах и событиях сценария. Если сразу
   снимка нет, проверьте работающий alert и дождитесь следующего обновления;
   история не рассылается как новые сигналы.

В Telegram по умолчанию приходят подготовка входа с уровнями и сроком,
активация модели, отмена и выход. Снимки контекста и пробой до ретеста
сохраняются тихо: `/status` показывает их без постоянных сообщений.
Время в ответах — **Москва**. Если снимок старше 1,5 часа или ожидание
просрочено, бот отметит это; у него нет независимого подключения к котировкам.

После перезапуска сервиса снова проверьте `/health` и `/status`. После изменения
Pine или его торговых настроек обязательно пересоздайте alert. Для обновления
кода остановите сервис, обновите ветку в `/opt/trading-assistant` и запустите
сервис снова; серверный файл окружения и `/var/lib/trading-assistant` не входят
в Git и сохраняются отдельно.

Подробности событий, очереди и возможных повторов исходящей доставки:
[bridge/README.md](bridge/README.md). Рыночный винрейт этой версии не измерен;
сообщение о входе сообщает о модели, а реальную сделку и риск контролируете вы.
