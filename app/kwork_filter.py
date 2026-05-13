"""
Kwork filter v3.

Изменения vs предыдущей версии:
- quota_status принимает days_until_refill (rolling-период Kwork) вместо дня месяца
- No-code платформы (n8n/Make/Zapier) — жёсткий стоп-фактор
- Scope red flags: "ТЗ в ЛС", "универсальный агент"
- Open-ended scope: "будущие задачи"
- Технически безграмотный заказчик
- Минимальный порог скора строго 7
- Early-period (<20% прошло) порог 8
"""

import asyncio
import json
import logging
import re
from typing import Dict, Optional

import anthropic

logger = logging.getLogger(__name__)

MONTHLY_QUOTA = 30
MIN_SCORE_FOR_RESPONSE = 7
DAILY_SOFT_LIMIT = 2
RESERVE_QUOTA_FOR_LAST_DAYS = 7

MIN_BUDGET_GENERAL = 50000
MIN_BUDGET_AI = 40000
BUDGET_SINGLE_THRESHOLD = 40000

DEVELOPER_PROFILE = """Профиль разработчика:

Стек: Next.js, React, NestJS, Node.js, TypeScript, PostgreSQL, WebSocket/Socket.IO, Docker, AI (Claude API, OpenAI, RAG, MCP).

Опыт: fullstack-разработка, основной фокус — AI-интеграции.
Цель: 3-5 заказов в период, чеки 50к+.
Лимит Kwork: 30 откликов на период.

ДИФФЕРЕНЦИАТОР: AI на собственном коде (Claude API, OpenAI, агенты на function calling, RAG, MCP), НЕ no-code конструкторы.

СИЛЬНЫЕ СТОРОНЫ:
- AI-интеграции в собственном коде
- Чат-боты с AI-контекстом и памятью
- Telegram/Discord/VK боты на Python (aiogram/pyrogram) — это основной инструмент, НЕ серая зона
- Парсеры сайтов/маркетплейсов (Wildberries, Ozon и т.п.) на Python с aiogram-фронтом — прямое попадание
- Real-time на Socket.IO
- Админки, дашборды, личные кабинеты с RBAC
- Fullstack с нуля
- Excel/CSV отчёты и интеграции (pandas, openpyxl)
- САЙТ/ЛЕНДИНГ ПОД КЛЮЧ с собственным дизайном (Next.js + Tailwind, дизайн делается в Claude Design/Figma) — это УТП когда у заказчика НЕТ готового макета и он хочет "придумайте и сделайте"

НЕТ В СТЕКЕ (no-code):
- n8n, Make.com, Zapier, Bubble, Airtable Automations, Retool

ПОДХОДИТ:
- Telegram/Discord/VK боты (Node.js/TypeScript или Python/aiogram) — ПРЯМОЕ попадание
- REST API, бэкенд, интеграции
- Парсеры с обработкой/интерфейсом при 40к+
- Простые RN/Flutter при 80к+
- Сайты/лендинги ПОД КЛЮЧ когда макета НЕТ и заказчик готов к собственному дизайну разработчика

ТРИ КАТЕГОРИИ САЙТОВ/ЛЕНДИНГОВ — РАЗЛИЧАТЬ СТРОГО:
1. Верстка по готовому макету (Figma/PSD/XD есть) — НЕ ПОДХОДИТ, это не моё.
   Маркеры: "есть макет в Figma", "готовый PSD", "сверстать по дизайну", "пиксель-перфект".
2. Чистый дизайн без кода — НЕ ПОДХОДИТ, это для дизайнеров.
   Маркеры: "нужен дизайнер", "опыт FMCG", "трендовый дизайн", "концепт бренда",
   "Behance-портфолио", "презентация дизайна".
3. Сайт/лендинг ПОД КЛЮЧ без готового макета — ПОДХОДИТ, это УТП.
   Маркеры: "сделать сайт с нуля", "разработать лендинг", "под ключ", "дизайн и разработка",
   "нет готовых макетов", "придумайте и сделайте", отсутствие упоминания Figma/PSD.

ВАЖНО ПРО СРОКИ KWORK:
На Kwork поле "срок" означает СРОК ПОДАЧИ ЗАЯВОК (через сколько дней заказ закроется),
а НЕ срок исполнения. "1-2 дня" значит только что заказчик спешит найти исполнителя,
реальный срок исполнения обсуждается отдельно. НЕ штрафовать скор за "короткий срок"
если задача явно требует 2-3 недели работы — это просто означает что скоро закроют приём заявок.

НЕ ПОДХОДИТ:
- CMS (Bitrix/WordPress/Tilda/Joomla/OpenCart/Shopify/Wix)
- No-code платформы
- Пиксель-перфект по чужому макету
- PHP/Laravel/Django/Flask/Angular/Svelte
- 1С/amoCRM/Bitrix24 кастомизация
- VPN-панели (RemnaWave/Marzban/3x-ui/XRay)
- Контент-менеджмент, тимлид, команды, gamedev
"""

AI_PRIORITY_KEYWORDS = (
    r"\bclaude\b", r"\bgpt\b", r"\bopenai\b", r"\banthropic\b",
    r"\bllm\b", r"\bchatgpt\b", r"\bgemini\b", r"\bllama\b",
    r"\brag\b", r"\blangchain\b",
    r"\bmcp[\s-]*сервер\b", r"\bmcp\b",
    r"\bembeddings?\b", r"\bэмбеддинг",
    r"\bвекторн(ая|ую|ой)\s+(бд|база)\b",
    r"\bpinecone\b", r"\bqdrant\b", r"\bweaviate\b", r"\bchroma\b",
    r"\bai[\s-]*агент", r"\bии[\s-]*агент",
    r"\bнейросет", r"\bискусственн(ый|ого)\s+интеллект",
    r"\bgenai\b",
)
_AI_PRIORITY_RE = re.compile("|".join(AI_PRIORITY_KEYWORDS), re.IGNORECASE)

NO_CODE_PLATFORM_KEYWORDS = (
    r"\bn8n\b",
    r"настроить\s+(в|через)\s+n8n",
    r"воркфлоу\s+в\s+n8n",
    r"workflow\s+в\s+n8n",
    r"\bmake\.com\b", r"\bintegromat\b",
    r"настроить\s+(в|через)\s+make",
    r"\bzapier\b", r"настроить\s+(в|через)\s+zapier", r"\bзапиер\b",
    r"\bbubble\.io\b", r"\bна\s+bubble\b",
    r"airtable\s+automation",
    r"\bretool\b",
    # P0.3: явные no-code формулировки
    r"\bno[\s-]?code\b", r"\blow[\s-]?code\b",
    r"\bноу[\s-]?код\b", r"\bлоу[\s-]?код\b",
    r"без\s+написания\s+кода",
    r"без\s+программирования",
    r"визуальн\w+\s+конструктор",
    r"автоматизаци\w+\s+на\s+(make|n8n|zapier)",
)
_NO_CODE_RE = re.compile("|".join(NO_CODE_PLATFORM_KEYWORDS), re.IGNORECASE)

# P0.3 edge case: миграция С no-code НА код — НЕ hard-reject, нормальный скоринг
# Явный сигнал миграции (один из этих паттернов достаточно)
NO_CODE_MIGRATION_FLAGS = (
    r"перепис(ать|ыва|али)",
    r"переехать", r"переезжае?м", r"переезд\s+с",
    r"уйти\s+от", r"уход\s+от", r"уходим?\s+от",
    r"отказ\w+\s+от\s+(no[\s-]?code|n8n|make|zapier|bubble|retool)",
    r"заменить\s+(n8n|make|zapier|bubble)",
    r"вместо\s+(n8n|make|zapier|bubble)",
    r"мигрир\w+\s+с",
    r"переход\s+(с|от)\s+(no[\s-]?code|n8n|make|zapier)",
    r"раньше\s+(был[оа]?|работал[оа]?)\s+на\s+(n8n|make|zapier)",
    r"написать\s+(заново|с\s+нуля)\s+на\s+(python|nest|next|node)",
)
_NO_CODE_MIGRATION_RE = re.compile("|".join(NO_CODE_MIGRATION_FLAGS), re.IGNORECASE)

# Текущее состояние на no-code (нужен parner-сигнал — цель на коде)
NO_CODE_CURRENT_STATE_FLAGS = (
    r"использу\w+\s+(n8n|make|zapier|bubble)",
    r"работает\s+на\s+(n8n|make|zapier|bubble)",
    r"сейчас\s+(на\s+|использу\w+\s+|работае?т\s+на\s+)?(n8n|make|zapier|bubble)",
    r"уже\s+(есть|сделан\w*|настроен\w*)\s+(на\s+)?(n8n|make|zapier|bubble)",
)
_NO_CODE_CURRENT_RE = re.compile("|".join(NO_CODE_CURRENT_STATE_FLAGS), re.IGNORECASE)

# Цель миграции — новый сервис на коде
CODE_TARGET_FLAGS = (
    r"новый\s+(сервис|проект|бэкенд|фронт|систем\w*|сайт|приложен\w*)\s+на\s+(python|nest|next|node|django|fastapi|typescript|nestjs|nextjs|go\b)",
    r"нужен\s+(сервис|проект|бэкенд|сайт|систем\w*)\s+на\s+(python|nest|next|node|nestjs|nextjs|django|fastapi)",
    r"переписать\s+на\s+(python|nest|next|node)",
)
_CODE_TARGET_RE = re.compile("|".join(CODE_TARGET_FLAGS), re.IGNORECASE)

SCOPE_RED_FLAGS = (
    r"универсальн(ый|ая|ое)\s+(агент|систем|решени|бот)",
    r"под\s+любые\s+задачи",
    r"будет\s+выполнять\s+любые",
    r"ставить\s+задачи\s+и\s+он\s+будет",
    r"самостоятельно\s+(делать|выполнять|решать)\s+задачи",
    r"полное\s+т[зз]\s+(в\s+)?(лс|личк|личны|после)",
    r"подробност[ия]\s+(в\s+)?(лс|личк|после|скину)",
    r"детали\s+(в\s+)?(лс|личк|после|обсудим)",
    r"расскажу\s+(в\s+)?(лс|личк|после)",
    r"скину\s+т[зз]\s+в\s+лс",
    r"допустим,?\s+(сейчас|сегодня)\s+есть\s+задача",
    r"например,?\s+(нужно|сейчас)\s+",
    r"похож(ее|ий)\s+на\s+(chatgpt|claude|n8n|make|zapier)\s+но\s+свой",
    r"свой\s+(chatgpt|claude|gpt)",
    r"бюджет\s+(обсуждаемый|уточним|определим)\s+после",
    r"обязательно\s+(созвониться|созвон|звонок)",
    r"нужен\s+(живой\s+разговор|обязательный\s+звонок)",
    r"только\s+(по\s+)?(звонку|телефону|созвону)",
    r"без\s+созвона\s+не",
    r"телемост",
)
_SCOPE_RED_FLAGS_RE = re.compile("|".join(SCOPE_RED_FLAGS), re.IGNORECASE)

OPEN_ENDED_SCOPE_FLAGS = (
    r"будущие\s+задачи",
    r"в\s+дальнейшем\s+(добавим|расширим)",
    r"потом\s+(добавим|расширим|обсудим)",
    r"далее\s+будем?\s+(добавлять|расширять)",
    r"это\s+(только\s+)?(первый\s+)?этап",
    r"много\s+задач\s+(в\s+)?(будущем|перспективе)",
    r"чем\s+больше.*тем\s+больше\s+задач",
    r"постоянн(ое|ая)\s+(сотрудничество|развитие)",
    r"планируется\s+развитие",
)
_OPEN_ENDED_RE = re.compile("|".join(OPEN_ENDED_SCOPE_FLAGS), re.IGNORECASE)

TECH_INCOMPETENCE_FLAGS = (
    r"запрос(ов|ы)\s+в\s+(gemini|алиса|claude|chatgpt)",
    r"(gemini|алиса|claude|chatgpt)\s+как\s+поисков",
    r"прост(ая|о)\s+(сделать|написать)\s+(нейросет|ии[\s-]*агент|llm|rag)",
    r"искусственн(ый|ого)\s+интеллект\s+за\s+(\d+\s+)?(день|дня|дней)",
    r"полностью\s+автономн(ый|ая)\s+систем",
    r"заменит\s+(целый\s+)?(отдел|сотрудник|менеджер)",
    r"найди(те)?\s+шаблон\s+в\s+n8n",
    r"возьми(те)?\s+готовое\s+решение",
)
_TECH_INCOMPETENCE_RE = re.compile("|".join(TECH_INCOMPETENCE_FLAGS), re.IGNORECASE)


# === Детекция категории сайта/лендинга ===

# Категория 1: готовый макет есть → НЕ моё (верстка чужого дизайна)
READY_MOCKUP_FLAGS = (
    r"есть\s+макет\s+в\s+(figma|фигм|psd|sketch|xd)",
    r"готов(ый|ая|ые)\s+макет",
    r"макет(ы)?\s+в\s+(figma|фигм|psd)",
    r"(figma|фигма|psd)\s+макет",
    r"сверстать\s+по\s+(макету|дизайну)",
    r"пиксель[\s-]*перфект",
    r"pixel[\s-]*perfect",
    r"дизайн\s+(уже\s+)?(есть|готов)",
    r"по\s+готовому\s+макету",
    r"прикрепил(а|и)?\s+(figma|макет|psd)",
    r"ссылк(а|у)\s+на\s+figma",
)
_READY_MOCKUP_RE = re.compile("|".join(READY_MOCKUP_FLAGS), re.IGNORECASE)

# Категория 2: чистый дизайн без кода → НЕ моё (нужен дизайнер)
PURE_DESIGN_FLAGS = (
    r"нужен\s+дизайнер",
    r"ищ(ем|у|ите)\s+дизайнер",
    r"требуется\s+дизайнер",
    r"\bfmcg\b",
    r"опыт\s+(в\s+)?(fmcg|брендинг|fashion|lifestyle)",
    r"дизайнерск(ий|ая|ое)",
    r"концепт(\s+|\-)(бренда|страниц|дизайн|лендинг)",
    r"трендов(ые|ый|ой)\s+(страниц|дизайн|лендинг|сайт)",
    r"актуальн(ый|ое|ая)\s+мировой",
    r"свежий\s+взгляд\s+на\s+(дизайн|тренд)",
    r"портфолио\s+(на\s+)?behance",
    r"арт[\s-]*директор",
    r"визуальн(ая|ый|ые)\s+(концепци|стиль|идентика)",
    r"айдентика",
    r"брендинг",
    r"минималистичн(ый|ая|ое)\s+(лендинг|страниц|дизайн|сайт)",
    r"design(er)?\s+landing",
)
_PURE_DESIGN_RE = re.compile("|".join(PURE_DESIGN_FLAGS), re.IGNORECASE)

