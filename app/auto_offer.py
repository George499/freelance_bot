"""Автоотправка откликов на Kwork (сентябрь 2026).

Зачем: George перестал откликаться вручную - не из-за отсутствия заказов
(за 3 недели бот прислал 28 карточек, 17 со скором >=7), а из-за
эмоциональной цены отправки в тишину. Коннекты при этом сгорают
неиспользованными (30/30 на 16.09). Бот берёт на себя первую часть воронки:
отклик уходит сам, George включается только когда заказчик ответил в личку.

Механика Kwork: мобильное API отклики отправлять НЕ умеет. Отправка идёт
через веб-сессию сайта, которая получается из мобильного токена методом
getWebAuthToken (пароль от сайта не нужен, браузер тоже).

ГЛАВНЫЙ ПРЕДОХРАНИТЕЛЬ - стек-фильтр. Проверено на живом заказе: генератор
отклика на задачу по 1С:УТ написал «я функциональный консультант с опытом
внедрений УТ 11.4 и 11.5», то есть выдумал экспертизу. При ручной отправке
George бы это не отправил, при авто - уйдёт. Поэтому отклик уходит ТОЛЬКО
на заказы из белого списка, независимо от скора.
"""

import json
import logging
import os
import re
from datetime import date, datetime

import aiohttp
from kwork import Kwork

logger = logging.getLogger(__name__)

STATE_FILE = "auto_offer.json"
DAILY_LIMIT = 3          # 30 коннектов в месяц; потолок держит квота, не этот лимит
# MIN_SCORE убран 17.09: порог теперь общий с карточкой и адаптируется
# к остатку коннектов (см. quota_status). Отдельная жёсткая планка при
# медиане 48 откликов в нише означала бы "не откликаться никогда".
MIN_SCORE = 0

# Границы слотов (UTC; МСК = +3) и планка «сильного» заказа для утра.
MORNING_UNTIL_H = 9      # до 12:00 МСК — один отклик, и только сильный
DAY_UNTIL_H = 14         # до 17:00 МСК — второй; третий бережём на вечер
STRONG_SCORE = 8
STRONG_PRICE = 30_000
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# Белый список: то, где George реально компетентен.
_STACK_OK = re.compile(
    r"\bpython\b|\bпитон\b|\bfastapi\b|\bdjango\b|\bflask\b|\baiogram\b|"
    r"\bnode\.?js\b|\bnest\w*|\btypescript\b|\breact\b|\bnext\.?js\b|"
    # Правка 20.09: боты ловятся ОДНИМ широким паттерном вместо перечисления
    # площадок. Прежние конструкции требовали, чтобы «бот» и «тг/телеграм»
    # стояли рядом и в правильном порядке, из-за чего терялись реальные GO:
    # «Создать бота автопродаж в тг» (скор 8, Haiku: «Telegram-бот класса A»)
    # и «WhatsApp-бот с ИИ» — WhatsApp вообще не был в списке.
    # Это безопасно: до стек-фильтра заказ уже прошёл hard-reject, no-code
    # детектор и скоринг, а чёрный список ниже перебивает белый.
    r"\bбот\w*|\bbot\b|чат[\s-]?бот\w*|"
    r"mini[\s-]?app|мини[\s-]?прил\w+|vk\s*api|long\s*poll|"
    r"\bapi\b|интеграц\w+|вебхук\w*|webhook|\bparser\b|парсер\w*|парсинг\w*|"
    r"postgres\w*|\bsql\b|баз[аыу]\s+данных|\bdocker\b|бэкенд|backend|"
    r"скрипт\w*\s+на\s+python|автоматизац\w+\s+на\s+python|"
    # Скрипты автоматизации и Excel-утилиты: та же работа (openpyxl,
    # python-docx, pandas). Без них терялись заказы вроде «Создание файла
    # для автоматической отчётности» (скор 7) и «Программа Excel в Word»
    # (скор 9) — Haiku их одобрял, стек-фильтр не узнавал.
    r"excel|эксель|xlsx?|csv|google\s*sheets|"
    r"гугл[\s-]?таблиц\w*|openpyxl|pandas|python[\s-]?docx|"
    r"скрипт\w*|утилит\w+|автоматизац\w+\s+отч[её]тн\w*|"
    r"автоматическ\w+\s+отч[её]тн\w*",
    re.IGNORECASE,
)

