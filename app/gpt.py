import json
import logging
import re
from typing import Dict, Optional

import anthropic
import g4f

g4f.debug.logging = False
g4f.debug.version_check = False

logger = logging.getLogger(__name__)

MIN_BUDGET = 30000

DEVELOPER_PROFILE = """Профиль разработчика:
- Стек: Next.js, React, NestJS, Node.js, TypeScript, PostgreSQL, WebSocket, Docker, AI-интеграции (Claude API)
- Дизайн делает сам — современный, при желании с анимациями (готовый макет не нужен и не требуется)
- Цель: брать проекты от 30 000 ₽, укладываться в срок до 1 месяца, дополнительный доход 50-100к ₽/мес
- Работает с AI-ассистентом (Claude Code), что расширяет возможности на смежные задачи

ПОДХОДИТ:
- Боты (Telegram, Discord, VK, MAX) на Node.js — любая сложность
- Telegram боты на Python (aiogram, pyrogram) — выполняю с AI-помощью
- Доработка и исправление существующих ботов на Node.js и Python
- Небольшие скрипты и автоматизации на Python (как часть проекта, не единственная задача)
- API, бэкенд, fullstack, интеграции, логика с ролями и маршрутизацией
- AI-интеграции через Claude API или аналоги (OpenAI, Gemini и др.)
- Frontend если дизайн свободный или разработчик делает его сам (React/Next.js)
- Frontend на Vue.js — принимается и выполняется на React/Next.js (аналог, заказчику не принципиально)
- Платёжные системы через готовые SDK (YooMoney, Stripe, Robokassa, Telegram Payments)

НЕ ПОДХОДИТ (отклонять всегда, независимо от бюджета):
- Вёрстка по готовому чужому макету (Figma, PSD, XD, Sketch), пиксель-перфект
- CMS: Bitrix, 1C-Bitrix, WordPress, Joomla, OpenCart, Drupal, MODX, Тильда, Tilda, Wix, Shopify
- Python как основной язык для крупных проектов (Django, Flask, FastAPI как основной стек)
- Языки вне стека: PHP, Ruby, Java, C#, Go, Rust, Laravel, Yii, Symfony
- Frontend-фреймворки вне стека: Angular, Svelte, Ember (как основной)
- Мобильные приложения (iOS, Android, Flutter, React Native), нативный десктоп
- Роли: тимлид, тех.лид, CTO, управление командой, менторинг, code review как сервис
- Контент-менеджмент: наполнение сайта, перенос контента, каталог товаров, модерация, SEO-тексты
- Парсинг/скрейпинг как единственная задача без веб-интерфейса
- Таблицы Excel/Google Sheets, скрипты VBA, макросы
- 1C, ERP, CRM-кастомизация (amoCRM, Bitrix24, Salesforce)
- Unity, Unreal, gamedev
- Специфические платформы требующие глубокой экспертизы: RemnaWave, Marzban, 3x-ui, XRay, V2Ray и подобные VPN/прокси системы
- Доработка чужой legacy-кодовой базы на чужом стеке (PHP, Java и т.д.)
- "Тестовое задание", "за отзыв", "за портфолио", "стажировка" — любая бесплатная или условно-оплачиваемая работа
- "Нужна команда", "ищем команду", "соберите команду" — только соло-работа
- Срочные задачи 1-2 дня где объём явно больше (заведомо нереально)
- Задачи с NDA требующими бумажный договор / личную встречу
- Задачи с пустым описанием ("детали в личке", "скину потом", "обсудим") — только если описание совсем пустое
- Долгосрочное почасовое сотрудничество как основная форма оплаты (предпочтительно fixed-price)

БЮДЖЕТ:
- Минимум: 30 000 ₽ (допустимый максимум заказчика)
- Если ОБА значения бюджета (желаемый И допустимый) < 30 000 ₽ — НЕ ПОДХОДИТ
- Если допустимый ≥ 30 000 ₽ — анализировать задачу по существу
- Чем выше бюджет — тем интереснее (90к+ особенно приоритетны)

СРОК:
- Простая задача (бот, небольшой API, интеграция): 3-7 дней
- Средняя (fullstack, система с ролями, несколько экранов): 2-3 недели
- Крупная (сложный fullstack, большая платформа): 3-4 недели
- Максимум всегда 1 месяц — если задача явно больше месяца для одного разработчика — НЕ ПОДХОДИТ"""