# Категория 3: под ключ с собственным дизайном → МОЁ УТП
TURNKEY_SITE_FLAGS = (
    r"под\s+ключ",
    r"с\s+нуля\s+(сайт|лендинг)",
    r"нет\s+(готов(ого|ых))?\s+макет",
    r"без\s+макет",
    r"придумайте\s+и\s+сделайте",
    r"разработать\s+(и\s+)?(сайт|лендинг)",
    r"создать\s+(сайт|лендинг)\s+(для|с)",
    r"разработка\s+(сайта|лендинга)",
    r"дизайн\s+и\s+разработка",
)
_TURNKEY_SITE_RE = re.compile("|".join(TURNKEY_SITE_FLAGS), re.IGNORECASE)

# Маркер что задача вообще про сайт/лендинг (чтобы не зря проверять остальное)
SITE_CONTEXT_FLAGS = (
    r"\bсайт", r"\bлендинг", r"landing[\s-]?page",
    r"одностраничн", r"многостраничн", r"веб[\s-]*страниц",
)
_SITE_CONTEXT_RE = re.compile("|".join(SITE_CONTEXT_FLAGS), re.IGNORECASE)


def detect_site_category(title: str, description: str) -> tuple[str, str]:
    """
    Определить категорию сайт/лендинг-заказа.

    Returns:
        (category, explanation):
        - "not_site": не про сайт/лендинг вообще
        - "ready_mockup": есть готовый макет → НЕ моё
        - "pure_design": чистый дизайн без кода → НЕ моё
        - "turnkey": под ключ с собственным дизайном → МОЁ УТП
        - "ambiguous": про сайт, но категория не определена — Claude решит
    """
    text = f"{title}\n{description}"

    if not _SITE_CONTEXT_RE.search(text):
        return "not_site", ""

    if _READY_MOCKUP_RE.search(text):
        m = _READY_MOCKUP_RE.search(text)
        return "ready_mockup", f"есть макет: '{m.group(0)}'"

    if _PURE_DESIGN_RE.search(text):
        m = _PURE_DESIGN_RE.search(text)
        return "pure_design", f"чистый дизайн: '{m.group(0)}'"

    if _TURNKEY_SITE_RE.search(text):
        m = _TURNKEY_SITE_RE.search(text)
        return "turnkey", f"под ключ: '{m.group(0)}'"

    return "ambiguous", "про сайт, но категория не определена"


# === P1.2: инфобиз / AI-агентство — универсальный hard-reject (даже для AI-заказов) ===
# Применяется отдельно от HARD_REJECT_KEYWORDS, который AI-заказы пропускает.
ALWAYS_HARD_REJECT_KEYWORDS = (
    r"\bинфобиз",
    r"\bинфопродукт",
    r"автоматизаци\w+\s+инфо\w+",
    r"\bии[\s-]*агентств",
    r"\bai[\s-]*агентств",
    r"\bии[\s-]*сотрудник",
    r"\bai[\s-]*сотрудник",
    r"автоматизировать\s+вс[её]\s+(с\s+помощью\s+)?(ии|ai|нейросет)",
    r"полная\s+автоматизация\s+бизнеса",
)
_ALWAYS_HARD_REJECT_RE = re.compile("|".join(ALWAYS_HARD_REJECT_KEYWORDS), re.IGNORECASE)


# === P1.1: лендинги для B2C-услуг — Tilda-территория ===
LANDING_KEYWORDS = (
    r"\bлендинг", r"\blanding\b", r"\bпосадочн",
    r"\bодностраничн", r"\bодностраничник",
    r"сайт[\s-]?визитк",
    r"продающ\w+\s+(сайт|landing|лендинг)",
    r"landing[\s-]?page",
)
_LANDING_RE = re.compile("|".join(LANDING_KEYWORDS), re.IGNORECASE)

B2C_SERVICE_NICHES = (
    r"\bтренер", r"\bфитнес", r"\bйога",
    r"\bпсихолог", r"\bкоуч\b", r"\bнаставник",
    r"\bюрист\b", r"\bадвокат", r"\bнотариус",
    r"\bдезинфек", r"\bуборк", r"\bклининг",
    r"мастер\s+маникюра", r"мастер\s+педикюра", r"мастер\s+бровей",
    r"\bпарикмахер", r"\bвизажист", r"\bстилист\b",
    r"\bстоматолог", r"\bмассажист", r"\bостеопат",
    r"\bрепетитор", r"\bучитель\b",
    r"\bфлорист", r"\bкондитер", r"\bбариста",
    r"\bавтосервис", r"ремонт\s+авто",
    r"\bнутрициолог", r"\bдиетолог",
    r"\bастролог", r"\bтаролог",
    r"\bсалон\s+красот",
)
_B2C_NICHES_RE = re.compile("|".join(B2C_SERVICE_NICHES), re.IGNORECASE)


# === P1.3: внешние API с барьером входа (TikTok / Instagram / WhatsApp / LinkedIn) ===
EXTERNAL_API_BARRIER_FLAGS = (
    r"tiktok\s+(business\s+)?(api|для\s+разработ)",
    r"tiktok\s+for\s+developers",
    r"instagram\s+(graph|business)\s+api",
    r"whatsapp\s+(business\s+)?(cloud\s+)?api",
    r"linkedin\s+(marketing|sales\s+navigator)\s+api",
    r"подключить\s+(tiktok|instagram|whatsapp|linkedin)\s+api",
    r"api\s+соц[\s-]?сет\w+\s+(для|чтобы|с\s+цель)",
)
_EXTERNAL_API_RE = re.compile("|".join(EXTERNAL_API_BARRIER_FLAGS), re.IGNORECASE)

EXTERNAL_API_APPROVED_FLAGS = (
    r"уже\s+(есть|одобрен\w*)\s+(api|приложен|ключ|access)",
    r"у\s+нас\s+есть\s+(api|приложен|access\s+token|ключ)",
    r"одобренное\s+приложен",
    r"api[\s-]?ключ\s+(уже\s+)?(есть|получ)",
    r"наше\s+приложение\s+уже",
    r"мы\s+уже\s+зарегистрир",
)
_EXTERNAL_API_OK_RE = re.compile("|".join(EXTERNAL_API_APPROVED_FLAGS), re.IGNORECASE)


# === P1.4: парсинг чужих коммерческих источников для построения каталога ===
COMMERCIAL_PARSING_FLAGS = (
    r"парс\w+\s+\d+\s*[-—]?\s*\d*\s+(сайт|источник|маркетплейс|конкурент|производит)",
    r"сбор\s+данных\s+с\s+\d+\s+(сайт|источник|маркетплейс)",
    r"парс\w+\s+(getcourse|skillbox|skill[\s-]?factory|нетолог|синергия|geekbrains)",
    r"(собрать|построить)\s+(собственн\w+\s+)?каталог.*?(парс|спарс)",
    r"спарсить.*?(собрать|построить).*(каталог|витрин|маркетплейс)",
    r"парс\w+.*?конкурент.*?(собрать|построить|витрин|каталог)",
)
_COMMERCIAL_PARSING_RE = re.compile("|".join(COMMERCIAL_PARSING_FLAGS), re.IGNORECASE)

PARSING_WHITELIST_FLAGS = (
    r"\bhh\.ru\b|хэдхантер|headhunter",
    r"\bгосуслуг|\.gov\.ru|\bросстат",
    r"открытые\s+данные",
    r"для\s+(собственн|нашего|своего)\s+(магазин|анализ|мониторинг|сайт)",
    r"для\s+(внутренн|собственн)\w*\s+аналитик",
    r"мониторинг\s+цен\s+конкурент",
    r"новостн\w+\s+(агрегат|парс)",
)
_PARSING_OK_RE = re.compile("|".join(PARSING_WHITELIST_FLAGS), re.IGNORECASE)


# === P2.5: НКО / благотворительные фонды — caution-флаг ===
NKO_KEYWORDS = (
    r"благотворительн\w+\s+фонд",
    r"\bнко\b", r"некоммерческ\w+\s+организаци",
    r"социальн\w+\s+проект",
    r"волонт[её]рск",
    r"общественн\w+\s+организаци",
    r"\bфонд\s+помощи",
)
_NKO_RE = re.compile("|".join(NKO_KEYWORDS), re.IGNORECASE)


# === P2.4: критичные неизвестные для GO/QUALI/SKIP роутинга ===
# Размытый бюджет (текстовые маркеры, дополнительно к price=0/limit=0)
VAGUE_BUDGET_FLAGS = (
    r"договорн\w+\s+(бюджет|оплат|цен)",
    r"бюджет\s+(договорн|обсуждаем|уточн|определим|после|зависит)",
    r"пишите\s+(свои\s+)?(цен|предложен|стоимост)",
    r"оценк\w+\s+(по\s+)?(тз|описан|задач)",
    r"сколько\s+(стои|просите)",
    r"свою\s+(цен|стоимост)\s+(в\s+)?отклик",
    r"за\s+сколько\s+готовы",
)
_VAGUE_BUDGET_RE = re.compile("|".join(VAGUE_BUDGET_FLAGS), re.IGNORECASE)

# Размытая ключевая технология
VAGUE_TECH_FLAGS = (
    r"как\w+[\s-]?(нибудь|то)\s+(cms|cms-к\w+|платформ\w*)",
    r"как\w+[\s-]?(нибудь|то)\s+соц[\s-]?сет",
    r"любая\s+(cms|платформ|технологи|бд|база)",
    r"можно\s+(на\s+)?(любой|любом)\s+(стек|язык|фреймворк)",
    r"\bна\s+любой\s+cms\b",
    r"подберит[ея]\s+(сами|сам|стек|технологи)",
)
_VAGUE_TECH_RE = re.compile("|".join(VAGUE_TECH_FLAGS), re.IGNORECASE)

# Срок исполнения не указан (не путать со сроком приёма откликов на Kwork)
VAGUE_DEADLINE_FLAGS = (
    r"срок\s+(исполнения|реализаци|разработк)\s+(уточн|обсуд|определим|зависит)",
    r"когда\s+будет\s+готово\s+обсудим",
    r"реальн\w+\s+срок\s+(уточн|обсуд)",
    r"срок\s+обсужда\w+\s+отдельн",
)
_VAGUE_DEADLINE_RE = re.compile("|".join(VAGUE_DEADLINE_FLAGS), re.IGNORECASE)

# P2.2 (минимум): ТЗ / детали во вложении — kwork-library не отдаёт attachments,
# поэтому ловим только текстовые намёки. Поднимает QUALI-флаг и сигналит что
# скоринг неполный — нужно открыть страницу заказа и прочитать вложение вручную.
ATTACHMENT_HINT_FLAGS = (
    r"\b(тз|техзадани\w+|задани\w+|описани\w+|подробн\w+|детал\w+)\s+(во?|на)\s+(вложен|файл\w*|pdf|docx?|word|архив|документ)",
    r"\bво?\s+вложени(и|е|ях)\b",
    r"\bсм\.?\s*(во\s+)?вложен",
    r"\bсмотри(те)?\s+(во\s+)?вложен",
    r"\b(прикреп\w+|прилож\w+|вложил\w*|приклад\w+)\s+(файл|архив|документ|тз|техзадани|pdf|docx?|word|excel|материал)",
    r"\bфайл\s+с\s+(тз|техзадани|описани|задани|подробн)",
    r"\b(тз|техзадани\w+|задани\w+)\s+(в\s+)?(pdf|docx?|word|excel)",
    r"\bархив\s+с\s+(тз|материал|примерами|задани|документ)",
    r"\bскача(й|йте|ть)\s+(тз|файл|архив|документ)",
    r"\b(подробн\w+\s+тз|полное\s+тз)\s+(в\s+|во\s+)?(файл|вложен|pdf|docx?|документ|архив)",
)
_ATTACHMENT_HINT_RE = re.compile("|".join(ATTACHMENT_HINT_FLAGS), re.IGNORECASE)


# === P0.1: серая / чёрная зона — обход политик платформ ===

# Whitelist легитимных применений (антифрод, тесты собственного сайта, парсинг
# открытых данных, прокси для одного аккаунта). Если совпало — grey-zone детектор
# возвращает pass даже при наличии grey-маркеров.
LEGITIMATE_USE_FLAGS = (
    r"\bhh\.ru\b|хэдхантер|headhunter",
    r"\bгосуслуг|\.gov\.ru|\bросстат",
    r"открытые\s+данные",
    r"для\s+(собственн|нашего|своего)\s+(магазин|анализ|мониторинг|сайт|api|сервис|проект)",
    r"для\s+(внутренн|собственн)\w*\s+аналитик",
    r"мониторинг\s+цен\s+конкурент",
    r"новостн\w+\s+(агрегат|парс)",
    r"тестировани\w+\s+(нашего|собственн|своего|нашей)\s+(сайт|приложен|api|систем)",
    r"антифрод|защит\w+\s+(собственн|нашего|нашей)\s+(сайт|продукт|проект|систем)",
    r"\bодин\s+(аккаунт|пользоват)",
    r"наш\w*\s+(собственн\w+\s+)?(сайт|api|сервис|проект|интернет[\s-]?магазин|корпоративн\w+\s+портал)",
)
_LEGITIMATE_USE_RE = re.compile("|".join(LEGITIMATE_USE_FLAGS), re.IGNORECASE)


# Группа 1: архитектурный обход (telegram не видит, воркеры-прокси, скрытая связь).
GREY_ARCHITECTURE_FLAGS = (
    r"telegram\s+не\s+видит",
    r"платформа\s+не\s+видит",
    r"\bне\s+(?:увидит|обнаружит|спалит|запалит|отследит)\b",
    r"невозможно\s+отследить",
    r"скрыт\w+\s+связь",
    r"воркер\w*\s+(?:на\s+)?(?:хостинг|прокси)",
    r"бот\w*[\s\S]{0,80}?прокси[\s\S]{0,80}?воркер",
    r"парсер\w*[\s\S]{0,40}?участник\w+\s+(?:каналов|чатов|групп)",
    r"подпис\w+\s+запросов\s+(?:timestamp|nonce)",
)
_GREY_ARCH_RE = re.compile("|".join(GREY_ARCHITECTURE_FLAGS), re.IGNORECASE)

# Группа 2: mass-action автоматизация (база аккаунтов, антидетект, эмуляция).
MASS_ACTION_FLAGS = (
    r"баз[аы]\s+аккаунтов",
    r"мно[жг]еств\w+\s+аккаунт",
    r"\bпрокси\b[\s\S]{0,80}?\bаккаунт",
    r"\bаккаунт\w*[\s\S]{0,80}?\bпрокси\b",
    r"антидетект",
    r"автоматическ\w+\s+авторизац\w+\s+(?:на\s+)?сайт",
    r"эмуляц\w+\s+(?:браузера|пользоват)",
    r"selenium[\s\S]{0,80}?автоматизац",
    r"playwright[\s\S]{0,80}?автоматизац",
    r"добавлен\w+\s+(?:позиций|товаров|корзин)",
    r"клик[аи]\s+автоматизированн",
    r"автоклик\w+",
)
_MASS_ACTION_RE = re.compile("|".join(MASS_ACTION_FLAGS), re.IGNORECASE)