# Чёрный список: чужая профессия. Перебивает белый.
_STACK_BAD = re.compile(
    r"\b1с\b|\b1c\b|битрикс\w*|\bbitrix\w*|\bwordpress\b|вордпресс\w*|"
    r"тильд\w+|\btilda\b|\bopencart\b|\bjoomla\b|\bwix\b|\bshopify\b|\bmodx\b|"
    r"вёрстк\w+|верстк\w+|фигм\w+|\bfigma\b|дизайн\w*\s+макет|"
    r"\bios\b|\bandroid\b|\bswift\b|\bkotlin\b|\bflutter\b|"
    r"консультац\w+|консультант\w*|обучени\w+|репетитор\w*|"
    r"настройк[аиуе]\s+(?:crm|срм|amocrm|bitrix|битрикс)|интегратор\s+\w*bitrix|"
    # No-code конструкторы ботов. Их и так ловит detect_no_code_required до
    # скоринга, но здесь последний рубеж перед отправкой от имени George —
    # дублируем. n8n намеренно НЕ включён: там бывает наша работа
    # («Развертывание шины n8n на Docker + Postgres» прошло как свой стек).
    r"\bsalebot\b|сейлбот|\bmanychat\b|менichat|\bbothelp\b|\baimylogic\b|"
    r"\bchatfuel\b|\bwazzup\b|\bтекстбэк\b|\btextback\b|"
    r"\bseo\b|\bсмм\b|\bsmm\b|таргет\w+|копирайт\w+|рерайт\w+|"
    r"\bunity\b|юнити|блендер|\bblender\b|\bphp\b|\blaravel\b",
    re.IGNORECASE,
)


def is_my_stack(title: str, description: str) -> tuple[bool, str]:
    """True если заказ в зоне реальной компетенции. Чёрный список сильнее белого."""
    text = f"{title}\n{description}"
    bad = _STACK_BAD.search(text)
    if bad:
        return False, f"чужая зона: {bad.group(0)}"
    ok = _STACK_OK.search(text)
    if not ok:
        return False, "нет маркеров моего стека"
    return True, f"мой стек: {ok.group(0)}"


def _load() -> dict:
    if not os.path.exists(STATE_FILE):
        return {"enabled": False, "date": "", "sent_today": 0, "log": []}
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"enabled": False, "date": "", "sent_today": 0, "log": []}