FILTER_PROMPT = (
    DEVELOPER_PROFILE
    + "\n\n---\nЗаказ для анализа:\nНазвание: {title}\nОписание: {description}\nБюджет заказчика: {budget}"
    + "\n\nПРАВИЛА АНАЛИЗА:"
    + "\n1. Отклоняй при ЛЮБОМ признаке НЕ ПОДХОДИТ — даже если есть слово 'сайт' или 'разработка'."
    + "\n2. ГЛАВНОЕ: различай РАЗРАБОТКУ (писать код, создавать функционал) и НАПОЛНЕНИЕ (заполнить существующий сайт контентом)."
    + "\n   Если задача — заполнить уже готовый сайт/каталог/админку — это контент-менеджмент, false."
    + "\n3. Красные флаги контент-менеджмента (при наличии любого — сразу false):"
    + "\n   • 'наполнить/заполнить/наполнение каталога', 'добавить товары на сайт', 'разместить товары'"
    + "\n   • 'сайт донора', 'перенести с сайта', 'скопировать с сайта', 'взять с сайта'"
    + "\n   • 'проверить наличие товаров/характеристик/описаний'"
    + "\n   • 'убрать водяные знаки', 'убрать упоминания', 'обработать изображения'"
    + "\n   • 'привести в порядок группу/каталог/раздел'"
    + "\n   • 'добавить файлы документации/pdf к товарам'"
    + "\n   • 'наполненная фотогалерея', 'разместить изображения у товаров'"
    + "\n   • упоминание артикулов/серий товаров ('кмп 63', 'ост 34.10' и т.п.) без упоминания разработки"
    + "\n4. Красные флаги вёрстки по чужому макету: 'есть макет в Figma/PSD', 'сверстать по макету', 'пиксель-перфект'"
    + "\n5. Красные флаги CMS: любое упоминание Bitrix/1C-Bitrix/WordPress/Tilda/Тильда/Joomla как платформы заказчика"
    + "\n6. Если задача на платформе вне стека (PHP/Laravel/Django/Angular/Svelte) — false, даже если описание звучит как 'разработка'."
    + "\n7. Python боты (aiogram, pyrogram, telebot) — ПОДХОДИТ. Python как основной стек (Django/Flask/FastAPI проект) — НЕ ПОДХОДИТ."
    + "\n8. Если заказчик требует 'уверенное знание', 'опыт работы с конкретной платформой' где платформа специфическая"
    + "\n   и не входит в стек (RemnaWave, Marzban, 3x-ui, XRay, V2Ray, специфические VPN/прокси системы) — false."
    + "\n9. Если описание сайта-примера показывает что это сложная бизнес-система (аренда с железом, постаматы,"
    + "\n   физические устройства, специфический маркетплейс) а бюджет явно занижен — false."
    + "\n10. Если задача в серой зоне — смотри на баланс: если ≥2 пунктов профиля совпадают и нет явных red flags — true."
    + "\n    Не отклоняй только потому что описание короткое."
    + "\n\nОтветь строго JSON без markdown: "
    + '{"fit": true/false, "reason": "одно короткое предложение — конкретно что именно подошло/не подошло"}'
)