# Группа 3: обход защиты (капча-сервисы, Cloudflare, "чтобы не банили").
ANTI_PROTECTION_FLAGS = (
    r"\brucaptcha\b",
    r"\banti[\s-]?captcha\b",
    r"\b2captcha\b",
    r"capmonster",
    r"обход\w*\s+(?:капчи|cloudflare|защит|блокировок)",
    r"защита\s+от\s+(?:блокировок|банов)",
    r"чтобы\s+не\s+(?:банили|палили)",
    r"тайминг\w*\s+(?:для\s+)?(?:незаметн|естественн)",
)
_ANTI_PROTECTION_RE = re.compile("|".join(ANTI_PROTECTION_FLAGS), re.IGNORECASE)

# Группа 4: обход правил/санкций платформ (Upwork-боты, парсеры маркетплейсов).
SANCTIONS_BYPASS_FLAGS = (
    r"\bupwork\b[\s\S]{0,80}?(?:бот|автоматиз|парсинг|api)",
    r"\bfiverr\b[\s\S]{0,80}?(?:бот|автоматиз|парсинг|api)",
    r"linkedin[\s\S]{0,80}?автомат",
    r"парсер\s+(?:upwork|fiverr|wildberries|ozon|avito|wb)",
    r"автоматическ\w+\s+отклик\w*\s+(?:на|для)\s+(?:upwork|fiverr|kwork)",
    r"генераци\w+\s+отклик\w*\s+через\s+(?:gpt|ai|claude)",
)
_SANCTIONS_BYPASS_RE = re.compile("|".join(SANCTIONS_BYPASS_FLAGS), re.IGNORECASE)


# === P2.2: маркеры авторского подхода для B2C-лендингов ===
# Если landing+B2C-niche, но клиент явно отверг Tilda и хочет авторский дизайн
# (анимации, GSAP, framer-motion и т.п.) — это наша зона, не reject.
AUTHORSHIP_MARKERS = (
    r"\bбез\s+tilda\b",
    r"не\s+(?:на\s+|использу\w+\s+)?tilda",
    r"необычн\w+\s+дизайн",
    r"авторск\w+\s+(?:дизайн|подход|решени|концепци)",
    r"WOW[\s-]?эффект",
    r"визуальн\w+\s+эффект",
    r"анимаци\w+",
    r"\bGSAP\b|\bframer[\s-]?motion\b|\bthree\.?js\b|\blottie\b",
    r"анимированн",
    r"интерактивн\w+\s+эффект",
)
_AUTHORSHIP_RE = re.compile("|".join(AUTHORSHIP_MARKERS), re.IGNORECASE)


HARD_REJECT_KEYWORDS = (
    # Русские склонения через \w* (исправление: \bбитрикс\b не ловил 'битриксе/битрикса')
    r"\b1c[\s-]*битрикс\w*", r"\bбитрикс\w*", r"\bbitrix\b",
    r"\bwordpress\b", r"\bвордпресс\w*", r"\bна\s+wp\b",
    r"\bтильд\w+", r"\btilda\b",
    r"\bjoomla\b", r"\bopencart\b", r"\bshopify\b", r"\bwix\b",
    r"\bdrupal\b", r"\bдрупал\w*",
    # v4 hot-fix: редкие CMS которые проскакивали (наблюдённые кейсы)
    r"\bmodx\b", r"\bмодекс\w*",
    r"\bmagento\b", r"\bмаджент\w*",
    r"\bprestashop\b", r"\bпрестошоп\w*",
    r"\boctober[\s-]?cms\b",
    r"\bdle\b\s+(сайт|разраб|админ)|datalife\s+engine",
    r"\bumi[\s.-]?cms\b",
    r"\binstantcms\b|инстант[\s-]?cms",
    r"\bhostcms\b",
    # v4 волна 1.5 / Идея 30: остальные CMS из полного списка user'а
    r"\badobe\s+muse\b|\bадоб\s+мьюз",
    r"\btextolite\b|\bтекстолайт",
    r"\bucoz\b|\bюкоз\w*",
    r"\bmegagroup\b|\bмегагрупп\w*",
    r"\bunisite\b|\bюнисайт\w*",
    r"\bzend\b\s+(framework|php)?",
    # Senler — основная работа (не интеграция через API)
    r"\bsenler\b\s+(как\s+основ|основная|настрой|разработ)",
    r"автоворонк\w+\s+(на\s+|в\s+)?senler",
    r"\blaravel\b", r"\bsymfony\b", r"\byii[\s-]?2?\b", r"\bcodeigniter\b",
    # v4 2.7: PHP жёстко — любое явное упоминание PHP как стека
    r"\bна\s+(чистом\s+|чисто\s+)?php\b",
    r"\bphp\s+(сайт|проект|разработ|приложен|сервер|api|бэкенд|backend|fullstack|fullstak|fullstak|веб[\s-]?приложен)",
    r"\bвеб[\s-]?приложен\w+\s+на\s+php\b",
    r"\bbackend\s+на\s+php\b|\bбэкенд\s+на\s+php\b",
    r"\bphp[\s-]*разработчик\s+нужен|нужен\s+php[\s-]*разработчик",
    r"\bстек:?\s*php\b",
    r"\bнужен\s+django\s+разработчик",
    r"\bremnawave\b", r"\bmarzban\b",
    r"\b3x[\s-]*ui\b", r"\bxray[\s-]*panel\b",
    r"контент[\s-]*менеджер\s+нужен",
    r"наполнить\s+сайт\s+товар",
    r"сайт[\s-]*донор",
    r"убрать\s+водяные\s+знаки",
    r"\bswift\b\s+(разработ|приложени)",
    r"нативн(ое|ая)\s+(android|ios)",
    r"\b1с[\s-]*предприятие\b", r"доработка\s+1с",
    r"тестовое\s+задание\s+без\s+оплат",
    r"за\s+отзыв(\s|$)", r"за\s+портфолио",
    r"требуется\s+команда", r"ищем\s+команду",
    r"\bтим[\s-]*лид\b\s+(нужен|требуется)",
    r"\bunity\b\s+разработ", r"\bunreal\b\s+engine",
)
_HARD_REJECT_RE = re.compile("|".join(HARD_REJECT_KEYWORDS), re.IGNORECASE)


# v4 волна 1.5 / Регрессия 1: маркеры "работы РЯДОМ с CMS"
# (бэкенд на нашем стеке, который интегрируется с чужим сайтом через API).
# Если совпало одновременно с CMS-маркером — пропускаем hard reject и даём в скоринг.
INTEGRATION_STACK_FLAGS = (
    r"\b(api|апи)\s+на\s+(nest|next|python|fastapi|node|go\b)",
    r"бот\s+(на\s+)?(python|aiogram|nest|node)",
    r"\b(next\.?js|nest\.?js|nodejs)\s+(приложен|сайт|сервис|бэкенд|api)",
    r"\bвебхук\w*|\bwebhook",
    r"интеграц\w+\s+(через|с|по)\s+(api|rest|graphql|webhook|вебхук)",
    r"бэкенд\s+(на\s+)?(nest|next|fastapi|python|node)",
    r"микросервис\s+(на\s+)?(node|python|nest)",
    r"телеграм[\s-]?бот\s+(для|которы|на)",
    r"telegram[\s-]?бот\s+(для|которы|на)",  # русское описание с английским словом
    r"\btelegram[\s-]?bot\s+(for|which|to)",  # на английском
    r"интегриру\w+\s+(с|через)\s+(rest|api|вебхук|webhook)",
    r"через\s+(rest|api|graphql|webhook)\s+(api|интерфейс|интеграц)?",
    r"\brest\s+api\b",
    r"бот\s+(который|для)\s+(работает|интегриру|подключа)",
)
_INTEGRATION_STACK_RE = re.compile("|".join(INTEGRATION_STACK_FLAGS), re.IGNORECASE)


def is_integration_with_external_cms(title: str, description: str) -> bool:
    """v4 рег.1: задача — наш стек НАД чужой CMS через API/webhook?

    Если ДА — CMS-маркер не приводит к hard reject, задача идёт в скоринг.
    """
    return bool(_INTEGRATION_STACK_RE.search(f"{title}\n{description}"))


# === Детекторы ===

def _has_ai_priority(title: str, description: str) -> bool:
    return bool(_AI_PRIORITY_RE.search(f"{title}\n{description}"))


def _hard_reject_reason(title: str, description: str) -> Optional[str]:
    match = _HARD_REJECT_RE.search(f"{title}\n{description}")
    return match.group(0) if match else None


def detect_no_code_required(title: str, description: str) -> Optional[str]:
    """
    Возвращает совпавший no-code маркер если задача требует no-code инструмент.
    Edge case (P0.3): если в тексте есть фразы миграции С no-code НА код —
    возвращает None, заказ обрабатывается обычным скорингом.
    Миграция засчитывается если:
      (a) явный migration-глагол (переписать, переехать, уйти от и т.п.), ИЛИ
      (b) комбинация (текущее состояние на no-code) + (новая цель на коде).
    """
    text = f"{title}\n{description}"
    match = _NO_CODE_RE.search(text)
    if not match:
        return None
    if _NO_CODE_MIGRATION_RE.search(text):
        return None
    if _NO_CODE_CURRENT_RE.search(text) and _CODE_TARGET_RE.search(text):
        return None
    return match.group(0)


def detect_always_hard_reject(title: str, description: str) -> Optional[str]:
    """P1.2: инфобиз / ИИ-агентство — отклоняем независимо от AI-флага."""
    match = _ALWAYS_HARD_REJECT_RE.search(f"{title}\n{description}")
    return match.group(0) if match else None


def detect_landing_reject(title: str, description: str, budget_limit: int) -> Optional[str]:
    """
    P1.1 + P2.2: hard-reject лендингов / одностраничников.

    Условия (P2.2 уточнение — есть escape через AUTHORSHIP_MARKERS):
      1. landing + B2C-ниша + бюджет < 100 000 ₽ + НЕТ авторских маркеров → reject.
      2. landing + бюджет < 80 000 ₽ + НЕТ авторских маркеров → reject (Tilda-территория).

    AUTHORSHIP_MARKERS (без tilda / GSAP / framer-motion / WOW-эффект / анимации
    и т.п.) сигналят что клиент целенаправленно ищет кодовый авторский лендинг,
    а не Tilda-сборку — это наша зона.
    """
    text = f"{title}\n{description}"
    if not _LANDING_RE.search(text):
        return None

    has_authorship = bool(_AUTHORSHIP_RE.search(text))

    niche_match = _B2C_NICHES_RE.search(text)
    if niche_match:
        if has_authorship:
            return None  # авторский подход для B2C — пропускаем
        if budget_limit and budget_limit >= 100_000:
            return None  # бюджет >100к — даже B2C-ниша может быть нашей
        return (
            f"лендинг для B2C-ниши '{niche_match.group(0)}' — "
            f"Tilda-территория (нет авторских маркеров, бюджет < 100к)"
        )

    if 0 < budget_limit < 80_000 and not has_authorship:
        return f"лендинг с бюджетом {budget_limit:,} ₽ < 80 000 ₽ — Tilda-территория"

    return None


def detect_grey_zone(title: str, description: str) -> tuple[str, int, str]:
    """
    P0.1: серая / чёрная зона — обход политик платформ, mass-action, обход защиты.

    Returns:
        (action, penalty, reason)
        action: 'pass' | 'penalty' | 'hard_reject'
        penalty: -3 (gr1/gr2 один маркер), -2 (gr3/gr4 один маркер), 0 — иначе.

    Логика:
      - 2+ маркера в одной группе ИЛИ ≥2 групп задействованы → hard_reject.
      - Один маркер в gr1 (архитектурный обход) или gr2 (mass-action) → -3.
      - Один маркер в gr3 (обход защиты) или gr4 (обход правил платформ) → -2.
      - Whitelist (LEGITIMATE_USE) полностью гасит детектор — pass.
    """
    text = f"{title}\n{description}"

    if _LEGITIMATE_USE_RE.search(text):
        return "pass", 0, ""

    groups = []
    for re_obj, group_name, base_penalty in (
        (_GREY_ARCH_RE, "архитектурный обход", -3),
        (_MASS_ACTION_RE, "mass-action", -3),
        (_ANTI_PROTECTION_RE, "обход защиты", -2),
        (_SANCTIONS_BYPASS_RE, "обход правил платформ", -2),
    ):
        matches = [m.group(0) for m in re_obj.finditer(text)]
        if matches:
            groups.append((group_name, matches, base_penalty))

    if not groups:
        return "pass", 0, ""

    has_double_in_one_group = any(len(matches) >= 2 for _, matches, _ in groups)
    has_multi_groups = len(groups) >= 2

    if has_double_in_one_group or has_multi_groups:
        summary = "; ".join(
            f"{name}({len(matches)})" for name, matches, _ in groups
        )
        return "hard_reject", 0, f"grey/black zone: {summary}"

    name, matches, penalty = groups[0]
    return "penalty", penalty, f"grey zone {name}: '{matches[0]}'"


def detect_external_api_barrier(title: str, description: str) -> tuple[int, str]:
    """
    P1.3: штраф -1 за API соцсетей с долгим одобрением (TikTok / Instagram Graph /
    WhatsApp Business / LinkedIn Marketing). Если приложение УЖЕ одобрено —
    штраф не применяется.
    """
    text = f"{title}\n{description}"
    match = _EXTERNAL_API_RE.search(text)
    if not match:
        return 0, ""
    if _EXTERNAL_API_OK_RE.search(text):
        return 0, ""
    return -1, f"API с барьером входа: '{match.group(0)}'"


def detect_commercial_parsing(title: str, description: str) -> tuple[int, str]:
    """
    P1.4: штраф -1 за парсинг чужих коммерческих источников для построения
    собственного каталога / витрины. Whitelist: HH, госсайты, новости,
    парсинг для внутренней аналитики / собственного магазина.
    """
    text = f"{title}\n{description}"
    match = _COMMERCIAL_PARSING_RE.search(text)
    if not match:
        return 0, ""
    if _PARSING_OK_RE.search(text):
        return 0, ""
    return -1, f"парсинг чужих коммерческих источников: '{match.group(0).strip()}'"


def detect_nko_caution(title: str, description: str) -> bool:
    """P2.5: флаг для Haiku — НКО / благотворительный фонд, осторожность."""
    return bool(_NKO_RE.search(f"{title}\n{description}"))


