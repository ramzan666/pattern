"""Deterministic explanations of saved strategy snapshots; no external AI."""
import re

from .app import snapshot_freshness


def explain_snapshot(data, now_ms, max_age):
    _, stale = snapshot_freshness(data, now_ms, max_age)
    if stale:
        return "Снимок устарел или срок ожидания истёк. Текущий вход по нему рассматривать нельзя; нужны новые данные."
    phase = data["stage"]
    if phase == "idle":
        if data["trend"] != "bullish":
            return "Для LONG нужен бычий фильтр старшего ТФ. Сейчас он не выполнен, поэтому вход не подготовлен."
        return "Бычий фильтр выполнен, но готового входа ещё нет. Нужны закрытый пробой диапазона и подтверждённый ретест."
    return {
        "waiting_retest": "Пробой уже подтверждён. Ждём ретест уровня с закрытием выше него; покупать только из-за пробоя рано.",
        "armed": "Ретест подтверждён. Вход подготовлен выше максимума свечи ретеста, но ещё не активирован. Уровни и срок указаны в снимке.",
        "active": "Виртуальный вход уже активирован. Модель ждёт SL или TP; повторного сигнала на вход сейчас нет. Реальный биржевой ордер бот не размещал.",
        "canceled": "Предыдущий сценарий отменён. Причина указана в снимке; его уровни больше не являются готовым входом.",
        "closed": "Предыдущая виртуальная сделка завершена. Для следующего входа нужен новый сценарий.",
    }[phase]


def assistant_answer(store, message, now_ms):
    """Return None for unsupported questions and commands; never execute actions."""
    text = message.lower().strip()
    if not text or text.startswith("/"):
        return None
    if any(word in text for word in ("стратег", "правила", "ema", " ема")):
        return ("<b>Как работает стратегия LONG</b>\n"
                "Тренд старшего ТФ: close выше EMA50, EMA50 выше EMA200 и растёт. "
                "Затем закрытие выше максимума предыдущих 20 сигнальных свечей и ретест в следующих пяти свечах. "
                "После ретеста вход — high ретеста + тик; SL — low ретеста − 0,25 ATR14. "
                "Вход действует следующие две сигнальные свечи. Известный неснятый свинг до TP блокирует план. "
                "Ожидание отменяется при потере тренда, истечении срока или нарушении SL на закрытии сигнальной свечи. "
                "Режимы: сигналы 1H / тренд 4H или сигналы 15м / тренд 1H. Сделки виртуальные.")
    if any(word in text for word in ("риск", "комис", "стоп", "тейк", " rr", "rr ")) or text in ("rr", "sl", "tp"):
        return ("<b>Риск и выход</b>\n"
                "По умолчанию риск по расстоянию до SL — до 0,5% виртуального капитала, TP — 2R. "
                "Размер ограничен капиталом и правилами Bybit. Каждая пара имеет отдельный виртуальный баланс. "
                "SL размещается ниже минимума ретеста с запасом 0,25 ATR14. "
                "В готовом сценарии точные вход, SL, TP и риск смотри кнопками BTC и ETH. "
                "Плановый RR не включает комиссии, проскальзывание и гэпы; фактический убыток может быть больше планового риска.")
    if any(word in text for word in ("новост", "прогноз", "выраст", "упад", "заработ", "винрейт")):
        return ("Я объясняю текущие данные и правила бота. Новости не загружаются; "
                "будущие цены, прибыль и рыночный винрейт по этим снимкам определить нельзя. Нажми «Обзор рынка» для текущих условий.")
    assets = []
    for asset, pattern in (("BTC", r"\bbtc(?:usdt)?\b|биткоин|биток"),
                           ("ETH", r"\beth(?:usdt)?\b|ethereum|эфир")):
        if re.search(pattern, text):
            assets.append(asset)
    known_topic = any(word in text for word in ("почему", "когда", "вход", "покуп", "купи", "сигнал", "лонг", "long", "тренд", "рын", "ситуац", "обзор"))
    if not assets and not known_topic:
        return None
    if any(word in text for word in ("цен", "котиров")):
        return "Текущая цена не хранится в снимке ассистента. Я могу показать тренд и плановые уровни входа, SL и TP; это не текущая котировка."
    from .commands import describe
    states = store.states()
    if assets:
        states = [data for data in states if any(re.fullmatch(asset + r"USDT(?:\.P)?", data["symbol"].split(":", 1)[1]) for asset in assets)]
    if not states:
        return "Пока нет сохранённых данных по этим парам. Проверь доступ к Bybit и список наблюдаемых инструментов."
    blocks = ["<b>Ассистент по данным стратегии</b>\nОбзор по наблюдаемым парам, без новостей и прогноза цен."]
    for data in states[:10]:
        block = describe(data, now_ms, store.state_max_age) + "\n" + explain_snapshot(data, now_ms, store.state_max_age)
        if len("\n\n".join(blocks + [block])) > 3500:
            break
        blocks.append(block)
    return "\n\n".join(blocks)