OFFER_PROMPT_CLAUDE = """Ты — опытный фрилансер (7+ лет). Пишешь отклик на заказ, который УЖЕ прошёл фильтр и подходит тебе. Задача — написать качественный, конкретный отклик.

ЖЁСТКИЕ ПРАВИЛА:
- Отклик ТОЛЬКО на подходящую задачу. Никаких формулировок вроде "эта задача не входит в мой профиль", "к сожалению не подхожу", "не мой стек" — задача УЖЕ отфильтрована как подходящая.
- Только обычный текст, без Markdown, без **, без ##, без списков через - или *
- Максимум 800 символов, минимум 200
- От первого лица (я, мой, сделаю)
- Конкретно под задачу, не шаблонно
- СТЕК ТОЛЬКО: Next.js, React, NestJS, Node.js, TypeScript, PostgreSQL, WebSocket, Docker, AI-интеграции, aiogram (для Python ботов)
- НИКОГДА не писать PHP, MySQL, Bitrix, WordPress, Laravel, Django и другие технологии вне стека
- Для Vue.js в задаче — писать что сделаю на React/Next.js (аналог, заказчику не принципиально)
- Для Python ботов — упоминать aiogram/Python как инструмент

ФОРМАТ ОТКЛИКА (строго такой порядок):

Строка 1: "Здравствуйте! Работаю на [конкретный стек под задачу из разрешённого списка], специализируюсь на [специализация под задачу]."

Строка 2-3: 1-2 предложения конкретно что сделаю и как (упомянуть ключевые технические моменты под задачу)

Строка 4 (ТОЛЬКО если в описании НЕТ готового макета/дизайна/Figma/PSD): "Дизайн разработаю сам — современный, при желании с анимациями."
(Если макет упомянут в задаче — пропустить эту строку полностью)

Строка про срок: выбери адекватный срок под сложность:
- Простая (бот, малый API, интеграция): "Срок — 3-7 дней"
- Средняя (fullstack, система с ролями, несколько экранов): "Срок — 2-3 недели"
- Крупная (большой fullstack, платформа): "Срок — 3-4 недели, максимум месяц"
НЕ ПИСАТЬ ВСЕГДА "до 1 месяца" — оценивай реально.

Строка про бюджет: "Бюджет — [X] ₽" где X — справедливая цена с учётом 20% комиссии Kwork (итог × 1.25).
Ставка ~3000-4000 ₽/час до комиссии.
- Простая 3-7 дней: 30 000 – 60 000 ₽
- Средняя 2-3 недели: 60 000 – 120 000 ₽
- Крупная 3-4 недели: 120 000 – 200 000 ₽
Если бюджет заказчика указан и он разумный — ориентируйся на верхнюю границу его допустимого, но не превышай её.
Если допустимый бюджет заказчика < твоей справедливой цены — ставь допустимый бюджет заказчика.

ТОЛЬКО если срок БОЛЬШЕ 7 дней — добавь разбивку сразу после строки бюджета:
Задача 1: [название] — [сумма] ₽
Задача 2: [название] — [сумма] ₽
Задача 3: [название] — [сумма] ₽
(сумма задач = итоговому бюджету)

Если срок 7 дней или меньше — разбивку НЕ ДОБАВЛЯТЬ ВООБЩЕ.

---
Заказ:
Название: {title}
Описание: {description}
Бюджет заказчика: {budget}"""

OFFER_PROMPT = {
    "ru": "НАПИШИ НА РУССКОМ.\n\n"
        "Проект с фриланс-биржи пойман, пора браться за него. Что предложить? До 800 символов (минимум 150).\n"
        "Представь, что ты фрилансер, хочешь заработать. Думай как фрилансер. Напиши от моего имени (я, мой, моего и т.д.).\n"
        "Опиши, как я собираюсь выполнить задание, выдели ключевые моменты. Не используй сложные слова. Не используй Markdown, просто обычный текст.\n"
        "Вкратце укажи клиенту пару подходящих (стеков, библиотек, решения, технологии) для этого проекта!\n\n"
        "Стартуй предложение услугу (с уважением и уникальный): 'Здравствуйте! Я готов ...'",

    "en": "A project from a freelance platform has been caught, it's time to take it. What can I offer? Up to 800 characters (minimum 150).\n"
        "Imagine that you are a freelancer and want to make money. Think like a freelancer. Write in my name (I, my, mine, etc.).\n"
        "Describe how I am going to complete the task, highlight the key points. Don't use difficult words. Don't use Markdown just Plain text.\n"
        "Briefly point out to the client a couple of suitable ones (stacks, libraries, solutions, technologies) for this project!\n\n"
        "Start offering a service (respectfully and uniquely): 'Hello! I'm ready ...'",
}