def _flatten(matches: list) -> list:
    flat = []
    for m in matches:
        if isinstance(m, tuple):
            flat.extend([x for x in m if x])
        else:
            flat.append(m)
    return list(set(flat))


def detect_scope_red_flags(title: str, description: str) -> tuple[int, list]:
    unique = _flatten(_SCOPE_RED_FLAGS_RE.findall(f"{title}\n{description}"))
    count = len(unique)
    if count == 0:
        return 0, []
    if count == 1:
        return -1, unique
    if count == 2:
        return -2, unique
    return -3, unique


def detect_open_ended_scope(title: str, description: str) -> tuple[int, list]:
    unique = _flatten(_OPEN_ENDED_RE.findall(f"{title}\n{description}"))
    return (-2, unique) if unique else (0, [])


def detect_tech_incompetence(title: str, description: str) -> tuple[int, list]:
    unique = _flatten(_TECH_INCOMPETENCE_RE.findall(f"{title}\n{description}"))
    return (-2, unique) if unique else (0, [])


def detect_attachment_hint(title: str, description: str) -> bool:
    """
    P2.2 (минимум): по тексту определяем есть ли вложение с ТЗ / деталями.
    kwork-library не отдаёт реальный список attachments, поэтому ловим
    только текстовые намёки ("ТЗ во вложении", "прикреплён файл" и т.п.).
    """
    return bool(_ATTACHMENT_HINT_RE.search(f"{title}\n{description}"))


def detect_critical_unknowns(
    title: str,
    description: str,
    scope_flags: list,
    open_flags: list,
    tech_flags: list,
    wanted: int,
    limit: int,
) -> list[str]:
    """
    P2.4: список 'критичных неизвестных' для GO/QUALI роутинга.
    Возвращает короткие лейблы вида 'budget=размыт', 'tech=не указана'.
    Если len(...) >= 2 — заказ помечается как QUALI (нужны уточнения).
    """
    text = f"{title}\n{description}"
    unknowns: list[str] = []

    if wanted == 0 and limit == 0:
        unknowns.append("бюджет не указан")
    elif _VAGUE_BUDGET_RE.search(text):
        unknowns.append("бюджет размыт")

    if _VAGUE_TECH_RE.search(text):
        unknowns.append("ключевая технология не указана")

    if scope_flags:
        unknowns.append("детали отложены (ЛС/созвон/после)")

    if open_flags:
        unknowns.append("open-ended scope")

    if tech_flags:
        unknowns.append("заказчик не понимает технических деталей")

    if _VAGUE_DEADLINE_RE.search(text):
        unknowns.append("срок исполнения не указан")

    if detect_attachment_hint(title, description):
        unknowns.append("ТЗ во вложении (бот не разбирает)")

    return unknowns


def _parse_budget_numbers(budget: str) -> tuple[int, int]:
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


def _budget_too_low(wanted: int, limit: int, is_ai: bool) -> Optional[str]:
    min_bar = MIN_BUDGET_AI if is_ai else MIN_BUDGET_GENERAL
    if wanted == 0 and limit == 0:
        return None
    if wanted == limit:
        threshold = BUDGET_SINGLE_THRESHOLD if not is_ai else MIN_BUDGET_AI
        if wanted < threshold:
            return f"единичный бюджет {wanted:,} ₽ < {threshold:,} ₽"
        return None
    if wanted < min_bar and limit < min_bar:
        return f"вилка {wanted:,}-{limit:,} ₽, оба < {min_bar:,} ₽"
    return None


# === Детектор несоответствия бюджета и скоупа ===

# Ключевые слова масштабируемости (признак 3)
_SCALE_KEYWORDS = re.compile(
    r"масштабируем(ая|ой|ую)\s+архитектур"
    r"|мультиязычност"
    r"|multi[\s-]*language"
    r"|\b50\s*[kк]\+?\s*(товар|product|sku)"
    r"|мультибрендов"
    r"|несколько\s+(стран|витрин|магазин|маркетплейс)"
    r"|multi[\s-]*(brand|store|region|country)",
    re.IGNORECASE,
)

# Крупные функциональные блоки (признак 1)
_FUNC_BLOCK_KEYWORDS = re.compile(
    r"\bпарсинг\b"
    r"|\bseo\b|поисковая\s+оптимизац"
    r"|мультиязычност|multi[\s-]*lang"
    r"|\bадминк[аеу]\b|\bадмин[\s-]*панел"
    r"|\bкаталог\b|\bкаталог(а|е|и)?\b"
    r"|\bинтеграци(я|и|й)\b"
    r"|\bвитрин(а|ы|е)\b"
    r"|\bлич(ный|ный|ном)\s+кабинет"
    r"|\bоплат(а|ы|е)\b|\bэквайринг\b"
    r"|\bуведомлени(я|й|е)\b"
    r"|\bаналитик(а|е|и)\b|\bдашборд\b"
    r"|\bпоиск\b.*\bфильтр|\bфильтр\b.*\bпоиск"
    r"|\bapi[\s-]*интеграци|\bсторонн\w+\s+api"
    r"|\bотзыв(ы|ов)?\b|\bрейтинг\b"
    r"|\bрегистраци(я|и)\b|\bавториза",
    re.IGNORECASE,
)


def detect_budget_scope_mismatch(
    title: str,
    description: str,
    budget_limit: int,
) -> tuple[int, str]:
    """
    Детектирует несоответствие масштаба задачи и бюджета.

    Признаки:
      1. >5 крупных функциональных блоков в описании
      2. Верхняя граница бюджета < 150 000 ₽
      3. Ключевые слова масштабируемости

    При 2+ совпадениях → штраф -2.

    Returns:
        (penalty, reason) — penalty = 0 или -2.
    """
    text = f"{title}\n{description}"
    matched = []

    # Признак 1: >5 функциональных блоков
    blocks = [m.group(0).lower().strip() for m in _FUNC_BLOCK_KEYWORDS.finditer(text)]
    block_count = len(set(blocks))
    if block_count > 5:
        matched.append(f"функц.блоков={block_count}")

    # Признак 2: бюджет < 150к
    if 0 < budget_limit < 150_000:
        matched.append(f"бюджет {budget_limit:,}<150к")

    # Признак 3: ключевые слова масштабируемости
    scale_match = _SCALE_KEYWORDS.search(text)
    if scale_match:
        matched.append(f"масштаб: '{scale_match.group(0)}'")

    if len(matched) >= 2:
        reason = "BudgetScopeMismatch: " + "; ".join(matched)
        return -2, reason

    return 0, ""


# === v4 Идея 31: AI-инженерия vs AI-маркетинг ===

# Маркеры AI-инженерии (наш профиль) — бонус +1
AI_ENGINEERING_MARKERS = (
    r"function\s+calling|tool[\s-]?use|tool\s+orchestrator",
    r"\brag\b|retrieval[\s-]?augmented|векторн\w+\s+(бд|база|сторадж|store)",
    r"\bembeddings?\b|эмбеддинг",
    r"\bchromadb\b|\bpinecone\b|\bqdrant\b|\bweaviate\b|\bchroma\b",
    r"system\s+prompt|систем\w+\s+промпт",
    r"\bclaude\s+api|\bopenai\s+api|\banthropic\s+api|yandex[\s-]?gpt|gigachat",
    r"\bwhisper\b|\btts\b|elevenlabs|yandex\s+speechkit",
    r"\blangchain\b|\blanggraph\b|llama[\s-]?index",
    r"мульти[\s-]?агент\w+\s+(систем|архитект)",
    r"чат[\s-]?(помощник|ассистент)\s+над\s+(данными|excel|crm|таблиц)",
    r"ассистент\s+над\s+документ\w+\s+через\s+rag",
    r"\bmcp\b\s+(сервер|server|integration)",
)
_AI_ENG_RE = re.compile("|".join(AI_ENGINEERING_MARKERS), re.IGNORECASE)

# СТРОГИЕ маркеры AI-маркетинга — 1 достаточно для hard reject
AI_MARKETING_STRONG_MARKERS = (
    r"ии[\s-]?агент\w*\s+(по\s+привлечен|для\s+(холодн|лидоген|outreach))",
    r"\bии[\s-]?агент\w*\s+(для\s+товарн|для\s+ниш|для\s+селлер)",
    r"research[\s-]?агент\w+\s+(для|по)\s+(ниш|товар|перепродаж)",
    r"\bai\b\s+для\s+селлер\w+\s+маркетплейс",
    r"сетк\w+\s+(агентов|ии[\s-]?ботов)|ии[\s-]?агентств",
    r"выявлени\w+\s+центров\s+принятия\s+решен",
    r"холодн\w+\s+outreach",
)
_AI_MKT_STRONG_RE = re.compile("|".join(AI_MARKETING_STRONG_MARKERS), re.IGNORECASE)

# СЛАБЫЕ маркеры — 1 = штраф -3, 2+ = hard reject
AI_MARKETING_WEAK_MARKERS = (
    r"автогенерац\w+\s+пост|автоматическ\w+\s+пост\w+\s+в\s+(телеграм|тг|vk|дзен)",
    r"автоматическ\w+\s+написани\w+\s+стат\w+",
    r"плотност\w+\s+словоформ|количеств\w+\s+ключей",
    r"seo[\s-]?копирайт|seo[\s-]?текст",
    r"продающ\w+\s+(ии|ai|нейросет)",
    r"удержани\w+\s+диалог\w+\s+автомат",
    r"автозаполнен\w+\s+карточек\s+товар\w+\s+через\s+(ai|ии)",
    r"товарн\w+\s+связк|торгов\w+\s+ниш\s+(для|маркетплейс)",
)
_AI_MKT_WEAK_RE = re.compile("|".join(AI_MARKETING_WEAK_MARKERS), re.IGNORECASE)


def classify_ai_task(title: str, description: str) -> tuple[str, list[str]]:
    """v4 Идея 31: классификация AI-задачи.

    Returns: (category, marker_examples)
        AI_MARKETING_HARD  — 1+ строгий маркетинговый ИЛИ 2+ слабых — hard reject
        AI_MARKETING_SOFT  — 1 слабый маркетинговый маркер — штраф -3
        AI_ENGINEERING     — 1+ инженерных и 0 маркетинговых — бонус +1
        AI_AMBIGUOUS       — иначе
    """
    text = f"{title}\n{description}"
    eng_matches = {m.group(0).lower() for m in _AI_ENG_RE.finditer(text)}
    strong = {m.group(0).lower() for m in _AI_MKT_STRONG_RE.finditer(text)}
    weak = {m.group(0).lower() for m in _AI_MKT_WEAK_RE.finditer(text)}

    if strong:
        return "AI_MARKETING_HARD", list(strong)[:3]
    if len(weak) >= 2:
        return "AI_MARKETING_HARD", list(weak)[:3]
    if len(weak) == 1:
        return "AI_MARKETING_SOFT", list(weak)[:3]
    if eng_matches:
        return "AI_ENGINEERING", list(eng_matches)[:3]
    return "AI_AMBIGUOUS", []


# === v4 раздел 7: терминологическая специфичность ===

QUALIFYING_TERMS = (
    # Backend
    r"\bnestjs\b", r"\bnest\.?js\b", r"\bfastapi\b", r"\bgrpc\b", r"\bgraphql\b",
    r"\bapollo\b", r"oauth\s+flow", r"\bsaml\b",
    r"\bidempotenc\w+", r"\brate[\s-]?limit", r"queue\s+worker",
    r"\bcelery\b", r"\bbullmq\b", r"\bsharding\b", r"\breplicat\w+",
    r"\bpgvector\b", r"redis\s+pub[\s-]?sub",
    # AI/ML
    r"\brag\b\s+(система|подход|архитектур|пайплайн)|retrieval[\s-]?augmented",
    r"function\s+calling|tool[\s-]?use",
    r"embeddings?\b|эмбеддинг", r"vector\s+(db|database|store)|векторн\w+\s+(бд|база)",
    r"fine[\s-]?tuning|дообучен", r"\blora\b",
    r"semantic\s+search", r"\blangchain\b", r"\bllama[\s-]?index\b",
    # Infrastructure
    r"docker[\s-]?compose", r"\bkubernetes\b|\bk8s\b", r"\bterraform\b", r"\bansible\b",
    r"\bprometheus\b", r"\bgrafana\b", r"\bsentry\b", r"\bdatadog\b", r"\bopentelemetry\b",
    r"ci/cd\s+pipeline|ci[\s-]?cd[\s-]?пайплайн", r"blue[\s-]?green",
    # Frontend
    r"server[\s-]?sent\s+events|\bsse\b",
    r"\bwebsocket", r"server\s+components|\brsc\b",
    r"\bsuspense\b", r"app\s+router", r"\bhydration\b",
    r"web\s+vitals", r"schema\.org", r"\bhreflang\b",
    # Telegram
    r"\baiogram\b", r"telegram\s+mini\s+app|tg[\s-]?mini[\s-]?app|tma\b",
    r"\bmtproto\b", r"\buserbot\b",
    # CMS/специализированное
    r"\bstrapi\b", r"\bsanity\b", r"headless\s+cms", r"\bdirectus\b", r"\bpayload\s+cms\b",
    # Платежи и интеграции
    r"юkassa\s+marketplace|recurrent\s+(payment|billing)",
    r"\bsplitt?ing\b\s+(платеж|payments?)", r"\bescrow\b", r"webhook\s+sign",
)
_QUALIFYING_RE = re.compile("|".join(QUALIFYING_TERMS), re.IGNORECASE)

MASS_TERMS = (
    r"\bбот\b", r"телеграм[\s-]?бот", r"\bтг[\s-]?бот",
    r"\bсайт\b", r"\bлендинг", r"\blanding\b",
    r"\bмагазин\b", r"интернет[\s-]?магазин",
    r"\bпарсер\b", r"\bскрипт\b",
    r"\bавтоматизаци\w+", r"\bчат[\s-]?бот",
    r"\bнейросет\w+", r"искусственн\w+\s+интеллект", r"\bии\b",
    r"\bчат[\s-]?гпт\b", r"\bchatgpt\b",
    r"\bопенаи\b", r"\bopenai\b", r"\bgpt\b",
)
_MASS_RE = re.compile("|".join(MASS_TERMS), re.IGNORECASE)


def detect_terminology_specificity(title: str, description: str) -> tuple[int, str]:
    """v4 раздел 7: терминологическая специфичность.

    Returns:
        (modifier, reason)
        - 3+ уникальных квалифицирующих маркеров → +2 (профессиональная задача)
        - Только массовые без квалифицирующих → -1 (заказ для автооткликов)
        - Иначе 0
    """
    text = f"{title}\n{description}"
    qual_matches = {m.group(0).lower() for m in _QUALIFYING_RE.finditer(text)}
    mass_matches = {m.group(0).lower() for m in _MASS_RE.finditer(text)}

    if len(qual_matches) >= 3:
        sample = ", ".join(sorted(qual_matches)[:5])
        return 2, f"квалифицирующая терминология ({len(qual_matches)} терминов: {sample}): +2"

    if mass_matches and not qual_matches:
        sample = ", ".join(sorted(mass_matches)[:3])
        return -1, f"только массовая терминология ({sample}) без квалифицирующих: -1"

    return 0, ""


