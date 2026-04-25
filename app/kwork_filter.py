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

Опыт: 5 лет fullstack, последние 2 года фокус на AI.
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


HARD_REJECT_KEYWORDS = (
    r"\b1c[\s-]*битрикс\b", r"\bбитрикс\b", r"\bbitrix\b",
    r"\bwordpress\b", r"\bвордпресс\b", r"\bна\s+wp\b",
    r"\bтильд[аеу]\b", r"\btilda\b",
    r"\bjoomla\b", r"\bopencart\b", r"\bshopify\b", r"\bwix\b",
    r"\blaravel\b",
    r"\bна\s+php\s+(сайт|проект|разработ)",
    r"\bphp[\s-]*разработчик\s+нужен",
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
    P1.1: hard-reject лендингов / одностраничников.

    Условия:
      1. landing keyword + B2C-ниша → reject независимо от бюджета.
      2. landing keyword + budget_max < 80 000 ₽ → reject (Tilda-территория).
    """
    text = f"{title}\n{description}"
    if not _LANDING_RE.search(text):
        return None

    niche_match = _B2C_NICHES_RE.search(text)
    if niche_match:
        return f"лендинг для B2C-ниши '{niche_match.group(0)}' — Tilda-территория"

    if 0 < budget_limit < 80_000:
        return f"лендинг с бюджетом {budget_limit:,} ₽ < 80 000 ₽ — Tilda-территория"

    return None


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
   - "ИИ-сотрудник", "ИИ-помощник заменит отдел"
   - "автоматизировать всё с помощью AI"
   - "познакомить агентов с источниками", "обучить нейросеть на наших данных"
     при отсутствии конкретики по архитектуре
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

JSON без markdown:
{{"score": 1-10, "is_ai": true/false, "breakdown": {{"stack": X, "budget": X, "ai": X, "quality": X, "penalties": X}}, "reason": "одно предложение"}}
"""
)