def _save(state: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def get_state() -> dict:
    """Состояние с автосбросом дневного счётчика."""
    state = _load()
    today = date.today().isoformat()
    if state.get("date") != today:
        state["date"] = today
        state["sent_today"] = 0
        _save(state)
    return state


def is_auto_enabled() -> bool:
    return bool(get_state().get("enabled"))


def set_auto(enabled: bool) -> dict:
    state = get_state()
    state["enabled"] = bool(enabled)
    _save(state)
    return state


def can_send_today() -> bool:
    return get_state().get("sent_today", 0) < DAILY_LIMIT


def can_send_now(score: int, price: int) -> tuple[bool, str]:
    """Резерв по времени суток: не выгребать дневной лимит с утра.

    Правка 21.09. Наблюдение George подтвердилось логами: бот тратил все три
    отклика до обеда, а вечерние заказы пропускались с «дневной лимит
    исчерпан».
      21.09: 06:39 (7к) → 09:00 (20к) → 09:39 (110к), и в 15:39 пропущен
             «Поставить бота на сервер»
      20.09: 11:10 (7к) → 11:30 (45к) → 12:39 (5к), и в 16:29 пропущен
             «Проект по веб программированию»
    Заказы идут весь день (пик в полдень и около 20:00 МСК), поэтому ранний
    слот тратим только на явно сильный заказ, остальные бережём.

    Время серверное (UTC), МСК = +3.
    """
    hour = datetime.now().hour
    sent = get_state().get("sent_today", 0)
    if sent >= DAILY_LIMIT:
        return False, f"дневной лимит {DAILY_LIMIT} исчерпан"

    if hour < MORNING_UNTIL_H:                      # до 12:00 МСК
        if sent >= 1:
            return False, "утренний резерв: до полудня не больше одного"
        if score < STRONG_SCORE and price < STRONG_PRICE:
            return False, (
                f"утро: слишком слабый для раннего слота "
                f"(скор {score} < {STRONG_SCORE} и {price} ₽ < {STRONG_PRICE} ₽)"
            )
    elif hour < DAY_UNTIL_H:                        # до 17:00 МСК
        if sent >= 2:
            return False, "дневной резерв: третий отклик оставляем на вечер"
    return True, ""


def register_sent(project_id, title: str, price: int, text: str = "") -> dict:
    """Записать отправку. Текст сохраняем: Telegram Bot API не даёт боту
    читать свою же историю, и без этого разобрать «почему нет ответов»
    можно только попросив George переслать сообщение вручную.
    """
    state = get_state()
    state["sent_today"] = state.get("sent_today", 0) + 1
    state.setdefault("log", []).insert(0, {
        "id": str(project_id), "title": title[:70],
        "price": price, "at": date.today().isoformat(),
        "text": (text or "")[:900],
    })
    state["log"] = state["log"][:50]
    _save(state)
    return state


def duration_for(price: int) -> int:
    """Срок в днях от размера бюджета. Переобещать дешевле не выйдет."""
    if price < 10_000:
        return 3
    if price < 30_000:
        return 7
    return 14


async def send_offer(
    config, project_id, description: str, price: int, title: str,
    days: int | None = None,
) -> tuple[bool, str]:
    """Отправить отклик через веб-сессию Kwork.

    Возвращает (успех, сообщение). Веб-сессия берётся из мобильного токена
    через getWebAuthToken - пароль от сайта и браузер не нужны.
    """
    kwork = Kwork(
        login=config.kw_login,
        password=config.kw_password,
        phone_last=config.kw_phone_last,
    )
    try:
        token = await kwork.token
        redirect = f"/new_offer?project={project_id}"
        resp = await kwork.api_request(
            method="post", api_method="getWebAuthToken",
            token=token, url_to_redirect=redirect,
        )
        payload = resp.get("response") or resp
        login_url = payload.get("url") if isinstance(payload, dict) else None
    except Exception as exc:
        return False, f"getWebAuthToken: {exc}"
    finally:
        await kwork.close()

    if not login_url:
        return False, "не получен login_url"

    jar = aiohttp.CookieJar(unsafe=True)
    async with aiohttp.ClientSession(cookie_jar=jar, headers={"User-Agent": UA}) as s:
        offer_page = f"https://kwork.ru/new_offer?project={project_id}"
        try:
            async with s.get(login_url, allow_redirects=True):
                pass
            async with s.get(offer_page, allow_redirects=True) as r:
                html = await r.text()
                if r.status != 200:
                    return False, f"страница отклика HTTP {r.status}"
        except Exception as exc:
            return False, f"веб-сессия: {exc}"

        csrf = next((c.value for c in jar if c.key == "csrf_user_token"), None)
        if not csrf:
            m = re.search(
                r"csrf[_-]?user[_-]?token[\"']?\s*[:=]\s*[\"']([A-Za-z0-9_\-=]{8,})",
                html, re.I,
            )
            csrf = m.group(1) if m else None
        if not csrf:
            return False, "не найден csrf_user_token"

        form = aiohttp.FormData()
        form.add_field("wantId", str(project_id))
        form.add_field("offerType", "custom")
        form.add_field("description", description)
        form.add_field("kwork_duration", str(days or duration_for(price)))
        form.add_field("kwork_price", str(price))
        form.add_field("kwork_name", (title or "Разработка")[:60])

        headers = {
            "X-Requested-With": "XMLHttpRequest",
            "X-CSRF-Token": csrf,
            "Origin": "https://kwork.ru",
            "Referer": offer_page,
            "Accept": "application/json, text/plain, */*",
        }
        try:
            async with s.post(
                "https://kwork.ru/api/offer/createoffer",
                data=form, headers=headers,
            ) as r:
                body = await r.text()
                if r.status != 200:
                    return False, f"HTTP {r.status}: {body[:160]}"
                try:
                    data = json.loads(body)
                except json.JSONDecodeError:
                    return False, f"не JSON: {body[:160]}"
                if data.get("success"):
                    return True, "отправлен"
                return False, f"отказ Kwork: {str(data.get('error') or data)[:160]}"
        except Exception as exc:
            return False, f"POST createoffer: {exc}"