# === v4 6.1: модификатор скора по конкурентной среде ===

def compute_competition_modifier(
    responses_count: int,
    hired_percent: Optional[int],
    buyer_achievements: int,
) -> tuple[int, list[str]]:
    """v4 6.1: модификатор скора по числу откликов + уровню покупателя.

    Args:
        responses_count: число откликов на момент первой проверки.
        hired_percent: hire_rate заказчика (0-100) или None.
        buyer_achievements: число медалек покупателя (из achievements_list).

    Returns:
        (modifier, reasons) — суммарный модификатор и список применённых правил.

    Шкала откликов:
        0-5    → +1 (первая волна)
        6-15   → 0
        16-30  → -1 (среднее наполнение)
        31+    → -3 (переполнено)

    Шкала покупателя:
        0 ачивок + hire_rate < 10%  → -2 (вероятный собиратель КП)
        0 ачивок + hire_rate 10-30% → 0 (нейтрально)
        1 ачивка                    → +1
        2+ ачивок                   → +2 (топ-покупатель)
    """
    modifier = 0
    reasons: list[str] = []

    # Отклики
    if responses_count <= 5:
        modifier += 1
        reasons.append(f"первая волна ({responses_count} откликов): +1")
    elif responses_count <= 15:
        pass
    elif responses_count <= 30:
        modifier -= 1
        reasons.append(f"среднее наполнение ({responses_count} откликов): -1")
    else:
        modifier -= 3
        reasons.append(f"переполнено ({responses_count} откликов): -3")

    # Уровень покупателя
    if buyer_achievements == 0:
        if hired_percent is not None and hired_percent < 10:
            modifier -= 2
            reasons.append(f"без медалек + hire_rate {hired_percent}%: -2 (собиратель КП)")
    elif buyer_achievements == 1:
        modifier += 1
        reasons.append("медалька покупателя: +1")
    elif buyer_achievements >= 2:
        modifier += 2
        reasons.append(f"{buyer_achievements} медальки покупателя: +2 (топ)")

    return modifier, reasons


# === v4 2.2: hard reject — автоматизация бронирования с обходом ===
# Триггер: 2+ из 4 категорий маркеров → hard reject.

_BOOKING_ACTION_RE = re.compile(
    r"\bбронировани\w+|\bбронир\w+|\bзаписат\w+\s+(на|в)|\bрегистрац\w+\s+(на|в|онлайн)|"
    r"\bуспе(ть|вать)\s+(забронировать|записаться|зарегистрироват)|"
    r"получить\s+(место|слот|талон|очеред)|"
    r"\bавтоматическ\w+\s+(бронир\w*|записыва)|"
    r"\bbooking[\s-]?bot|appointment[\s-]?(bot|booking)",
    re.IGNORECASE,
)
_BOOKING_BYPASS_RE = re.compile(
    r"пройти\s+капч|обход\w*\s+капч|\bcaptcha[\s-]?solver|"
    r"\bantibot|антибот|анти[\s-]?капч|"
    r"ротац\w+\s+ip|пул\s+прокси|фингерпринт|fingerprint",
    re.IGNORECASE,
)
_BOOKING_SPEED_RE = re.compile(
    r"быстрее\s+(человек|любого)|за\s+секунд|в\s+разы\s+быстрее|"
    r"мгновенн\w+\s+бронир|первым\s+успе|раньше\s+(всех|других)",
    re.IGNORECASE,
)
_BOOKING_TARGET_RE = re.compile(
    r"\bгосуслуг|gosuslugi|"
    r"\bвизов\w+\s+(центр|анкет)|\bvfsglobal|\btlscontact|\bvacprime|"
    r"\bмемориал|колумбари|"
    r"\bполиклиник|\bбольниц|запись\s+к\s+врач|"
    r"\bшкол\w+\s+(запис|регистрац)|\bдетск\w+\s+сад\w+\s+(запис|очеред)|"
    r"\bпарковк\w+\s+(онлайн|резерв|бронир)|"
    r"\bштраф\w*\s+(онлайн|оплат)|"
    r"\bмфц\b|\bросреестр|"
    r"\bmirzamak|\bаквапарк|\bбассейн\w+\s+(запис|сеанс)",
    re.IGNORECASE,
)


def detect_booking_automation(title: str, description: str) -> Optional[str]:
    """v4 2.2: автоматизация бронирования с обходом — hard reject при 2+ из 4 категорий маркеров.

    Категории: action (бронирование/запись), bypass (капча/прокси/антибот),
    speed (быстрее человека), target (госуслуги/визовые/мемориалы/поликлиники).
    """
    text = f"{title}\n{description}"
    matched = []
    if _BOOKING_ACTION_RE.search(text):
        matched.append("действие (бронирование)")
    if _BOOKING_BYPASS_RE.search(text):
        matched.append("обход защиты")
    if _BOOKING_SPEED_RE.search(text):
        matched.append("преимущество в скорости")
    if _BOOKING_TARGET_RE.search(text):
        matched.append("целевой ресурс с конкуренцией среди людей")
    if len(matched) >= 2:
        return f"автоматизация бронирования: {' + '.join(matched)} — правовой/этический риск"
    return None


# === v3: усиления BIG-промпта (применяются после Haiku, как post-penalty) ===

# 3.1: терминологический mismatch — крупный класс системы + сжатый срок.
_BIG_SYSTEM_CLASS_RE = re.compile(
    r"\bмаркетплейс|\bcrm\b|\berp\b|"
    r"социальн\w+\s+сет|торгов\w+\s+площадк|"
    r"\bплатформ(а|у|е|ы|ой|ам)\b",
    re.IGNORECASE,
)
_TIGHT_DEADLINE_TEXT_RE = re.compile(
    r"за\s+(одну\s+)?(недел|месяц)|"
    r"\bсрочно\b|\bв\s+сжат\w+\s+срок|"
    r"до\s+конца\s+(месяц|недел|квартал)|"
    r"за\s+\d+\s+(день|дня|дней|недел)",
    re.IGNORECASE,
)


def detect_terminology_mismatch(title: str, description: str) -> tuple[int, str]:
    """v3 3.1: класс системы (маркетплейс/CRM/ERP/соц.сеть/платформа) + сжатый срок.

    Срок Kwork = срок ПОДАЧИ заявок, не исполнения, поэтому смотрим только
    текстовые маркеры срока внутри описания.
    """
    text = f"{title}\n{description}"
    sys_match = _BIG_SYSTEM_CLASS_RE.search(text)
    if not sys_match:
        return 0, ""
    deadline_match = _TIGHT_DEADLINE_TEXT_RE.search(text)
    if not deadline_match:
        return 0, ""
    return -3, (
        f"терминологический mismatch — '{sys_match.group(0)}' + "
        f"'{deadline_match.group(0)}': класс системы требует месяцев работы"
    )


# 3.2: требование public github — фильтр под профиль не подходит.
_GITHUB_REQUIRED_RE = re.compile(
    r"обязательн\w+\s+ссылк\w+\s+на\s+github|"
    r"github\s+обязател|"
    r"без\s+github\s+не\s+(отклика|расс|пиш)|"
    r"не\s+рассматрива\w+\s+без\s+(репозитори|github)|"
    r"примеры\s+кода\s+в\s+открытом\s+доступ|"
    r"открытый\s+github\s+обязател",
    re.IGNORECASE,
)


def detect_github_required(title: str, description: str) -> tuple[int, str]:
    """v3 3.2: требование public github."""
    match = _GITHUB_REQUIRED_RE.search(f"{title}\n{description}")
    if match:
        return -3, f"требование public github: '{match.group(0)}'"
    return 0, ""


# 3.3: NDA до ТЗ + hire_rate 0% — паттерн собирателя КП.
_NDA_PATTERN_RE = re.compile(
    r"тз\s+после\s+(отклика|подпис|соглас)|"
    r"тз\s+под\s+nda|"
    r"nda\s+(до|перед)\s+тз|"
    r"подробност\w+\s+после\s+(подпис|соглас|nda)|"
    r"детал\w+\s+(в\s+)?лс\s+после|"
    r"подробн\w+\s+после\s+согласи",
    re.IGNORECASE,
)


def detect_nda_collector(
    title: str, description: str, hired_percent: Optional[int]
) -> tuple[int, str]:
    """v3 3.3: NDA до ТЗ при hire_rate 0% — собиратель КП."""
    if hired_percent is None or hired_percent > 0:
        return 0, ""
    match = _NDA_PATTERN_RE.search(f"{title}\n{description}")
    if not match:
        return 0, ""
    return -2, (
        f"NDA до ТЗ при hire_rate 0%: '{match.group(0)}' — возможный собиратель КП"
    )


# 3.4: real-time стриминг при низком бюджете.
_STREAMING_RE = re.compile(
    r"\bстриминг\w*|\bтрансляци\w*|live[\s-]?видео|"
    r"\bwebrtc\b|\bhls\b|"
    r"\bмедиасервер\w*|\bmediasoup\b|\bjanus\b|ant[\s-]?media|\bkurento\b",
    re.IGNORECASE,
)