OFFER_PROMPT_CLAUDE = """Ты — fullstack с AI-специализацией (5 лет). Пишешь отклик на Kwork для заказа со скором 7+.

ПРАВИЛА:
- Plain text. Никакого Markdown, списков.
- 400-800 знаков.
- От первого лица.
- Стек: Next.js, React, NestJS, TypeScript, PostgreSQL, Socket.IO, Docker, Claude API, OpenAI, aiogram, Qdrant.
- НИКОГДА: PHP, Laravel, Django, Bitrix, WordPress, Angular, n8n, Make, Zapier.
- Vue.js → "сделаю на React/Next.js".

ЗАПРЕЩЕНО:
- Предлагать созвон, звонок, встречу в любой форме ("созвонимся", "готов к созвону",
  "давайте обсудим на звонке", "Телемост", "Zoom"). Kwork не разрешает внеплатформенное
  общение до сделки. Если нужно уточнение — задать один вопрос текстом в конце отклика.
- Писать плавающую цену ("от X, точно после уточнения деталей"). Указывать одну конкретную
  цифру. Если scope неясен — брать нижнюю адекватную оценку.
- Выдавать полную архитектуру (конкретные библиотеки, микросервисы, очереди) если ТЗ
  размыто (scope_unclear=да). Только общий подход + один конкретный вопрос.
- Использовать слово "портфель" — только "портфолио".
- Ссылаться на конкретные выполненные проекты по Telegram-ботам ("делал несколько
  проектов", "примеры в профиле", "покажу работающий бот"). Позиционирование только
  через стек и опыт — без упоминания конкретных реализованных ботов.

ИЗБЕГАЙ:
1. Уточняй вопросом в тексте, не предлагай созвон.
2. Завышать срок для part-time (15-25 ч/нед) — 80 часов работы = 4-5 недель.

ПОЗИЦИОНИРОВАНИЕ:
- AI: "Специализируюсь на AI-интеграциях на собственном коде (Claude API, OpenAI, агенты на function calling) с fullstack-бэкендом Next.js/NestJS."
- Бот: "Работаю с Telegram Bot API и aiogram на Python, Node.js/TypeScript — знаю стек изнутри."
- Fullstack: "Работаю на Next.js + NestJS + PostgreSQL, специализируюсь на [конкретика]."
- Real-time: "Socket.IO — рабочая лошадка, делал чаты и live-дашборды."
- Сайт/лендинг под ключ (site_category=turnkey): "Разработчик-дизайнер — делаю сайт целиком, от концепта до деплоя. Дизайн создаю сам (Claude Design + Figma), реализую на Next.js + Tailwind. Без отдельного дизайнера, без посредников, один исполнитель на весь цикл."

СТРУКТУРА:
1: Приветствие + позиционирование
2-3: Что зацепило + что планируется (без архитектуры если scope_unclear=да)
4 (только если НЕТ макета): "Дизайн разработаю сам — современный, с анимациями при желании."
5: Срок реалистичный
6: Бюджет
7: Если есть что уточнить — один конкретный вопрос, органично вписанный в последний
   абзац. Не начинать с "Вопрос:", не выносить отдельным блоком. Если уточнять нечего —
   просто "Готов начать."

ЦЕНЫ (× 1.25 комиссия):
Простая 5-10 дней: 40-70к
Средняя 2-3 недели: 70-130к
Крупная 3-4 недели: 130-220к
AI ×1.3-1.5.
Не выше допустимого заказчика. Минимум 40к.
scope_unclear=да → "от X ₽".

Срок >7 дней → разбивка 2-3 этапов.

===
Название: {title}
Описание: {description}
Бюджет: {budget}
AI: {is_ai}
ТЗ размыто: {scope_unclear}
Категория сайта: {site_category}
===
===
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


async def score_project(
    title: str,
    description: str,
    budget: str,
    deadline: str,
    responses_count: int,
    anthropic_api_key: Optional[str],
    hired_percent: Optional[int] = None,
) -> Dict:
    default_result = {
        "score": 5, "is_ai": False, "reason": "no API key",
        "hard_reject": False, "scope_unclear": False, "no_code_required": None, "site_category": "not_site",
        "hire_rate_penalty": False, "hired_percent": hired_percent,
        "breakdown": {},
    }
    if not anthropic_api_key:
        return default_result

    is_ai = _has_ai_priority(title, description)

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

    if not is_ai:
        hr = _hard_reject_reason(title, description)
        if hr:
            logger.info("HardReject [%s]: %s", title[:60], hr)
            return {
                "score": 0, "is_ai": False, "reason": f"hard reject: '{hr}'",
                "hard_reject": True, "scope_unclear": False, "no_code_required": None, "site_category": "not_site",
                "breakdown": {},
            }

    wanted, limit = _parse_budget_numbers(budget)
    bi = _budget_too_low(wanted, limit, is_ai)
    if bi:
        logger.info("BudgetLow [%s]: %s", title[:60], bi)
        return {
            "score": 0, "is_ai": is_ai, "reason": bi,
            "hard_reject": True, "scope_unclear": False, "no_code_required": None, "site_category": "not_site",
            "breakdown": {},
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

    prompt = SCORING_PROMPT.format(
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

    try:
        client = anthropic.AsyncAnthropic(api_key=anthropic_api_key)

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
            result = json.loads(raw)
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
                result = json.loads(raw2)
            except (json.JSONDecodeError, Exception) as je2:
                logger.warning(
                    "JSON parse failed twice for '%s': %s — fallback score=5",
                    title[:60], je2,
                )
                scope_unclear = bool(scope_flags) or bool(open_flags) or bool(tech_flags)
                return {
                    "score": 6, "is_ai": is_ai,
                    "reason": "[json_failed] fallback: не удалось распарсить JSON от Claude",
                    "hard_reject": False, "scope_unclear": scope_unclear,
                    "no_code_required": no_code, "site_category": site_category,
                    "hire_rate_penalty": False, "hired_percent": hired_percent,
                    "critical_unknowns": critical_unknowns,
                    "breakdown": {},
                }

        score = int(result.get("score", 0))
        is_ai_final = bool(result.get("is_ai", is_ai)) or is_ai
        reason = result.get("reason", "")
        breakdown = result.get("breakdown", {})

        scope_unclear = bool(scope_flags) or bool(open_flags) or bool(tech_flags)

        # Штраф за низкий процент найма
        hire_rate_penalty = False
        if hired_percent is not None and hired_percent < 30:
            old_score = score
            score = max(0, score - 1)
            hire_rate_penalty = True
            penalty_note = f"штраф -1: hire_rate {hired_percent}%"
            reason = f"{reason}; {penalty_note}" if reason else penalty_note
            logger.info(
                "HireRatePenalty [%s]: %d→%d (hire_rate=%d%%)",
                title[:60], old_score, score, hired_percent,
            )

        # Штраф за несоответствие бюджета и скоупа
        bsm_penalty, bsm_reason = detect_budget_scope_mismatch(title, description, limit or wanted)
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

        logger.info(
            "Score [%s]: %d (ai=%s, no_code=%s) — %s",
            title[:60], score, is_ai_final, bool(no_code), reason,
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
        }
    except Exception as exc:
        logger.warning("Scoring error for '%s': %s", title[:60], exc)
        return {
            "score": 5, "is_ai": is_ai, "reason": f"error: {exc}",
            "hard_reject": False, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
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