REFUSAL_MARKERS = (
    "не входит в мой профиль",
    "не мой профиль",
    "не мой стек",
    "не подходит мне",
    "не подхожу",
    "рекомендую обратиться",
    "обратитесь к другому",
    "это не моя сфера",
    "не моя область",
    "не берусь за",
    "не могу взяться",
    "не специализируюсь на",
    "это не входит",
)


def looks_like_refusal(offer: str) -> bool:
    """Detect if the generated offer is actually a refusal text.
    When True — filter likely erred and project should be skipped entirely."""
    if not offer:
        return False
    lower = offer.lower()
    return any(marker in lower for marker in REFUSAL_MARKERS)


HARD_REJECT_KEYWORDS = (
    # CMS / чужие платформы
    r"\b1c[\s-]*битрикс\b", r"\bбитрикс\b", r"\bbitrix\b",
    r"\bwordpress\b", r"\bвордпресс\b", r"\bwp[\s-]*сайт\b",
    r"\bтильд[аеу]\b", r"\btilda\b",
    r"\bjoomla\b", r"\bджумла\b",
    r"\bopencart\b", r"\bопенкарт\b",
    r"\bdrupal\b", r"\bshopify\b", r"\bwix\b", r"\bmodx\b",
    # Языки/фреймворки вне стека
    r"\blaravel\b", r"\byii\b", r"\bsymfony\b",
    r"\bна\s+php\b", r"\bphp[\s-]*разработчик\b",
    r"\bна\s+django\b", r"\bна\s+flask\b", r"\bна\s+fastapi\b",
    r"\bangular\b", r"\bsvelte\b",
    # Специфические VPN/прокси платформы
    r"\bremnawave\b",
    r"\bmarzban\b",
    r"\b3x[\s-]*ui\b",
    r"\bx[\s-]*ui\b",
    r"\bxray[\s-]*panel\b",
    r"\bv2ray\b",
    # Контент-менеджмент / наполнение
    r"наполнить\s+(сайт|каталог|магазин)",
    r"заполнить\s+(сайт|каталог|карточки)",
    r"наполнение\s+(сайта|каталога|магазина)",
    r"контент[\s-]*менеджер",
    r"разместить\s+товары",
    r"добавить\s+товары\s+на\s+сайт",
    r"сайт[\s-]*донор",
    r"убрать\s+водяные\s+знаки",
    r"перенос\s+контента",
    r"наполненн(ую|ая|ой)\s+фотогалере",
    # Вёрстка по готовому макету
    r"пиксель[\s-]*перфект",
    r"сверстать\s+по\s+макету",
    r"готов(ый|ая)\s+макет\s+в\s+figma",
    # Мобильная/десктоп нативная разработка
    r"\bios[\s-]*приложени",
    r"android[\s-]*приложени",
    r"\bflutter\b",
    r"\breact[\s-]*native\b",
    # 1С / ERP / CRM кастомизация
    r"\b1с[\s-]*предприятие\b", r"доработка\s+1с",
    r"\bamocrm\b", r"\bамо[\s-]*срм\b",
    # Парсинг без веб-интерфейса
    r"парсер\s+на\s+python",
    r"скрипт\s+на\s+(python|vba|excel)",
    # Бесплатная работа
    r"тестовое\s+задание",
    r"за\s+(отзыв|портфолио)",
    r"стажировк",
    # Команды / роли
    r"требуется\s+команда",
    r"ищем\s+команду",
    r"\bтим[\s-]*лид\b", r"\bтех[\s-]*лид\b",
)