def detect_streaming_low_budget(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """v3 3.4: real-time стриминг при бюджете < 150к."""
    if not budget_limit or budget_limit >= 150_000:
        return 0, ""
    match = _STREAMING_RE.search(f"{title}\n{description}")
    if not match:
        return 0, ""
    return -3, (
        f"real-time стриминг ('{match.group(0)}') при бюджете {budget_limit:,} ₽ "
        f"< 150 000 ₽ — инфраструктура от 200к"
    )


# 3.5: enterprise scope при низком бюджете (3+ маркера).
_ENTERPRISE_SCOPE_MARKERS = (
    (
        re.compile(
            r"сайт.{0,40}админк|админк.{0,40}сайт|сайт.{0,40}интеграц.{0,40}админк",
            re.IGNORECASE,
        ),
        "сайт+админка+интеграции",
    ),
    (
        re.compile(
            r"(\d+|пят[ьи]|шест[ьи]|сем[ьи]|восем[ьи]|девят[ьи]|десят[ьи])"
            r"\s*\+?\s*язык",
            re.IGNORECASE,
        ),
        "5+ языков",
    ),
    (
        re.compile(
            r"\bsitemap\b|\bhreflang\b|\b301[\s-]?редирект|schema\.org|"
            r"микроразметк\w+\s+(schema|json[\s-]?ld)",
            re.IGNORECASE,
        ),
        "SEO полного цикла",
    ),
    (
        re.compile(
            r"\bроли\b.{0,40}(админ|редактор|менеджер)|"
            r"admin.{0,30}редактор|разграничени\w+\s+прав|"
            r"\brbac\b",
            re.IGNORECASE,
        ),
        "роли в админке",
    ),
    (
        re.compile(
            r"\bbullmq\b|\bcelery\b|очеред\w+\s+задач|фонов\w+\s+обработк",
            re.IGNORECASE,
        ),
        "очередь задач",
    ),
    (
        re.compile(
            r"\bredis\b|кеширован|кеш\s+(переводов|данных|запрос)",
            re.IGNORECASE,
        ),
        "кеширование",
    ),
    (
        re.compile(
            r"тз\s+с\s+раздел|структурирован\w+\s+тз|документ\s+с\s+разделами|"
            r"прикреплён\w*\s+тз",
            re.IGNORECASE,
        ),
        "структурированное ТЗ",
    ),
)


def detect_enterprise_scope_low_budget(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """v3 3.5: 3+ маркера enterprise scope при бюджете < 150к."""
    if not budget_limit or budget_limit >= 150_000:
        return 0, ""
    text = f"{title}\n{description}"
    matched = [name for re_obj, name in _ENTERPRISE_SCOPE_MARKERS if re_obj.search(text)]
    if len(matched) < 3:
        return 0, ""
    return -3, (
        f"enterprise scope ({', '.join(matched)}) при бюджете "
        f"{budget_limit:,} ₽ < 150 000 ₽ — реальная стоимость 250к+"
    )


# 3.6: domain expertise — fuzzy fallback на специфические классификаторы/нормативы.
_DOMAIN_EXPERTISE_RE = re.compile(
    r"\bфкко\b|"
    r"\bснип\b|\bсп\s+\d+\.\d+|"
    r"\bгост\s+\d|"
    r"медицинск\w+\s+классификатор|"
    r"\bмкб[\s-]?10\b|"
    r"\bокпд\b|\bокп\s*\d|"
    r"строительн\w+\s+норматив|"
    r"бухгалтерск\w+\s+отчётн\w+\s+по\s+1с|"
    r"специфическ\w+\s+(api|апи)\s+(ржд|почт\w+\s+росс|мин\w+|госуслуг)|"
    r"обращени\w+\s+с\s+отход|"
    r"экологическ\w+\s+норматив|"
    r"\bтн[\s-]?вэд\b|"
    r"фармакопе|клиническ\w+\s+рекоменд",
    re.IGNORECASE,
)


def detect_domain_expertise(title: str, description: str) -> tuple[int, str]:
    """v3 3.6: страховочный детектор узкодоменной экспертизы.

    Применяется в дополнение к правилу J Haiku — если явные маркеры
    классификаторов / нормативов сработали, ставим -3 в коде.
    """
    match = _DOMAIN_EXPERTISE_RE.search(f"{title}\n{description}")
    if match:
        return -3, f"domain expertise mismatch: '{match.group(0)}' — узкодоменная экспертиза"
    return 0, ""


# === v3: категоризация заказа по бюджету ===

def categorize_by_budget(price_limit: int, price_wanted: int = 0) -> str:
    """v3 1.1: категория для роутинга промптов.

    BIG    (>=100к)  — крупные заказы для дохода.
    FAST   (<=50к)   — быстрые заказы для отзывов.
    DUAL   (50-100к) — скорится двумя промптами параллельно.
    BIG    (бюджет=0) — консервативно, как unknown-бюджет.
    """
    effective = price_limit or price_wanted
    if effective == 0:
        return "BIG"
    if effective <= 50_000:
        return "FAST"
    if effective >= 100_000:
        return "BIG"
    return "DUAL"


# === quota_status для rolling-периода Kwork ===

def quota_status(
    used_this_month: int,
    used_today: int,
    days_until_refill: int,
    period_days: int = 30,
) -> Dict:
    """
    Args:
        used_this_month: потрачено откликов в периоде
        used_today: потрачено откликов сегодня
        days_until_refill: дней до пополнения квоты Kwork
        period_days: длина периода (30)

    Минимальный порог всегда 7 (не ниже).
    Early-period (<20% прошло) → 8 (ждём лучших)
    Резерв (последняя неделя + мало осталось) → 8
    """
    remaining = MONTHLY_QUOTA - used_this_month
    period_elapsed = period_days - days_until_refill
    period_progress = period_elapsed / period_days if period_days > 0 else 0
    quota_used_ratio = used_this_month / MONTHLY_QUOTA if MONTHLY_QUOTA > 0 else 0

    is_early = period_progress < 0.2
    is_reserve = days_until_refill <= 7 and remaining < RESERVE_QUOTA_FOR_LAST_DAYS

    score_threshold = MIN_SCORE_FOR_RESPONSE

    if is_early:
        score_threshold = 8
        pace = "ранний период"
    elif is_reserve:
        score_threshold = 8
        pace = "резерв"
    elif quota_used_ratio > period_progress * 1.3:
        score_threshold = 8
        pace = "опережаю"
    else:
        pace = "нормально"

    daily_allowed = max(0, DAILY_SOFT_LIMIT - used_today)
    if remaining <= 0:
        daily_allowed = 0

    return {
        "remaining": remaining,
        "days_left": days_until_refill,
        "reserve_active": is_reserve,
        "daily_allowed": daily_allowed,
        "score_threshold": score_threshold,
        "pace": pace,
        "period_progress": round(period_progress, 2),
        "quota_used_ratio": round(quota_used_ratio, 2),
    }


SCORING_PROMPT = (
    DEVELOPER_PROFILE
    + """

===
ЗАКАЗ:
Название: {title}
Описание: {description}
Бюджет: {budget}
Срок: {deadline}
Откликов: {responses_count}

PRE-CHECK:
- AI: {is_ai}
- No-code требуется: {no_code} {no_code_note}
- Scope flags: {scope_flags} (penalty {scope_penalty})
- Open-ended: {open_ended_flags} (penalty {open_ended_penalty})
- Tech incompetence: {tech_flags} (penalty {tech_penalty})
- Категория сайта/лендинга: {site_category} ({site_note})
- API соцсетей с барьером входа: {api_barrier} (penalty {api_barrier_penalty})
- Парсинг чужих коммерческих источников: {parsing_flag} (penalty {parsing_penalty})
- НКО / благотворительный фонд: {nko_caution}
===

Оцени 1-10. Не "подходит технически", а "стоит ли тратить один из 30 патронов".

ЖЁСТКИЕ ПРАВИЛА:
A. No-code требуется (n8n/Make/Zapier/no-code/без кода) → скор НЕ ВЫШЕ 4.
   В обычном случае такие заказы уже отсеяны до Haiku; правило применяется
   только к edge-case "переписываем С n8n/Make НА код".
B. Open-ended scope + бюджет <150к → скор НЕ ВЫШЕ 5.
C. Tech incompetence + серьёзная задача → скор НЕ ВЫШЕ 5.
D. Все penalty складываются.
E. site_category == "turnkey":
   - Если задача — сложный продукт (бот, парсер, API, SaaS, MVP, дашборд,
     личный кабинет, бизнес-приложение с нетривиальной логикой) — +2 к скору (УТП).
   - Если задача — лендинг / сайт-визитка / одностраничник для услуг — БЕЗ бонуса.
     Опасные landing-кейсы B2C уже отсеяны в python-фильтре, остаётся серая зона.
F. site_category == "ambiguous" — нейтрально, БЕЗ авто-бонуса. НЕ достраивать
   "макета не упомянули → значит turnkey": ждём явных признаков.
G. "CMS" или "админ-панель для управления контентом/товарами" сами по себе НЕ являются
   пенальти — разработчик делает кастомную админку на NestJS/PostgreSQL. Пенальти и
   hard reject только если заказчик явно называет платформу: WordPress, Bitrix, Tilda,
   OpenCart, Shopify, Joomla, Wix.
H. AI-маркетинговый клиент vs AI-инженерный — критично.
   ШТРАФ -2 к БАЗОВОМУ скору если признаки AI-маркетинга:
   - "сетка агентов / ассистентов" в маркетинговом контексте
   - "мультиагентная система" в контексте маркетинга/продаж/инфобиза
   - "ИИ-сотрудник", "ИИ-помощник заменит отдел"
   - "автоматизировать всё с помощью AI"
   - "познакомить агентов с источниками", "обучить нейросеть на наших данных"
     при отсутствии конкретики по архитектуре
   - "боты сами разрабатывают/создают/формируют контент/стратегию"
   - "генеративный AI для бизнеса/продаж/клиентов" в обобщённой форме
   - "AI-агентство по инфобизу"
   - размытые модные термины без технического понимания
   БЕЗ штрафа (иногда +1) если признаки AI-инженерии:
   - конкретные термины: function calling, RAG, vector search, embedding,
     fine-tuning, system prompt, tool use
   - понимание ограничений: rate limits, context window, токены, latency
   - конкретный API: Claude Sonnet 4, GPT-4o, Whisper, Anthropic
   - технический стек рядом: NestJS + Postgres + pgvector
   ВАЖНО: "сетка телеграм-ботов для автопостинга" — это НЕ AI-маркетинг,
   это просто несколько ботов, обычная задача, без штрафа.
I. nko_caution=да — НЕ штрафовать автоматически, но проверить:
   - Если требуется бесплатная / льготная разработка / "за идею" → штраф -2.
   - Если бюджет заметно ниже скоупа (НКО часто грантовые) → флаг в reason
     "nko_caution: уточнить условия".
   В reason обязательно упомянуть "nko_caution" чтобы я уточнил условия в первом
   сообщении.
J. Domain-expertise mismatch (AI в специализированной области).
   ШТРАФ -2 если AI-проект требует "экспертных решений" в области требующей
   профессиональной квалификации, И верхняя граница бюджета < 500 000 ₽.
   Признаки: заказчик хочет чтобы система "анализировала параметры", "предлагала
   решения", "проверяла на нормативные ограничения", "выполняла расчёты".
   Профессиональные области:
   - Инженерное проектирование (строительство, архитектура, с/х, промышленность)
   - Медицина и фармацевтика (диагностика, назначения)
   - Юриспруденция (составление документов, оценка дел)
   - Бухгалтерия и налогообложение (составление отчётности)
   - Финансовый консалтинг (рекомендации по инвестициям)
   Реальная такая система требует команды экспертов и программистов на годы;
   за меньший бюджет — либо chat-bot с галлюцинациями, либо конфликт ожиданий.
   БЕЗ штрафа если заказчик прямо пишет: "вспомогательный инструмент",
   "черновики", "первый этап анализа", "не заменяет специалиста".
K. Selenium / Playwright / эмуляция браузера для "автоматизации действий на сайте":
   - Если указан собственный сайт заказчика ("наш интернет-магазин на example.com",
     "наш корпоративный портал") → легитимно, без штрафа.
   - Если у целевого сайта есть публичный API, но просят браузерную автоматизацию →
     это обход чего-то, серая зона, штраф -2 или -3.
   - Сайт не указан / указан чужой коммерческий сайт (маркетплейс, площадка) →
     серая или чёрная зона. Часть этого уже отсечена python-фильтром (см.
     P0.1 grey-zone), здесь подтверди штраф -3 если фильтр пропустил.
L. Калибровка budget-vs-scope на ВЕРХНИХ бюджетах (150-300к).
   Python-фильтр detect_budget_scope_mismatch ловит >5 функц.блоков при
   бюджете <150к. На бюджетах 150-300к, где реальная стоимость может быть
   500к+, проверяй вручную.

   Прикинь expected_cost из таблицы компонентов (нашёл в описании → +стоимость):
   - Простой лендинг: 30к
   - Сложный лендинг с эффектами/анимацией: 60к
   - Корпоративный сайт 10 страниц: 80к
   - Каталог e-commerce: 100к
   - Простой Telegram-бот: 30к
   - Сложный Telegram-бот (с БД, оплатой, аналитикой): 60к
   - Админ-панель: 50к
   - Интеграция с внешним API: 40к (за каждую)
   - Авторизация OAuth: 30к
   - Оплата (ЮKassa, Stripe): 25к
   - Личный кабинет с RBAC: 80к
   - Аналитика / дашборд: 50к
   - Real-time чат / уведомления: 60к
   - Поиск с фильтрами: 25к
   - Мультиязычность (5+ языков): 30к
   - AI-интеграция с RAG: 80-150к
   - Парсер с обработкой / антибаном: 50-100к

   Если expected_cost / budget_max > 1.5 → штраф -2, в reason укажи
   "budget_scope_v2: насчитано ~Xк (компоненты A, B, C) при бюджете Yк".
   Не дублировать с штрафом python-фильтра detect_budget_scope_mismatch
   (его сигнатура другая; считай только если он не сработал).

ТИП ЗАКАЗА (для воронки short / long):
- short: верхняя граница бюджета ≤50к, 1-2 функциональных блока,
  junior-friendly, срок исполнения 1 день - 1 неделя. Цель — быстрый отзыв.
- long: верхняя граница ≥100к, 3+ блока, сложная архитектура, срок ≥2 недель.
  Цель — основной доход.
- medium: между ними. Универсальный заказ.

БАЗОВЫЙ СКОР:

Стек (до 4):
+2 прямое совпадение (Next.js/NestJS/TS/PostgreSQL) ИЛИ Python/aiogram бот ИЛИ парсер+веб-интерфейс
+1 серая зона (Vue→React, мобилка)
+1 сильная сторона (AI на своём коде, real-time, админка, WB/Ozon-парсер с Excel-отчётом)

Бюджет (до 3):
+3 верхняя ≥100к, +2 70-100к, +1 50-70к, 0 <50к

AI (до 2):
+2 Claude/GPT/LLM/RAG/MCP с конкретным скоупом
+1 AI побочно или с размытым скоупом

Качество (до 2):
+1 детальное описание с критериями
+1 адекватный срок (2-4 недели) — но "срок" на Kwork = срок ПОДАЧИ ЗАЯВОК, не исполнения
+1 мало откликов (<10)
-2 пустое/противоречивое

ВАЖНО: НЕ штрафовать скор за короткий "срок" (1-3 дня) в заказе — это срок закрытия
приёма откликов на Kwork, а не срок исполнения работы. Реальный срок исполнения
обсуждается в переписке. Много откликов (30+) — просто признак популярного заказа.

ИТОГ = базовый + penalty.

9-10 обязательно, 7-8 рекомендую, 5-6 пропуск, <5 пропуск.

ПРИМЕРЫ КАЛИБРОВКИ (нестандартные формулировки про сайты/лендинги):

ПРИМЕР A — заказчик прячет готовый макет за нестандартной формулировкой:
"Сайт небольшой, дизайнер уже сделал, нужно перенести в код. NextJS подойдёт. Бюджет 60к."
→ это ready_mockup даже если в pre-check ambiguous: "дизайнер уже сделал" = чужой макет.
   Скор 2, reason: "по факту ready_mockup — у клиента готов чужой дизайн".

ПРИМЕР B — заказчик ищет дизайнера в плохо замаскированной форме:
"Нужен сайт-визитка для салона. Хочу что-то модное, в трендах 2026. Покажите портфолио лендингов."
→ упор на "тренды", "портфолио лендингов", B2C-ниша салона = pure_design,
   даже если site_category=ambiguous. Скор 0-3, reason: "B2C + упор на тренды — Tilda-территория".

ПРИМЕР C — turnkey без явных слов "под ключ":
"Открываем онлайн-школу программирования. Нужен сайт с разделами, формой заявки, оплатой курсов.
 Макетов нет, дизайн на твоё усмотрение. Бюджет 80-120к."
→ макета нет ИЗ КОНТЕКСТА ("дизайн на твоё усмотрение"), это turnkey даже без слова "под ключ".
   Скор 8-9, reason: "сайт под ключ без готового макета, бюджет адекватный — попадание в УТП".

ПРИМЕР D — ambiguous остаётся ambiguous (нет сигналов в обе стороны):
"Сделать сайт для магазина оптики. NextJS. Нужны интеграции с CRM."
→ про макет ничего не сказано — НЕ достраивать "значит turnkey". Оценка по другим осям
   (стек, бюджет, скоуп). Скор по обычной шкале, без бонуса/штрафа за site_category.

ПРИМЕР E — упомянута Figma как референс, не как готовый макет:
"Прикрепил пару скриншотов в Figma — это примерные референсы, окончательный дизайн делайте сами."
→ это turnkey, не ready_mockup. Слово "Figma" есть, но контекст явно про "референсы, делайте сами".
   Скор 7-8, reason: "Figma только как референс — фактический дизайн на разработчика".

JSON без markdown:
{{"score": 1-10, "is_ai": true/false, "type": "short"/"medium"/"long", "breakdown": {{"stack": X, "budget": X, "ai": X, "quality": X, "penalties": X}}, "reason": "одно предложение"}}
"""
)


# === v3: FAST-промпт для быстрых заказов (1-3 дня, ≤50к) ===

FAST_SCORING_PROMPT = (
    DEVELOPER_PROFILE
    + """

===
ЗАКАЗ:
Название: {title}
Описание: {description}
Бюджет: {budget}
Срок: {deadline}
Откликов: {responses_count}

PRE-CHECK:
- AI: {is_ai}
- No-code требуется: {no_code} {no_code_note}
- Scope flags: {scope_flags} (penalty {scope_penalty})
- Open-ended: {open_ended_flags} (penalty {open_ended_penalty})
- Tech incompetence: {tech_flags} (penalty {tech_penalty})
- Категория сайта/лендинга: {site_category} ({site_note})
===

КАТЕГОРИЯ FAST — быстрые заказы на 1-3 дня работы.
Цель: отзывы для профиля + покрытие краткосрочных финансовых задач.

{review_farming_block}

ВАЖНО ДЛЯ ОЦЕНКИ:
- НЕ оценивать в плюс наличие AI-компоненты (для FAST не нужно).
- НЕ требовать комплексности и fullstack-проекта (это не FAST).
- Отдавать приоритет ЗАМКНУТОСТИ и ЧЁТКОСТИ скоупа над масштабом.
- Тип заказа всегда "short".

СТАРТОВЫЙ СКОР: 5 (середина шкалы).

ПОЗИТИВНЫЕ МАРКЕРЫ (+1 каждый):

Чёткость скоупа (до +3):
+1 чёткое ТЗ или ясные требования (заказчик знает что хочет, не "обсудим")
+1 замкнутый scope: НЕТ "первый этап", "потом обсудим", "стек может расшириться", "по ходу уточним"
+1 конкретика: указаны имена сайтов / форматы файлов / поля данных / API

Тематическое попадание (+1, один из классов):
- Класс A — простые приложения/боты/скрипты:
  парсеры открытых данных в CSV/Excel/JSON;
  простые Telegram-боты (уведомления, FAQ, калькулятор, приём заявок);
  скрипты автоматизации (обработка файлов, конвертации);
  утилиты для Excel/Google Sheets, Apps Script.
- Класс B — серверные/инфраструктурные:
  деплой на VPS/VDS, nginx, SSL через Certbot;
  Docker / docker-compose для существующего проекта;
  CI/CD на GitHub Actions / GitLab CI;
  перенос проекта между серверами/хостингами;
  бэкапы, базовый мониторинг (Sentry, Healthchecks);
  webhook-обработчики (Telegram, GitHub, ЮKassa).
- Класс C — интеграции готовых API:
  подключение платёжек (ЮKassa, CloudPayments, Robokassa);
  подключение рассылок (SendPulse, UniSender, Mailgun);
  подключение аналитики (Метрика, GA4) с настройкой целей;
  импорт/экспорт данных между системами (разовый ETL);
  базовая SEO (sitemap, robots, мета, OG, schema.org).
- Класс D — доработки существующих проектов:
  мелкие правки в Next.js/NestJS/React/Python проектах;
  добавление 1-2 фич в существующий код;
  фикс багов;
  базовая оптимизация скорости (lighthouse, lazy loading, минификация).

Адекватность бюджета:
+1 бюджет соответствует объёму задачи (не "сделайте CRM за 30к")

НЕГАТИВНЫЕ МАРКЕРЫ (-2 каждый):
- Расширяемость: "первый этап", "потом обсудим", "стек может расшириться",
  "по ходу скоуп уточним", "пилот с продолжением"
- Требование верстать по готовой Figma/PSD/макету (pixel-perfect по чужому дизайну)
- Лендинг для B2C-услуг (тренер, дезинфекция, салон, мастер) — Tilda-территория
- Описание абстрактное без конкретики ("сделайте бота для бизнеса", "нужна автоматизация")
- "Консультация по разработке" / "поможете с выбором архитектуры"
- Деплой на инфраструктуру клиента с SSH-доступом без чёткого ТЗ
- Дизайн с нуля где требуется арт-направление (не быстрый шаблон)

ДОПОЛНИТЕЛЬНЫЕ НЕГАТИВНЫЕ МАРКЕРЫ (-1 каждый):
- Windows Server / IIS / Active Directory
- Kubernetes от продакшна (k8s-кластер с нуля)
- Bitrix / WordPress миграция
- "Разобраться с нашей текущей инфраструктурой"

ВАЖНО ПРО СРОКИ KWORK:
"Срок" на Kwork = срок ПОДАЧИ ЗАЯВОК, не исполнения. Не штрафовать за "1-3 дня".

ИТОГ = 5 + позитивы - негативы. Зажать в [1, 10].

JSON без markdown:
{{"score": 1-10, "is_ai": true/false, "type": "short", "breakdown": {{"clarity": X, "topic": X, "budget": X, "negatives": X}}, "reason": "одно предложение"}}
"""
)


OFFER_PROMPT_CLAUDE = """Ты пишешь отклик на Kwork от лица fullstack-разработчика. Заказ прошёл скоринг и достоин качественного отклика. Текст пойдёт пользователю как ЧЕРНОВИК для проверки и ручной отправки — поэтому качество критично.

КАТЕГОРИЧЕСКИЕ ПРАВИЛА (нарушение = брак, отклик уйдёт в мусор):

1. Тон. Только "Здравствуйте", не "Привет". Только "Вы" с большой буквы, не "ты". Формальный русский.

2. Никаких длинных тире (em dash, символ "—"). Только обычные дефисы "-" с пробелами. Длинное тире выдаёт AI-генерацию.

3. Никакого вранья про годы опыта и конкретные цифры. ЗАПРЕЩЕНО писать "5 лет", "10+ лет", "более 50 проектов", "опыт с 2020". Используй: "работаю с этим стеком ежедневно", "делал похожие проекты", "есть production-кейсы".

4. НЕ упоминать AI / Claude / Cursor / Copilot / нейросеть / LLM как инструмент написания кода. Это про инструментарий разработчика, клиенту знать не нужно. Исключение: если клиент сам просит интеграцию AI в продукт — тогда упоминаем как функциональность.

5. Цена и сроки НЕ в тексте отклика. Они идут в отдельные поля формы Kwork. В тексте можно упомянуть подход к этапам ("работу разбиваю на этапы: А, Б, В"), но БЕЗ конкретных цифр в рублях и днях.

6. Ровно ОДИН конкретный полезный вопрос в конце. Не три, не пять, не общие ("какие модули важнее всего"). Конкретные практические: "есть ли тестовый стенд API", "какой инструмент трекинга задач", "есть ли уже схема БД", "какие приёмочные критерии для первого этапа". Если уточнять реально нечего - закончить "Готов начать в ближайшие дни."

7. Никаких созвонов / звонков / встреч / Zoom / Телемост / Discord-call. Kwork не разрешает внеплатформенное общение до сделки.

8. Никаких англицизмов. Вместо "solid foundation", "work in progress", "багфиксы", "part-time", "deadline", "delivery", "scope" - русские эквиваленты или объяснение по-человечески.

9. Plain text. Никакого Markdown, заголовков "**", списков с дефисами в начале строки.

10. 400-700 знаков. Длинные простыни не читают.

11. "Портфель" -> "портфолио".

12. Vue.js упомянут в требованиях -> "сделаю на React/Next.js".

ВЫБОР ФОРМАТА.

Структурный (для технически грамотных клиентов с детальным ТЗ - описание заказа > 500 символов И есть тех. детали типа стека / API / интеграций):
- Здравствуйте. Прямой стек попадания + упоминание конкретной технологии из ТЗ.
- "Понимаю задачу так": перефразирование сути в 2-3 предложения.
- "Работу разбиваю на N этапов": этапы без цен и сроков, только границы.
- Один конкретный вопрос или "Готов начать в ближайшие дни."

Короткий (для массовых заказов и простых задач - описание короткое, клиент ждёт быстрых ответов):
- Здравствуйте. Релевантный кейс или конкретное достижение одной фразой.
- Понимание задачи в 1-2 предложения.
- Готовность взяться + общий подход (без архитектуры).
- Один конкретный вопрос или "Готов начать в ближайшие дни."

ЕСЛИ scope_unclear=да:
- НЕ давать полную архитектуру (микросервисы, очереди, конкретные библиотеки)
- Обозначить только общий подход
- Один конкретный вопрос обязателен (без него отклик пустой)

ЕСЛИ site_category=turnkey (сайт/лендинг под ключ без макета):
Можно упомянуть "разработчик-дизайнер - делаю сайт целиком, без отдельного дизайнера".

ПРОФИЛЬ ДЛЯ УПОМИНАНИЯ (только проверенные кейсы):
- Telegram-боты на aiogram - есть production-кейс
- Веб-приложения NestJS + Next.js - есть кейсы (платформа психотестов, два сайта)
- AI-интеграции через Claude/OpenAI API - есть опыт (упоминать только если клиент сам про AI)
- Парсеры и автоматизация - есть кейсы
- Strapi - три проекта в активном опыте
- React/Next.js с GSAP / Framer Motion / Lottie - есть лендинг с серьёзными визуальными эффектами

НЕ УПОМИНАТЬ:
- Telegram Mini Apps - публичных кейсов пока нет
- Vision API в продакшене - нет опыта
- Конкретные годы опыта
- Имена бывших клиентов
- Названия конкретных реализованных Telegram-ботов

КОНТЕКСТ ЗАКАЗА:
Название: {title}
Описание: {description}
Бюджет: {budget}
AI: {is_ai}
ТЗ размыто: {scope_unclear}
Категория сайта: {site_category}

Сгенерируй текст черновика отклика. Только сам текст, без префиксов "Отклик:", "Текст:" и т.п.
"""


REFUSAL_MARKERS = (
    "не входит в мой профиль", "не мой профиль", "не мой стек",
    "не подходит мне", "не подхожу", "рекомендую обратиться",
    "обратитесь к другому", "это не моя сфера", "не моя область",
    "не берусь за", "не могу взяться", "не специализируюсь на",
    "это не входит", "к сожалению, не",
)


def looks_like_refusal(offer: str) -> bool:
    if not offer:
        return False
    lower = offer.lower()
    return any(marker in lower for marker in REFUSAL_MARKERS)


async def _run_haiku_scoring(
    client: anthropic.AsyncAnthropic,
    prompt_template: str,
    prompt_kwargs: dict,
    title: str,
    fallback_type: str = "medium",
) -> dict:
    """v3: один Haiku-вызов для скоринга с retry на JSON-ошибку.

    Возвращает dict с полями score / is_ai / reason / breakdown / type.
    При двух подряд JSON-ошибках возвращает fallback c score=6 и пометкой
    [json_failed] в reason.
    """
    prompt = prompt_template.format(**prompt_kwargs)

    async def _call(extra_instruction: str = "") -> str:
        full_prompt = prompt + ("\n\n" + extra_instruction if extra_instruction else "")
        msg = await client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=500,
            temperature=0,
            messages=[{"role": "user", "content": full_prompt}],
        )
        r = msg.content[0].text.strip()
        if r.startswith("```"):
            r = r.split("```")[1]
            if r.startswith("json"):
                r = r[4:]
            r = r.strip()
        return r

    raw = await _call()
    try:
        return json.loads(raw)
    except json.JSONDecodeError as je:
        logger.warning(
            "JSON parse failed for '%s': %s — retrying with strict instruction",
            title[:60], je,
        )
        strict = (
            "КРИТИЧНО: верни СТРОГО валидный JSON. Все кавычки внутри строк "
            "значений ЭКРАНИРУЙ обратным слешем (\\\"). Не добавляй текст до "
            "или после JSON. Не используй markdown."
        )
        try:
            raw2 = await _call(strict)
            return json.loads(raw2)
        except (json.JSONDecodeError, Exception) as je2:
            logger.warning(
                "JSON parse failed twice for '%s': %s — fallback score=6",
                title[:60], je2,
            )
            return {
                "score": 6,
                "is_ai": False,
                "reason": "[json_failed] fallback: не удалось распарсить JSON от Claude",
                "breakdown": {},
                "type": fallback_type,
            }


FAST_FARM_BLOCK = """⚡ РЕЖИМ ОТЗЫВ-ФАРМ АКТИВЕН.
Цель — набрать отзывы. Простые объективные задачи в приоритете.

+2 БОНУС за признаки гарантированной приёмки:
- объективная задача с измеримым результатом (скрипт работает / парсер вернул правильные данные / файл получен)
- заказчик технически грамотный (по описанию видно)
- простая приёмка без длительного тестирования
- НЕ subjective задача (не "сделайте красиво", не "по вкусу")

При совпадении 2+ из 4 признаков добавь +2 к стартовому скору и явно отметь это в reason."""


async def score_project(
    title: str,
    description: str,
    budget: str,
    deadline: str,
    responses_count: int,
    anthropic_api_key: Optional[str],
    hired_percent: Optional[int] = None,
    buyer_achievements: int = 0,
    farm_mode_active: bool = False,
) -> Dict:
    default_result = {
        "score": 5, "is_ai": False, "reason": "no API key",
        "hard_reject": False, "scope_unclear": False, "no_code_required": None, "site_category": "not_site",
        "hire_rate_penalty": False, "hired_percent": hired_percent,
        "breakdown": {},
        "category": None, "score_big": None, "score_fast": None,
    }
    if not anthropic_api_key:
        return default_result

    is_ai = _has_ai_priority(title, description)

    # v4 Идея 31: AI-маркетинг с 2+ маркерами → hard reject
    ai_class, ai_markers = classify_ai_task(title, description)
    if ai_class == "AI_MARKETING_HARD":
        marker_str = "; ".join(ai_markers)
        logger.info("AIMarketingHardReject [%s]: %s", title[:60], marker_str)
        return {
            "score": 0, "is_ai": is_ai,
            "reason": f"AI-маркетинг / инфобиз (2+ маркера): {marker_str}",
            "hard_reject": True, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    # P1.2: универсальный hard-reject (инфобиз / AI-агентство) — даже для AI-заказов
    always_hr = detect_always_hard_reject(title, description)
    if always_hr:
        logger.info("AlwaysHardReject [%s]: %s", title[:60], always_hr)
        return {
            "score": 0, "is_ai": is_ai,
            "reason": f"infobiz/AI-агентство: '{always_hr}'",
            "hard_reject": True, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    # v4 2.2: автоматизация бронирования с обходом — hard reject
    booking_hr = detect_booking_automation(title, description)
    if booking_hr:
        logger.info("BookingAutoHardReject [%s]: %s", title[:60], booking_hr)
        return {
            "score": 0, "is_ai": is_ai, "reason": booking_hr,
            "hard_reject": True, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    # P0.3: no-code hard-reject (detect_no_code_required учитывает edge-case миграции)
    no_code_hard = detect_no_code_required(title, description)
    if no_code_hard:
        logger.info("NoCodeHardReject [%s]: %s", title[:60], no_code_hard)
        return {
            "score": 0, "is_ai": is_ai,
            "reason": f"требуется no-code: '{no_code_hard}'",
            "hard_reject": True, "scope_unclear": False,
            "no_code_required": no_code_hard, "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    # P0.1: серая / чёрная зона — early hard-reject если 2+ маркера или ≥2 групп.
    # Penalty (-3 / -2) применяется ниже, после Haiku, для слабых сигналов.
    grey_action, grey_penalty, grey_reason = detect_grey_zone(title, description)
    if grey_action == "hard_reject":
        logger.info("GreyZoneHardReject [%s]: %s", title[:60], grey_reason)
        return {
            "score": 0, "is_ai": is_ai, "reason": grey_reason,
            "hard_reject": True, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    # v4 волна 1.5 рег.1: hard reject для CMS/PHP-стеков ВСЕГДА (снято AI-исключение).
    # Escape — если в описании явные маркеры работы РЯДОМ с CMS (наш стек через API).
    hr = _hard_reject_reason(title, description)
    if hr:
        if is_integration_with_external_cms(title, description):
            logger.info(
                "CMS-stack mention with integration context [%s]: '%s' — пропускаем в скоринг",
                title[:60], hr,
            )
        else:
            logger.info("HardReject [%s]: %s", title[:60], hr)
            return {
                "score": 0, "is_ai": is_ai, "reason": f"hard reject: '{hr}'",
                "hard_reject": True, "scope_unclear": False, "no_code_required": None,
                "site_category": "not_site",
                "breakdown": {},
            }

    wanted, limit = _parse_budget_numbers(budget)
    # v3-fix: категорию определяем ДО бюджетного guard'а, иначе FAST-заказы
    # с бюджетом <50к режутся hard-reject'ом и не доходят до FAST-промпта.
    early_category = categorize_by_budget(limit, wanted)
    if early_category == "BIG":
        bi = _budget_too_low(wanted, limit, is_ai)
        if bi:
            logger.info("BudgetLow [%s]: %s", title[:60], bi)
            return {
                "score": 0, "is_ai": is_ai, "reason": bi,
                "hard_reject": True, "scope_unclear": False, "no_code_required": None, "site_category": "not_site",
                "breakdown": {},
                "category": early_category, "score_big": None, "score_fast": None,
            }

    # Категория сайт/лендинг: готовый макет или чистый дизайн = hard reject
    site_category, site_note = detect_site_category(title, description)
    if site_category == "ready_mockup":
        logger.info("SiteCategory [%s]: готовый макет — %s", title[:60], site_note)
        return {
            "score": 0, "is_ai": is_ai,
            "reason": f"верстка по готовому макету — не моё ({site_note})",
            "hard_reject": True, "scope_unclear": False, "no_code_required": None, "site_category": "not_site",
            "breakdown": {},
        }
    if site_category == "pure_design":
        logger.info("SiteCategory [%s]: чистый дизайн — %s", title[:60], site_note)
        return {
            "score": 0, "is_ai": is_ai,
            "reason": f"чистый дизайн без кода — ищут дизайнера ({site_note})",
            "hard_reject": True, "scope_unclear": False, "no_code_required": None, "site_category": "not_site",
            "breakdown": {},
        }

    # Сайт под ключ: верхняя планка должна быть ≥60 000 ₽
    if site_category == "turnkey":
        effective_limit = limit or wanted
        if effective_limit and effective_limit < 60000:
            reason = f"сайт под ключ, верхняя планка {effective_limit:,} < 60 000 ₽"
            logger.info("TurnkeyBudgetLow [%s]: %s", title[:60], reason)
            return {
                "score": 0, "is_ai": is_ai, "reason": reason,
                "hard_reject": True, "scope_unclear": False, "no_code_required": None,
                "site_category": site_category,
                "hire_rate_penalty": False, "hired_percent": hired_percent,
                "breakdown": {},
            }

    # P1.1: hard-reject лендингов B2C / лендингов с бюджетом < 80к
    landing_reason = detect_landing_reject(title, description, limit or wanted)
    if landing_reason:
        logger.info("LandingReject [%s]: %s", title[:60], landing_reason)
        return {
            "score": 0, "is_ai": is_ai, "reason": landing_reason,
            "hard_reject": True, "scope_unclear": False, "no_code_required": None,
            "site_category": site_category,
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    no_code = detect_no_code_required(title, description)
    scope_pen, scope_flags = detect_scope_red_flags(title, description)
    open_pen, open_flags = detect_open_ended_scope(title, description)
    tech_pen, tech_flags = detect_tech_incompetence(title, description)
    api_barrier_pen, api_barrier_reason = detect_external_api_barrier(title, description)
    parsing_pen, parsing_reason = detect_commercial_parsing(title, description)
    nko_caution = detect_nko_caution(title, description)

    critical_unknowns = detect_critical_unknowns(
        title, description, scope_flags, open_flags, tech_flags, wanted, limit,
    )

    no_code_note = f"({no_code}) — cap 4" if no_code else ""
    budget_limit_eff = limit or wanted

    # v3: категория заказа для роутинга промптов (BIG/FAST/DUAL).
    category = categorize_by_budget(limit, wanted)

    big_kwargs = dict(
        title=title,
        description=description[:1500],
        budget=budget,
        deadline=deadline or "не указан",
        responses_count=responses_count,
        is_ai=is_ai,
        no_code=no_code or "нет",
        no_code_note=no_code_note,
        scope_flags=", ".join(scope_flags) if scope_flags else "нет",
        scope_penalty=scope_pen,
        open_ended_flags=", ".join(open_flags) if open_flags else "нет",
        open_ended_penalty=open_pen,
        tech_flags=", ".join(tech_flags) if tech_flags else "нет",
        tech_penalty=tech_pen,
        site_category=site_category,
        site_note=site_note if site_note else "нет",
        api_barrier=api_barrier_reason or "нет",
        api_barrier_penalty=api_barrier_pen,
        parsing_flag=parsing_reason or "нет",
        parsing_penalty=parsing_pen,
        nko_caution="да" if nko_caution else "нет",
    )
    fast_kwargs = {k: big_kwargs[k] for k in (
        "title", "description", "budget", "deadline", "responses_count",
        "is_ai", "no_code", "no_code_note",
        "scope_flags", "scope_penalty",
        "open_ended_flags", "open_ended_penalty",
        "tech_flags", "tech_penalty",
        "site_category", "site_note",
    )}
    fast_kwargs["review_farming_block"] = FAST_FARM_BLOCK if farm_mode_active else ""

    try:
        client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)

        score_big_raw: Optional[int] = None
        score_fast_raw: Optional[int] = None

        if category == "BIG":
            ai_result = await _run_haiku_scoring(client, SCORING_PROMPT, big_kwargs, title)
            score_big_raw = int(ai_result.get("score", 0))
        elif category == "FAST":
            ai_result = await _run_haiku_scoring(
                client, FAST_SCORING_PROMPT, fast_kwargs, title, fallback_type="short",
            )
            score_fast_raw = int(ai_result.get("score", 0))
        else:  # DUAL — оба промпта параллельно
            big_result, fast_result = await asyncio.gather(
                _run_haiku_scoring(client, SCORING_PROMPT, big_kwargs, title),
                _run_haiku_scoring(
                    client, FAST_SCORING_PROMPT, fast_kwargs, title,
                    fallback_type="short",
                ),
            )
            score_big_raw = int(big_result.get("score", 0))
            score_fast_raw = int(fast_result.get("score", 0))
            # Выбор: оба ≥7 → больший (FAST при равенстве); один ≥7 → его; иначе → больший.
            if score_big_raw >= 7 and score_fast_raw >= 7:
                if score_fast_raw >= score_big_raw:
                    ai_result, dual_chosen = fast_result, "FAST"
                else:
                    ai_result, dual_chosen = big_result, "BIG"
            elif score_big_raw >= 7:
                ai_result, dual_chosen = big_result, "BIG"
            elif score_fast_raw >= 7:
                ai_result, dual_chosen = fast_result, "FAST"
            elif score_big_raw >= score_fast_raw:
                ai_result, dual_chosen = big_result, "BIG"
            else:
                ai_result, dual_chosen = fast_result, "FAST"
            logger.info(
                "DualMerge [%s]: BIG=%d FAST=%d → chosen=%s",
                title[:60], score_big_raw, score_fast_raw, dual_chosen,
            )

        score = int(ai_result.get("score", 0))
        is_ai_final = bool(ai_result.get("is_ai", is_ai)) or is_ai
        reason = ai_result.get("reason", "")
        breakdown = ai_result.get("breakdown", {})

        # P3.1: тип заказа для воронки short/long
        order_type = ai_result.get("type", "medium")
        if order_type not in ("short", "medium", "long"):
            order_type = "medium"
        # FAST-категория всегда short
        if category == "FAST":
            order_type = "short"

        scope_unclear = bool(scope_flags) or bool(open_flags) or bool(tech_flags)

        # v4 волна 1.5 рег.2: старый штраф -1 за hire_rate<30% удалён —
        # сигнал теперь учитывается через compute_competition_modifier
        # (комбинация с медальками покупателя).
        hire_rate_penalty = False

        # Штраф за несоответствие бюджета и скоупа
        bsm_penalty, bsm_reason = detect_budget_scope_mismatch(title, description, budget_limit_eff)
        if bsm_penalty:
            old_score = score
            score = max(0, score + bsm_penalty)
            reason = f"{reason}; {bsm_reason}" if reason else bsm_reason
            logger.info(
                "BudgetScopeMismatch [%s]: %d→%d — %s",
                title[:60], old_score, score, bsm_reason,
            )

        # P1.3: штраф за API соцсетей с барьером входа
        if api_barrier_pen:
            old_score = score
            score = max(0, score + api_barrier_pen)
            reason = f"{reason}; {api_barrier_reason}" if reason else api_barrier_reason
            logger.info(
                "APIBarrier [%s]: %d→%d — %s",
                title[:60], old_score, score, api_barrier_reason,
            )

        # P1.4: штраф за парсинг чужих коммерческих источников
        if parsing_pen:
            old_score = score
            score = max(0, score + parsing_pen)
            reason = f"{reason}; {parsing_reason}" if reason else parsing_reason
            logger.info(
                "CommercialParsing [%s]: %d→%d — %s",
                title[:60], old_score, score, parsing_reason,
            )

        # P0.1: штраф за серую зону (один маркер; multi-маркеры уже отсечены до Haiku)
        if grey_action == "penalty" and grey_penalty:
            old_score = score
            score = max(0, score + grey_penalty)
            reason = f"{reason}; {grey_reason}" if reason else grey_reason
            logger.info(
                "GreyZonePenalty [%s]: %d→%d — %s",
                title[:60], old_score, score, grey_reason,
            )

        # === v4 6.1: модификатор по конкурентной среде ===
        comp_mod, comp_reasons = compute_competition_modifier(
            responses_count, hired_percent, buyer_achievements,
        )
        if comp_mod != 0:
            old_score = score
            score = max(0, min(10, score + comp_mod))
            comp_reason_str = "; ".join(comp_reasons)
            reason = f"{reason}; {comp_reason_str}" if reason else comp_reason_str
            logger.info(
                "Competition [%s]: %d→%d — %s",
                title[:60], old_score, score, comp_reason_str,
            )

        # === v4 раздел 7: терминологическая специфичность ===
        term_mod, term_reason = detect_terminology_specificity(title, description)
        if term_mod != 0:
            old_score = score
            score = max(0, min(10, score + term_mod))
            reason = f"{reason}; {term_reason}" if reason else term_reason
            logger.info(
                "Terminology [%s]: %d→%d — %s",
                title[:60], old_score, score, term_reason,
            )

        # === v4 Идея 31: AI-инженерия / AI-маркетинг soft-модификатор ===
        # AI_MARKETING_HARD уже отбит как hard reject выше.
        if ai_class == "AI_MARKETING_SOFT":
            old_score = score
            score = max(0, score - 3)
            ai_note = f"AI-маркетинг (1 маркер: {ai_markers[0]}): -3"
            reason = f"{reason}; {ai_note}" if reason else ai_note
            logger.info(
                "AIMarketingSoft [%s]: %d→%d — %s",
                title[:60], old_score, score, ai_note,
            )
        elif ai_class == "AI_ENGINEERING":
            old_score = score
            score = min(10, score + 1)
            ai_note = f"AI-инженерия ({ai_markers[0]}): +1"
            reason = f"{reason}; {ai_note}" if reason else ai_note
            logger.info(
                "AIEngineering [%s]: %d→%d — %s",
                title[:60], old_score, score, ai_note,
            )

        # === v3: усиления штрафов ===
        for detector_fn, detector_args, label in (
            (detect_terminology_mismatch, (title, description), "TerminologyMismatch"),
            (detect_github_required, (title, description), "GithubRequired"),
            (detect_nda_collector, (title, description, hired_percent), "NDACollector"),
            (detect_streaming_low_budget, (title, description, budget_limit_eff), "StreamingLowBudget"),
            (detect_enterprise_scope_low_budget, (title, description, budget_limit_eff), "EnterpriseScopeLowBudget"),
            (detect_domain_expertise, (title, description), "DomainExpertise"),
        ):
            pen, pen_reason = detector_fn(*detector_args)
            if pen:
                old_score = score
                score = max(0, score + pen)
                reason = f"{reason}; {pen_reason}" if reason else pen_reason
                logger.info(
                    "%s [%s]: %d→%d — %s",
                    label, title[:60], old_score, score, pen_reason,
                )

        logger.info(
            "Score [%s] cat=%s big=%s fast=%s final=%d (ai=%s, no_code=%s) — %s",
            title[:60], category, score_big_raw, score_fast_raw,
            score, is_ai_final, bool(no_code), reason,
        )
        return {
            "score": score,
            "is_ai": is_ai_final,
            "reason": reason,
            "breakdown": breakdown,
            "hard_reject": False,
            "scope_unclear": scope_unclear,
            "no_code_required": no_code,
            "site_category": site_category,
            "hire_rate_penalty": hire_rate_penalty,
            "hired_percent": hired_percent,
            "critical_unknowns": critical_unknowns,
            "order_type": order_type,
            "category": category,
            "score_big": score_big_raw,
            "score_fast": score_fast_raw,
        }
    except Exception as exc:
        logger.warning("Scoring error for '%s': %s", title[:60], exc)
        return {
            "score": 5, "is_ai": is_ai, "reason": f"error: {exc}",
            "hard_reject": False, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
            "category": category, "score_big": None, "score_fast": None,
        }


async def generate_offer_claude(
    title: str,
    description: str,
    budget: str,
    anthropic_api_key: str,
    is_ai: bool = False,
    scope_unclear: bool = False,
    site_category: str = "not_site",
    max_retries: int = 1,
) -> str:
    prompt = OFFER_PROMPT_CLAUDE.format(
        title=title,
        description=description[:1500],
        budget=budget,
        is_ai="да" if is_ai else "нет",
        scope_unclear="да — НЕ давать готовое решение и точную цену" if scope_unclear else "нет",
        site_category=site_category,
    )

    for attempt in range(max_retries + 1):
        try:
            client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)
            message = await client.messages.create(
                model="claude-haiku-4-5-20251001",
                max_tokens=700,
                messages=[{"role": "user", "content": prompt}],
            )
            offer = message.content[0].text.strip()
            if looks_like_refusal(offer):
                logger.warning("Refusal [%s] attempt %d", title[:60], attempt + 1)
                if attempt < max_retries:
                    continue
                return ""
            return offer
        except Exception as exc:
            logger.warning("Offer error [%s] attempt %d: %s", title[:60], attempt + 1, exc)
            if attempt >= max_retries:
                return ""
    return ""


def should_respond(
    score: int,
    quota: Dict,
    is_ai: bool,
    no_code_required: Optional[str] = None,
) -> tuple[bool, str]:
    if quota["remaining"] <= 0:
        return False, "квота на период исчерпана"
    if quota["daily_allowed"] <= 0:
        return False, "дневной лимит (2) исчерпан"
    if no_code_required:
        return False, f"требуется '{no_code_required}' — не моё"

    threshold = quota["score_threshold"]

    if is_ai and score >= 7:
        return True, f"AI, скор {score} (порог {threshold})"
    if score < threshold:
        return False, f"скор {score} < порога {threshold} ({quota['pace']})"
    if quota["reserve_active"] and quota["remaining"] <= RESERVE_QUOTA_FOR_LAST_DAYS:
        if score < 8:
            return False, f"резерв: нужен ≥8, получен {score}"

    return True, f"скор {score} ≥ порога {threshold}"