_HARD_REJECT_RE = re.compile("|".join(HARD_REJECT_KEYWORDS), re.IGNORECASE)


def _hard_reject_reason(title: str, description: str) -> Optional[str]:
    """Return matched keyword if title+description triggers a hard reject, else None.
    Saves Claude tokens on obvious non-fits."""
    text = f"{title}\n{description}"
    match = _HARD_REJECT_RE.search(text)
    return match.group(0) if match else None


def _parse_budget_numbers(budget: str) -> tuple[int, int]:
    """
    Extract (желаемый, допустимый) из строки вида
    'желаемый 5,000 ₽, допустимый до 15,000 ₽'.
    Returns (0, 0) if not parseable.
    """
    numbers = re.findall(r"[\d\s,]+", budget)
    cleaned = []
    for n in numbers:
        digits = re.sub(r"[^\d]", "", n)
        if digits:
            cleaned.append(int(digits))
    if len(cleaned) >= 2:
        return cleaned[0], cleaned[1]
    if len(cleaned) == 1:
        return cleaned[0], cleaned[0]
    return 0, 0


async def filter_project(
    title: str,
    description: str,
    budget: str,
    anthropic_api_key: Optional[str],
) -> tuple[bool, str]:
    """
    Ask claude-haiku-4-5 whether this job fits the developer profile.
    Returns (fit, reason). If ANTHROPIC_API_KEY is not set, always returns (True, "").

    Hard pre-check: если оба значения бюджета < MIN_BUDGET — сразу False без вызова Claude.
    """
    if not anthropic_api_key:
        return True, ""

    wanted, limit = _parse_budget_numbers(budget)
    if wanted > 0 and limit > 0 and wanted < MIN_BUDGET and limit < MIN_BUDGET:
        reason = f"бюджет ниже минимума: {wanted:,} / {limit:,} ₽ < {MIN_BUDGET:,} ₽"
        logger.info("Filter [%s]: fit=False (hard) — %s", title[:60], reason)
        return False, reason

    hard_reject = _hard_reject_reason(title, description)
    if hard_reject:
        reason = f"ключевое слово вне профиля: '{hard_reject}'"
        logger.info("Filter [%s]: fit=False (hard) — %s", title[:60], reason)
        return False, reason

    prompt = FILTER_PROMPT.format(
        title=title,
        description=description[:1500],
        budget=budget,
    )

    try:
        client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()
        result = json.loads(raw)
        fit: bool = bool(result.get("fit", True))
        reason: str = result.get("reason", "")
        logger.info("Filter [%s]: fit=%s — %s", title[:60], fit, reason)
        return fit, reason
    except Exception as exc:
        logger.warning("Filter error for '%s': %s — passing through", title[:60], exc)
        return True, ""


async def generate_offer_claude(
    title: str,
    description: str,
    budget: str,
    anthropic_api_key: str,
) -> str:
    """Generate a short offer text using Claude. Only called for jobs that passed the filter."""
    prompt = OFFER_PROMPT_CLAUDE.format(
        title=title,
        description=description[:1500],
        budget=budget,
    )
    try:
        client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)
        message = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=600,
            messages=[{"role": "user", "content": prompt}],
        )
        return message.content[0].text.strip()
    except Exception as exc:
        logger.warning("Offer generation error for '%s': %s", title[:60], exc)
        return ""


async def generate_offer(project_description: str, lang: str) -> Dict[str, bool | str]:
    try:
        response = await g4f.ChatCompletion.create_async(
            model="gpt-3.5-turbo",
            messages=[
                {"content": OFFER_PROMPT.get(lang, "en"), "role": "system"},
                {"role": "user", "content": f"Project: {project_description}"},
            ],
        )
        return {"ok": True, "response": str(response)}
    except BaseException as e:
        # TODO: Too Long Error Message Issue
        return {"ok": False, "err": f"{e.__class__.__name__}"}