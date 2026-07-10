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
- Парсинг ТОЛЬКО собственных данных заказчика или через официальный API под его аккаунтом.
  Парсинг чужих маркетплейсов/витрин/контактов/поисковой выдачи — НЕ подходит (серая зона, reject).
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
    # === Правки июль 2026, Группа 5: bare "Make" как ядро связки ===
    # Заказ Beds24 → Make → SendPulse получал скор 9: matcher ловил только
    # 'make.com'. Ловим Make как оркестратор по контексту связки/стрелок, но НЕ
    # голое \bmake\b (англ. "make" даёт ложняки).
    r"\b(?:через|на|в)\s+make\b",
    r"\bmake\b\s*[→\-–—>]\s*\w",
    r"\w\s*[→\-–—>]\s*\bmake\b",
    r"(?:связк\w+|цепочк\w+|интеграци\w+|сценари\w+|пайплайн|pipeline)\s+(?:\w+\s+){0,4}make\b",
    r"\balbato\b",
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
    # === Правки июль 2026, Группа 1: капча-решалки / обход антибот-защиты ===
    # Запрещённый предмет: обход капчи/антибота как услуга. Часто подаётся как
    # "AI/автоматизация", поэтому именно ALWAYS (бьёт даже AI-заказы), до бонусов.
    r"решалк\w*\s+капч",
    r"(?:обход\w*|обойти|распозна\w+|решени\w+|решать|автомат\w+\s+решени\w+)\s+капч",
    r"капч\w*\s+(?:солвер|solver|solving|автореш)",
    r"puzzle[\s-]*captcha",
    r"captcha[\s-]*(?:solver|solving|bypass)",
    r"(?:solve|bypass)[\s-]*captcha",
    r"(?:обход\w*|обойти|решени\w+|распозна\w+)\s+(?:turnstile|hcaptcha|recaptcha|ddos[\s-]*guard|\bddg\b)",
    r"(?:turnstile|hcaptcha|recaptcha)\s+(?:обход|обойти|решени\w+|распозна\w+|solver|solving|bypass)",
    r"(?:обход\w*|обойти)\s+антибот",
    # === Правки июль 2026, Группа 1: уникализаторы контента ===
    # Обслуживают массовый залив в обход площадочных фильтров (детекция дублей,
    # copyright/Content ID). Назначение — обход детекции.
    r"уникализатор",
    r"уникализ\w+\s+(?:видео|фото|контент|изображен|аудио|текст|ролик)",
    r"(?:обход\w*|обойти)\s+(?:детекц\w+|детект\w+)\s+(?:дубл|повтор)",
    r"(?:обход\w*|обойти)\s+(?:copyright|content[\s-]*id|авторск\w+\s+прав|защит\w+\s+авторск)",
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
    # Аудит 10.07: клиент явно отстраивается от шаблонов/конструкторов или
    # задаёт планку топ-SaaS — ищет кодовый авторский лендинг (наша зона).
    r"не\s+(?:нужен\s+)?(?:шаблонн\w+|конструкторн\w+)",
    r"а\s+не\s+конструкторн\w+",
    r"\bpremium\b|премиальн\w+",
    r"уровня\s+(?:linear|notion|stripe|framer|vercel|figma|apple)",
)
_AUTHORSHIP_RE = re.compile("|".join(AUTHORSHIP_MARKERS), re.IGNORECASE)


HARD_REJECT_KEYWORDS = (
    # Русские склонения через \w* (исправление: \bбитрикс\b не ловил 'битриксе/битрикса')
    r"\b1c[\s-]*битрикс\w*", r"\bбитрикс\w*", r"\bbitrix\b",
    r"\bwordpress\b", r"\bвордпресс\w*", r"\bна\s+wp\b",
    # русское сокращение WP + типичные плагины
    r"\bна\s+(сайт\w*\s+)?вп\b", r"\bвп[\s-]?сайт", r"\bсайт\s+на\s+вп\b",
    r"\ball[\s-]?import\b",  # WP-плагин массового импорта
    r"\bwoocommerce\b|\bвуком(мерс|ерс)\w*|\bвокоммерс\w*",
    r"\bwp[\s-]?(импорт|товар|плагин|сайт)",
    r"импорт\s+\d+\s*(к|тыс|т)\s+товар\w+\s+(на|в)\s+(вп|wp|wordpress|вордпрес)",
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
    # v4 Идея 34: Bitrix24 модули (не интеграция через API) и YClients основная
    r"модул\w+\s+(для|в|под)\s+bitrix\s*24",
    r"bitrix\s*24\s+(приложен|разработ|модул|настр)",
    r"приложен\w+\s+(для|в)\s+bitrix\s*24",
    r"\byclients\b\s+(как\s+основ|основная|разработ\w+\s+интеграц|настройк)",
    r"работа\s+(с|в)\s+yclients\s+(как|основн)",
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


# === v4 Идея 32: расширенный hard reject парсинга коммерческих источников ===

# Прямые домены/имена — hard reject при упоминании в контексте парсинга
COMMERCIAL_PARSING_TARGETS = (
    # Маркетплейсы и каталоги
    r"\b(wildberries|вайлдберриз|вб)\b",
    r"\bozon\b|\bозон\b",
    r"\bavito\b|\bавито\b",
    r"\baliexpress\b|\bалиэкспресс|\baliexp\b",
    r"\btaobao\b|\bтаобао",
    r"\betsy\b|\bэтси\b",
    r"\bamazon\b|\bамазон",
    # B2B каталоги (российские)
    r"\betm\.ru|\betm[\s-]?каталог|\bauvix|\bcvg\b|\bpult[\s-]?av",
    # Авто
    r"\bencar\b|\bautohome\b|\bdcarauto|\bautoria",
    # Недвижимость
    r"\bциан\b|\bcian\.ru|\bдомклик\b|\bметр[\s-]?квадрат",
    # Новости
    r"\bbloomberg\b|\breuters\b|\brbk\.ru|\bкоммерсант|\bлента\.ру|\bforbes\b",
    r"\btechcrunch\b|the\s+information",
    # Соц. сети и платформы
    r"\bтикток\b|\btiktok\b\s+(парс|спарс|скрэйп)",
    r"\binstagram\b\s+(парс|спарс|скрэйп)",
    r"\byoutube\b\s+(парс|спарс|скрэйп|скачать|yt[\s-]?dlp)",
    r"\blinkedin\b\s+(парс|спарс|профил|контакт)",
    r"\bheadhunter\b\s+(парс|спарс|резюм|вакансий\s+массов)",
    r"\bhh\.ru\b\s+(парс|спарс)\s+(резюм|вакансий)",
)
_COMMERCIAL_PARSING_TARGETS_RE = re.compile("|".join(COMMERCIAL_PARSING_TARGETS), re.IGNORECASE)

# Контексты в которых парсинг почти всегда означает коммерческий источник
COMMERCIAL_PARSING_CONTEXTS = (
    r"парс\w+\s+товар\w+\s+(для|в)\s+(интернет[\s-]?магазин|свой\s+магазин|сайт)",
    r"парс\w+\s+цен\w+\s+(конкурент|маркетплейс|чужих)",
    r"сбор\s+данных\s+по\s+недвижимост",
    r"парс\w+\s+автомобил\w+\s+(из\s+(корея|кореи|кит))",
    r"парс\w+\s+новост\w+\s+(популярн|крупн)",
    r"база\s+контактов\s+youtube",
    r"парс\w+\s+резюм\s+с\s+(hh|headhunter|linkedin)",
)
_COMMERCIAL_PARSING_CONTEXTS_RE = re.compile("|".join(COMMERCIAL_PARSING_CONTEXTS), re.IGNORECASE)

# "API есть но лимит/медленно" — сильный сигнал серой зоны
API_BYPASS_FLAGS = (
    r"api\s+есть\s+но\s+(лимит|медленн|неудобн|плат|дорог)",
    r"\bобход\w+\s+api[\s-]?лимит",
    r"api\s+не\s+(подходит|устраивает)\s+парс",
    r"вместо\s+api\s+парс\w+",
)
_API_BYPASS_RE = re.compile("|".join(API_BYPASS_FLAGS), re.IGNORECASE)


def detect_commercial_parsing_v2(title: str, description: str) -> Optional[str]:
    """v4 Идея 32: hard reject парсинга коммерческих источников.

    Логика:
      1. Прямое упоминание коммерческого источника (Wildberries/Циан/Encar/etm.ru)
         + парсинг-намерение → hard reject (whitelist игнорируется, эти источники
         серые по умолчанию).
      2. Контекстный паттерн (товары для магазина) → hard reject, но whitelist
         может спасти (свой сайт).
      3. "API есть но лимит" → hard reject.
    """
    text = f"{title}\n{description}"

    has_parsing_intent = bool(
        re.search(r"\bпарс\w+|\bспарс\w+|\bскрэйп|\bscrap|сбор\s+данных", text, re.IGNORECASE)
    )
    if not has_parsing_intent:
        return None

    # 1. Прямые targets — whitelist не отменяет (источники сами по себе серые)
    direct = _COMMERCIAL_PARSING_TARGETS_RE.search(text)
    if direct:
        return f"парсинг коммерческого источника '{direct.group(0)}' — нарушение ToS"

    # 2/3. Косвенные — whitelist спасает
    if _PARSING_OK_RE.search(text):
        return None

    context = _COMMERCIAL_PARSING_CONTEXTS_RE.search(text)
    if context:
        return f"парсинг в коммерческом контексте: '{context.group(0)}'"

    bypass = _API_BYPASS_RE.search(text)
    if bypass:
        return f"парсинг в обход API ('{bypass.group(0)}') — серая зона"

    return None


# === v4 Идея 35: несоответствие профиля fullstack ===

PROFILE_MISMATCH_PATTERNS = (
    # Сисадминство для чужих хостингов
    (
        re.compile(
            r"(аудит|настройк\w+|оптимизац\w+|миграц\w+)[\s\S]{0,80}?"
            r"(beget|reg\.ru|timeweb|firstvds|cpanel|ispmanager|fastpanel|hostland)|"
            r"(beget|reg\.ru|timeweb|cpanel|ispmanager|fastpanel)[\s\S]{0,80}?"
            r"(настройк|оптимизац|аудит)",
            re.IGNORECASE,
        ),
        "сисадминство для чужих хостингов",
    ),
    # Аудит существующих систем без разработки
    (
        re.compile(
            r"\bаудит\s+(сайт|систем|приложен|инфраструктур|кода)|"
            r"провести\s+аудит|нужен\s+аудит",
            re.IGNORECASE,
        ),
        "аудит без разработки",
    ),
    # 1С-программирование (любого вида)
    (
        re.compile(
            r"\b1с\s+(программ|разработ|конфигурац|обработк|отчёт)|"
            r"\b1с[\s-]?предприят|программист\s+1с",
            re.IGNORECASE,
        ),
        "1С-программирование",
    ),
    # Desktop native
    (
        re.compile(
            r"\bdelphi\b|\bpascal\s+(native|разработ)|\bvb6\b|\bvisual\s+basic\b|"
            r"\bwpf\b\s+приложен|\bwinforms?\b",
            re.IGNORECASE,
        ),
        "desktop native (Delphi/Pascal/WPF/WinForms)",
    ),
    # Native mobile (хотя в HARD_REJECT уже есть, тут как дубль для -3 если не сработало)
    (
        re.compile(
            r"\bkotlin\s+(android|приложен|разработ)|"
            r"\bobjective[\s-]?c\b|"
            r"flutter\s+нативн|jetpack\s+compose|swiftui",
            re.IGNORECASE,
        ),
        "native mobile",
    ),
    # Embedded и IoT
    (
        re.compile(
            r"\barduino\b|\besp32\b|\besp8266\b|raspberry\s+pi\s+прошивк|"
            r"микроконтроллер|stm32|\baltium\s+designer|"
            r"\bazбук\w+\s+(морзе|кода)|связь\s+азбукой",
            re.IGNORECASE,
        ),
        "embedded/IoT",
    ),
    # ML с обучением моделей (не использование API)
    (
        re.compile(
            r"обучен\w+\s+(кастомн|собственн|своей)\s+модел|"
            r"train\w+\s+(custom|own)\s+model|"
            r"\bfine[\s-]?tun\w+\s+(c\s+нул|with\s+own\s+data)|"
            r"датасет\s+(на\s+|для\s+)?(\d+\s+тысяч|сбор)|"
            r"\btraining\s+pipeline|trained\s+from\s+scratch",
            re.IGNORECASE,
        ),
        "ML обучение кастомных моделей",
    ),
    # 3D/WebGL/Three.js глубокого уровня
    (
        re.compile(
            r"three\.?js\s+(сложн|шейдер|глубок|professional)|"
            r"\bwebgl\s+(шейдер|глубок|сложн)|"
            r"3d[\s-]?визуализ\w+\s+(глубок|сложн|интерактивн)|"
            r"\bblender\s+(моделир|анимац)",
            re.IGNORECASE,
        ),
        "3D/WebGL глубокого уровня",
    ),
    # Геймдев — уже частично есть в hard reject, дублирующая страховка
    (
        re.compile(
            r"\bunity[\s-]?(разработ|игр|приложен)|\bunreal\s+engine|"
            r"\bgodot\s+(разработ|игр)|игров\w+\s+движок",
            re.IGNORECASE,
        ),
        "геймдев",
    ),
    # Криптотрейдинг боты для бирж
    (
        re.compile(
            r"торгов\w+\s+бот\w+\s+(для|на)\s+(бирж|binance|bybit|kucoin|okx)|"
            r"крипто[\s-]?(трейд|арбитраж)\s+бот|"
            r"\barbitrage\s+bot|hft\s+бот",
            re.IGNORECASE,
        ),
        "криптотрейдинг боты",
    ),
    # Перенос/миграция серверов как основная задача
    (
        re.compile(
            r"перенос\s+сервер\w*\s+(как\s+основ|основная|задача)|"
            r"миграц\w+\s+(сервер|сайт)\s+(как\s+основ|основная)|"
            r"\bперевезти\s+(сайт|сервер|проект)\s+(с\s+|на\s+)\w+\s+на\s+\w+",
            re.IGNORECASE,
        ),
        "миграция серверов как основная задача",
    ),
    # SMTP / спам-защита / прогрев IP — как основная задача
    (
        re.compile(
            r"настройк\w+\s+smtp\s+(как\s+основ|основная|разверн)|"
            r"прогрев\s+ip|warmup\s+ip|защит\w+\s+от\s+спам[\s\S]{0,30}?как\s+основн",
            re.IGNORECASE,
        ),
        "SMTP/spam-protection как основное",
    ),
    # Волна 3 идея 42: корпоративная почта на домене
    (
        re.compile(
            r"корпоративн\w+\s+почт\w+\s+на\s+домен|"
            r"настр\w+\s+почт\w+\s+(\w+\s+){0,3}(\d+\s+(адрес|ящик|почт))|"
            r"\bmx[\s-]?запис\w+\s+(настр|сконфиг|пропис)|"
            r"\bimap\b|\bpop3\b\s+(настр|конфиг)",
            re.IGNORECASE,
        ),
        "корпоративная почта/MX",
    ),
    # Волна 3 идея 42: освобождение/чистка диска хостинга
    (
        re.compile(
            r"освободить\s+(место|диск)\s+на\s+(хостинг|сервер)|"
            r"почистить\s+(диск|сервер|хостинг)|"
            r"анализ\s+диск\w+\s+(хостинг|сервер)|"
            r"забит\s+(диск|сервер)",
            re.IGNORECASE,
        ),
        "обслуживание хостинга",
    ),
    # Волна 3 идея 42: миграция сервера как основная задача (отдельно от dev-flow)
    (
        re.compile(
            r"перенести\s+сервер\s+на\s+нов\w+\s+vps|"
            r"миграц\w+\s+сервер\w+\s+(с\s+\w+\s+)?на\s+нов",
            re.IGNORECASE,
        ),
        "миграция сервера как самостоятельная задача",
    ),
    # Волна 3 идея 42: выпуск SSL без deployment-контекста (cert как основная задача)
    (
        re.compile(
            r"выпустить\s+ssl\s+(сертификат\w*\s+)?и\s+установить\s+на\s+хостинг|"
            r"купить\s+ssl[\s\S]{0,30}?установить\s+на\s+(сайт|хостинг)",
            re.IGNORECASE,
        ),
        "SSL без deployment-контекста",
    ),
)


# Волна 3 идея 42: whitelist для fullstack-deploy (Категория А).
# Если в тексте есть deploy-маркер РЯДОМ с маркером нашего стека —
# не штрафуем как профиль-mismatch, это нормальная работа fullstack-разработчика.
_FULLSTACK_DEPLOY_VERB_RE = re.compile(
    r"\b(развернуть|разверну|развёртывани|поднять|подними|деплой\w*|"
    r"задеплоить|запустить\s+на\s+(сервер|vps)|поставить\s+на\s+сервер|"
    r"настроить\s+nginx|"
    r"настроить\s+reverse[\s-]?proxy|"
    r"добавить\s+ssl|подключить\s+ssl|выпустить\s+ssl\s+для\s+приложен|"
    r"привязать\s+домен)",
    re.IGNORECASE,
)
_FULLSTACK_STACK_RE = re.compile(
    r"\b(fastapi|nest\.?js|next\.?js|nuxt|nodejs|node\.js|node\b|"
    r"python\s+(приложен|проект|сервис|бот)|"
    r"\baiogram\b|aiohttp|"
    r"telegram[\s-]?бот|tg[\s-]?бот|"
    r"react|vue|express|fastify|"
    r"docker[\s-]?compose|docker\s+контейнер|"
    r"\bcoolify\b|\bdokku\b|\bpm2\b|\bsystemd\b|"
    r"мо(й|его)\s+(приложен|проект|сервис|бот|api))",
    re.IGNORECASE,
)


def _is_fullstack_deploy_context(title: str, description: str) -> bool:
    """Волна 3 идея 42, Категория А: deploy fullstack-проекта — НЕ mismatch."""
    text = f"{title}\n{description}"
    return bool(_FULLSTACK_DEPLOY_VERB_RE.search(text) and _FULLSTACK_STACK_RE.search(text))


def detect_profile_mismatch(title: str, description: str) -> tuple[int, str]:
    """Волна 4 п.3.2: штраф ТОЛЬКО для системных/низкоуровневых задач.

    По стратегии Claude Code: прикладные задачи (CRUD/UI/интеграции/парсинг/
    доработка/скрипты/отчётность) на ЛЮБОМ стеке (Rust/Go/Tauri/VBA/C#/Java/
    1С/Delphi) — НЕ штрафуем. Узкий стек = низкая конкуренция.

    Штрафуем только реально системные направления где Claude Code не заменяет
    экспертизу: embedded, ML обучение моделей, 3D/WebGL глубокого уровня,
    геймдев, криптотрейдинг для бирж, новые архитектуры/драйверы.
    """
    # Категория А (waved 3): deploy fullstack-приложения — нормальный flow.
    if _is_fullstack_deploy_context(title, description):
        return 0, ""

    text = f"{title}\n{description}"
    for regex, name in SYSTEM_LEVEL_MISMATCH_PATTERNS:
        m = regex.search(text)
        if m:
            return -5, f"системная задача вне профиля ({name}): '{m.group(0).strip()[:60]}'"
    return 0, ""


# Волна 4 п.3.2: только реально СИСТЕМНЫЕ направления (Claude Code не закроет).
SYSTEM_LEVEL_MISMATCH_PATTERNS = (
    # Embedded / IoT — нужна работа с железом, прошивки, низкий уровень
    (
        re.compile(
            r"\barduino\b|\besp32\b|\besp8266\b|raspberry\s+pi\s+прошивк|"
            r"микроконтроллер|stm32|\baltium\s+designer|"
            r"\bazбук\w+\s+(морзе|кода)|связь\s+азбукой|"
            r"прошивк\w+\s+(железа|устройства|плат|чип)|"
            r"\bfpga\b|\bvhdl\b|\bverilog\b",
            re.IGNORECASE,
        ),
        "embedded/IoT — работа с железом",
    ),
    # ML с обучением кастомных моделей с нуля (не fine-tuning готовых)
    (
        re.compile(
            r"обучен\w+\s+модел\w+\s+с\s+нул|train\s+from\s+scratch|"
            r"новая\s+архитектур\w+\s+нейросет|"
            r"\btraining\s+pipeline|trained\s+from\s+scratch|"
            r"phd[\s\-]?уровн|research\s+ml|академическ\w+\s+ml",
            re.IGNORECASE,
        ),
        "ML обучение архитектур с нуля",
    ),
    # 3D/WebGL глубокий уровень (шейдеры, движки), не базовая Three.js-сцена
    (
        re.compile(
            r"three\.?js\s+(сложн|шейдер|глубок|professional)|"
            r"\bwebgl\s+(шейдер|глубок|сложн|raymarch)|"
            r"3d[\s-]?визуализ\w+\s+(глубок|сложн|интерактивн\s+физик)|"
            r"\bshader\b\s+(программир|разработ)|"
            r"\bray\s+(marching|tracing)\b",
            re.IGNORECASE,
        ),
        "3D/WebGL шейдеры/движок",
    ),
    # Gamedev на Unity/Unreal — игровые движки глубокого уровня
    (
        re.compile(
            r"\bunity[\s-]?(разработ|игр|приложен|3d)|"
            r"\bunreal\s+engine\s+(разработ|игр|пайплайн)|"
            r"\bgodot\s+(разработ|игр)|игров\w+\s+движок\s+(разработ|с\s+нуля)",
            re.IGNORECASE,
        ),
        "геймдев",
    ),
    # Криптотрейдинг боты для бирж — специфическая зона с финрисками
    (
        re.compile(
            r"торгов\w+\s+бот\w+\s+(для|на)\s+(бирж|binance|bybit|kucoin|okx)|"
            r"крипто[\s-]?(трейд|арбитраж)\s+бот|"
            r"\barbitrage\s+bot|\bhft\b\s+бот|market[\s\-]?making\s+бот",
            re.IGNORECASE,
        ),
        "криптотрейдинг боты для бирж",
    ),
    # Кастомные аллокаторы / низкоуровневая оптимизация / драйверы
    (
        re.compile(
            r"кастомн\w+\s+аллокатор|custom\s+allocator|"
            r"разработ\w+\s+драйвер|kernel\s+module|"
            r"низкоуровнев\w+\s+оптимизац|low[\s\-]?level\s+optimiz|"
            r"performance[\s\-]?critical\s+(systemн?|систем|kernel)",
            re.IGNORECASE,
        ),
        "системная разработка / драйверы",
    ),
)


# === Волна 3 идея 43: B2C-услуговые сайты как прокси для хард-реджект CMS ===

# Доменные паттерны / тематика — B2C-услуги, почти всегда на WP/Tilda/Битрикс
_B2C_DOMAIN_RE = re.compile(
    r"\b(yurist|advokat|salon|beauty|master|klinika|clinic|"
    r"optika|optic|stomatolog|dent|"
    r"remont(?!_|\.ru)|stroy|uslugi|avtoservis|\bsto\b|"
    r"barber|nails|nail|spa)[a-z0-9\-]*\.(ru|com|net|info|pro|biz)"
    r"|\b(юрист|адвокат|клиника|оптика|салон\s+(красот|спа)|"
    r"автосервис|стоматолог|массаж|парикмахер|маникюр|барбер)\b",
    re.IGNORECASE,
)
# Контекстные маркеры B2C-сайта (без явного домена)
_B2C_CONTEXT_RE = re.compile(
    r"сайт\s+(мастер|специалист|услуг|компани\w+\s+услуг)|"
    r"корпоративн\w+\s+сайт(?!\s+(на|с)\s+(nestjs|next|fastapi|react|django))|"
    r"сайт[\s-]?визитк|"
    r"лендинг\s+услуг",
    re.IGNORECASE,
)
# Маркеры доработки/правок чужого сайта
_SITE_MODIFICATION_RE = re.compile(
    r"добавить\s+[\w\s,]{0,40}?на\s+сайт|"
    r"доработать\s+сайт|"
    r"починить\s+сайт|"
    r"поправить\s+сайт|"
    r"обновить\s+сайт|"
    r"внести\s+правки\s+(в\s+)?сайт|"
    r"редактирован\w+\s+сайт",
    re.IGNORECASE,
)
# Маркеры явного современного стека (исключение из B2C-штрафа)
_EXPLICIT_MODERN_STACK_RE = re.compile(
    r"\b(nestjs|nest\.?js|next\.?js|nuxt|fastapi|django(\s+rest)?|"
    r"flask|fastify|express|nodejs|node\.js|react|vue|svelte|"
    r"strapi|sanity|directus|payload\s+cms|headless)\b",
    re.IGNORECASE,
)


def detect_b2c_service_site(title: str, description: str) -> tuple[int, str]:
    """Волна 3 идея 43: B2C-услуговые сайты — почти всегда WP/Tilda/Битрикс24.

    Эмпирически подтверждено на yurist-72.ru (Битрикс24). Юрист/салон/оптика/
    клиника/автосервис «без указания стека» работают на хард-реджект CMS.
    Если есть явный современный стек (NestJS/Next.js/Django/etc) — НЕ штрафуем.

    Returns:
        (-3, reason) при совпадении B2C-домен/контекст + site-modification без стека.
        (0, "") иначе.
    """
    text = f"{title}\n{description}"

    has_b2c = bool(_B2C_DOMAIN_RE.search(text) or _B2C_CONTEXT_RE.search(text))
    if not has_b2c:
        return 0, ""

    has_site_mod = bool(_SITE_MODIFICATION_RE.search(text))
    if not has_site_mod:
        return 0, ""

    if _EXPLICIT_MODERN_STACK_RE.search(text):
        return 0, ""

    b2c_match = _B2C_DOMAIN_RE.search(text) or _B2C_CONTEXT_RE.search(text)
    return -3, f"B2C-услуговый сайт ('{b2c_match.group(0)[:40]}') без явного стека: -3"


# === v4 Идея 36: копирование продукта по референсу ===

COPY_REFERENCE_FLAGS = (
    r"\bкопи\w+\b\s+(сайт|приложен|калькулятор|конструктор|сервис)",
    r"сделать\s+(как|по\s+образц|такое\s+же)\s+(сайт|приложен|сервис)",
    r"по\s+образц\w+\s+(этого|вот\s+этого)\s+сайт",
    r"\bклон\b\s+(сайт|приложен)|clone\s+of",
    r"повторить\s+(функционал|сайт|сервис)\s+(как|по)",
)
_COPY_REFERENCE_RE = re.compile("|".join(COPY_REFERENCE_FLAGS), re.IGNORECASE)

# Маркеры "вдохновлено" — НЕ копия, не штрафовать
INSPIRATION_FLAGS = (
    r"по\s+референс|вдохновл\w+\s+(дизайн|пример)|"
    r"в\s+стиле\s+\w+|похож\w+\s+(на\s+|стиль)",
)
_INSPIRATION_RE = re.compile("|".join(INSPIRATION_FLAGS), re.IGNORECASE)


def detect_copy_by_reference(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """v4 Идея 36: копирование по референсу с низким бюджетом.

    -2 если есть прямая ссылка + слово "копия/как образец" + budget < 100k.
    Не штрафовать если "по референсу/вдохновлено" вместо "копия".
    """
    text = f"{title}\n{description}"
    if _INSPIRATION_RE.search(text):
        # "Вдохновлено" — это нормально, не штрафуем
        return 0, ""
    match = _COPY_REFERENCE_RE.search(text)
    if not match:
        return 0, ""
    # есть ли URL?
    has_url = bool(re.search(r"https?://", text))
    if not has_url:
        return 0, ""
    if budget_limit and budget_limit >= 100_000:
        return 0, ""
    return -2, f"копирование по референсу при бюджете <100к: '{match.group(0)}'"


# === v6 (волна 4) Группа 1: HARD REJECT — серая зона / ToS / юр.риски ===

# 1.1 Парсинг маркетплейсов и работа с карточками — категоричный hard reject.
# Исключение: официальный API под seller-аккаунтом (advert-api.wildberries.ru и т.п.)
_MARKETPLACE_NAMES_RE = re.compile(
    r"\bwildberries\b|\bwb\b|вайлдберр\w*|дики[ех]\s+ягод\w*|"
    r"\bozon\b|\bозон\b|"
    r"\bavito\b|\bавито\b|"
    r"\byandex[\s.\-]?market\b|я[\s.\-]?маркет|яндекс[\s.\-]?маркет|"
    r"\betsy\b|\bэтси\b|"
    r"\bamazon\b|\bамазон|"
    r"\bмаркетплейс\w*",
    re.IGNORECASE,
)
_MARKETPLACE_ACTION_RE = re.compile(
    r"\bпарс\w+|\bспарс\w+|\bскрейп\w*|\bscrape\w*|"
    r"мониторинг\s+(цен|товар|конкурент|маркетплейс)|"
    r"собрать\s+(данные|товары|отзыв|карточ|цен)|"
    r"работа\s+с\s+карточ\w+|"
    r"создан\w+\s+карточ\w+|"
    r"перевыпуск\s+карточ\w+|"
    r"найти\s+аналогичн\w+\s+товар|"
    r"средн\w+\s+цен\w+\s+по\s+рынку|"
    r"затян\w+\s+отзыв\w+|тянуть\s+отзыв\w+|"
    r"выгруз\w+\s+(товар|карточ|отзыв)",
    re.IGNORECASE,
)
# Whitelist: легитимная работа через официальный API под seller-аккаунтом
_MARKETPLACE_OFFICIAL_API_RE = re.compile(
    r"\bofficial\s+api\b|официальн\w+\s+api|"
    r"\badvert[\s\-]?api\.wildberries|seller[\s\-]?api|"
    r"\bseller\.wildberries|suppliers[\s\-]?api|"
    r"\bsvoj?\s+(seller|селлер|продавец)|свой\s+(seller|селлер|кабинет\s+продавца)|"
    r"кабинет\s+(продавца|селлера|рекламн)|"
    r"\bnashi\s+(карточк|товар)|наши\s+(собственн\w+\s+)?(карточк|товар)",
    re.IGNORECASE,
)


def detect_marketplace_work(title: str, description: str) -> Optional[str]:
    """Волна 4 п.1.1: парсинг маркетплейсов / работа с карточками WB/Ozon/Avito/etc.

    Hard reject при сочетании имени маркетплейса + действия (парсинг/мониторинг/
    работа с карточками/перевыпуск/затянуть отзывы).
    Исключение: официальный API под собственным seller-аккаунтом — НЕ reject.
    """
    text = f"{title}\n{description}"
    if not _MARKETPLACE_NAMES_RE.search(text):
        return None
    if not _MARKETPLACE_ACTION_RE.search(text):
        return None
    if _MARKETPLACE_OFFICIAL_API_RE.search(text):
        return None  # официальный API под seller-аккаунтом — норма
    name_match = _MARKETPLACE_NAMES_RE.search(text)
    action_match = _MARKETPLACE_ACTION_RE.search(text)
    return (
        f"маркетплейс ({name_match.group(0)}) + {action_match.group(0)[:30]} — "
        f"парсинг чужих витрин/нарушение ToS"
    )


# 1.2 Парсинг мобильных приложений — reverse engineering, против Apple/Google ToS.
_MOBILE_APP_PARSING_RE = re.compile(
    r"(\bпарс\w+|\bспарс\w+|\bвыгруз\w+|reverse[\s\-]?engineer)[\s\S]{0,80}?"
    r"(приложен\w+\s+(ios|android|iphone)|мобильн\w+\s+приложен|"
    r"apps\.apple\.com|play\.google\.com|appstore|app\s+store|"
    r"google\s+play|\bipa\b|\bapk\b)|"
    r"(приложен\w+\s+(ios|android)|мобильн\w+\s+приложен|"
    r"apps\.apple\.com|play\.google\.com)[\s\S]{0,80}?"
    r"(\bпарс\w+|\bспарс\w+|\bвыгруз\w+|\bперехват\w*)",
    re.IGNORECASE,
)


def detect_mobile_app_parsing(title: str, description: str) -> Optional[str]:
    """Волна 4 п.1.2: парсинг/реверс мобильных приложений → hard reject."""
    text = f"{title}\n{description}"
    m = _MOBILE_APP_PARSING_RE.search(text)
    if m:
        return (
            f"парсинг/реверс мобильного приложения: '{m.group(0)[:60]}' — "
            f"против Apple/Google ToS, 272 УК РФ"
        )
    return None


# 1.3 Обёртка чужого закрытого сервиса в свой API через имитацию браузера.
_BROWSER_IMITATION_RE = re.compile(
    r"имитир\w+\s+браузер|имитац\w+\s+браузер|"
    r"понять\s+как\s+идут\s+запрос|"
    r"оберну\w+\s+в\s+api|упакова\w+\s+в\s+api|"
    r"написать\s+api\s+для\s+(google|сервис|сайт)|"
    r"reverse[\s\-]?engineer\w*\s+(api|сервис)",
    re.IGNORECASE,
)
_CLOSED_SERVICE_RE = re.compile(
    r"google\s+flow|imagefx|image[\s\-]?fx|"
    r"\bseedance\b|seedance\s*2|"
    r"\bsuno\b|udio\.com|"
    r"закрыт\w+\s+(сервис|api)|нет\s+(публичн|официальн)\w+\s+api|"
    r"\bесли\s+получится\b",  # явный риск-сигнал — заказчик сам не уверен
    re.IGNORECASE,
)


def detect_browser_imitation_wrapper(title: str, description: str) -> Optional[str]:
    """Волна 4 п.1.3: обёртка закрытого сервиса в свой API → hard reject."""
    text = f"{title}\n{description}"
    if not _BROWSER_IMITATION_RE.search(text):
        return None
    if not _CLOSED_SERVICE_RE.search(text):
        return None
    return (
        "обёртка закрытого сервиса в свой API через имитацию браузера — "
        "обход защиты, нестабильность, юр.риск"
    )


# 1.4 Game botting — против EULA игр, RMT-риск.
_GAME_BOTTING_RE = re.compile(
    r"ферм\w+\s+(в\s+|для\s+)?(игр|game)|"
    r"автокликер\s+для\s+игр|автоматизац\w+\s+ферм|"
    r"макрос\w*\s+для\s+игр|"
    r"бот\s+для\s+(аккаунт\w+\s+в\s+игр|игр\w+|game)|"
    r"собирать\s+(jewels|gems|coins|ресурс\w+)\s+в\s+(игр|game)|"
    r"выполнять\s+квест\w+\s+(в\s+)?(игр|на\s+аккаунт)|"
    r"bot\s+for\s+(game|mmo|mmorpg)|"
    r"game\s+of\s+war|clash\s+of\s+clans|brawl\s+stars|"
    r"\bmmo\b\s+бот|\bmmorpg\b\s+бот|"
    # Волна 5 (2.1): Minecraft / AFK / обход защиты игрового сервера
    r"\bminecraft\b|\bмайнкрафт\w*|"
    r"\bafk\b|\bафк\b|"
    r"обход\w*\s+(афк|afk|защит\w+\s+сервера)|"
    r"анти[\s-]?афк|anti[\s-]?afk|"
    r"(рыбалк|фарм)\w*\s+(в\s+)?(minecraft|майнкрафт|игр)|"
    r"ботинг|game[\s-]?botting|"
    r"бот\s+для\s+(roblox|роблокс|wow|world\s+of\s+warcraft|tarkov|тарков|dota|cs)",
    re.IGNORECASE,
)

# Волна 5 (2.1): сбор контактов компаний/людей для рассылок (152-ФЗ).
_CONTACT_HARVEST_RE = re.compile(
    r"спарс\w+\s+(телефон|почт|email|e-mail|контакт|номер)|"
    r"собрать\s+(базу\s+)?(телефон|почт|email|e-mail|контакт|номеров)|"
    r"парс\w+\s+(телефон|почт|email|контакт)|"
    r"сбор\s+(контакт|телефон|email|почт)\w*\s+(компани|клиент|людей|организац)|"
    r"кто\s+реклам\w+\s+в\s+(директ|яндекс|google|гугл)|"
    r"база\s+(контакт|телефон|email|почт)\w*\s+для\s+рассылк|"
    r"лидген\w*\s+баз|собрать\s+лиды\s+(из|с)\s+",
    re.IGNORECASE,
)


def detect_contact_harvest(title: str, description: str) -> Optional[str]:
    """Волна 5 (2.1): сбор контактов для рассылок → hard reject (152-ФЗ)."""
    m = _CONTACT_HARVEST_RE.search(f"{title}\n{description}")
    if m:
        return f"сбор контактов для рассылок ('{m.group(0)[:40]}') — нарушение 152-ФЗ"
    return None


# Волна 5 (1.4): парсинг поисковой выдачи (смена региона + ключи) → серая зона.
_SERP_PARSING_RE = re.compile(
    r"парс\w+\s+(выдач|поиск\w+\s+выдач|серп|serp)|"
    r"парс\w+\s+(яндекс|google|гугл)\s*(поиск|выдач|серп)?|"
    r"мен\w+\s+регион\w*\s+и\s+(вбива|парс|собира)|"
    r"сбор\s+позиц\w+\s+(в\s+)?(яндекс|google|гугл|поиск)|"
    r"парс\w+\s+поисков\w+\s+(результат|подсказ)",
    re.IGNORECASE,
)


def detect_serp_parsing(title: str, description: str) -> Optional[str]:
    """Волна 5 (1.4): парсинг поисковой выдачи → hard reject (обход ToS)."""
    m = _SERP_PARSING_RE.search(f"{title}\n{description}")
    if m:
        return f"парсинг поисковой выдачи ('{m.group(0)[:40]}') — обход ToS поисковика"
    return None


# Волна 5 (1.5): незаполненный шаблон с {плейсхолдерами} в фигурных скобках.
_TEMPLATE_PLACEHOLDER_RE = re.compile(r"\{[a-zA-Zа-яА-Я_][\w\s]{1,30}\}")


def detect_template_placeholders(title: str, description: str) -> Optional[str]:
    """Волна 5 (1.5): описание содержит {плейсхолдеры} — незаполненный шаблон."""
    text = f"{title}\n{description}"
    matches = _TEMPLATE_PLACEHOLDER_RE.findall(text)
    if len(matches) >= 2:
        return f"незаполненный шаблон-заготовка (плейсхолдеры: {', '.join(matches[:3])})"
    return None


# Волна 5 (1.5): pixel-perfect вёрстка по чужому макету / копия сайта / HTML-письма.
_PIXEL_PERFECT_RE = re.compile(
    r"pixel[\s-]?perfect|пиксель[\s-]?перфект|"
    r"вёрстк\w+\s+по\s+(макет|psd|figma|фигм|образц)|"
    r"сверста\w+\s+по\s+(макет|psd|figma|фигм|образц)|"
    r"html[\s-]?письм\w+\s+по\s+(образц|шаблон|макет|демо)|"
    r"(аналог|копи\w+)\s+сайт\w+\s+(как|по\s+образц|tilda|тильд)?|"
    r"сделать\s+как\s+(вот\s+)?этот\s+сайт|"
    r"свёрстать\s+(лендинг|страниц)\s+по\s+готов\w+\s+дизайн",
    re.IGNORECASE,
)


def detect_pixel_perfect(title: str, description: str) -> Optional[str]:
    """Волна 5 (1.5): pixel-perfect вёрстка по чужому макету → не наше."""
    m = _PIXEL_PERFECT_RE.search(f"{title}\n{description}")
    if m:
        return f"pixel-perfect вёрстка по чужому макету ('{m.group(0)[:40]}') — не профиль"
    return None


# === Волна 30.06 A3: копирование конкретного сайта / сайт-донор ===
_SITE_DONOR_RE = re.compile(
    r"сайт[\s\-]?донор|"
    r"\bдонор\w*\b[\s\S]{0,40}?(сайт|структур|контент)|(сайт|структур|контент)[\s\S]{0,40}?\bдонор\w*\b|"
    r"копи\w+\s+сайт|скопир\w+\s+сайт|копирован\w+\s+сайт|"
    r"структур\w+\s+и\s+контент\s+бер[её]тся\s+с|"
    r"по\s+образц\w+\s+сайт|переносим\s+(контент\s+)?с\s+сайт|"
    r"перенести\s+(весь\s+)?(контент|структур)\s+с\s+сайт|"
    r"сделать\s+как\s+на\s+сайте\s+https?://",
    re.IGNORECASE,
)
# Исключение: "аналог X" / "упрощённый аналог" — разработка похожего с нуля, НЕ копия.
_ANALOG_NOT_COPY_RE = re.compile(
    r"аналог\s+(jira|trello|notion|asana|\w+)|упрощённ\w+\s+аналог|"
    r"похож\w+\s+(по\s+смыслу|функционал)|свой\s+аналог|с\s+нуля\s+похож",
    re.IGNORECASE,
)


def detect_site_donor(title: str, description: str) -> Optional[str]:
    """Волна 30.06 A3: копирование КОНКРЕТНОГО сайта (контент/структура) → reject.

    По видимому тексту описания, не дожидаясь вложения. НЕ триггерит на
    "аналог Jira/Trello" (разработка похожего с нуля).
    """
    text = f"{title}\n{description}"
    m = _SITE_DONOR_RE.search(text)
    if not m:
        return None
    # "аналог X" без явного донора/копирования — это не копирование сайта.
    if _ANALOG_NOT_COPY_RE.search(text) and not re.search(
        r"сайт[\s\-]?донор|копирован\w+\s+сайт|структур\w+\s+и\s+контент\s+бер",
        text, re.IGNORECASE,
    ):
        return None
    return f"копирование сайта / сайт-донор ('{m.group(0)[:40].strip()}') — hard reject"


def detect_game_botting(title: str, description: str) -> Optional[str]:
    """Волна 4 п.1.4: game botting → hard reject (против EULA, RMT-риск)."""
    text = f"{title}\n{description}"
    m = _GAME_BOTTING_RE.search(text)
    if m:
        return f"game botting ('{m.group(0)[:40]}') — против EULA игр, RMT-риск"
    return None


# 1.5 Обход anti-spam соцсетей — заказчик прямо говорит про блокировки.
_ANTISPAM_BYPASS_RE = re.compile(
    r"уже\s+блокирова\w+\s+(акк|нас)|"
    r"соцсет\w+\s+(стал\w*\s+)?(блочит|блокирует|банит)|"
    r"чтобы\s+не\s+(заблокирова|забанили|спалили|палиться)|"
    r"с\s+(задержк|пинг|тайминг)\w*\s+(между\s+действиями\s+)?чтобы\s+не\s+(пали|спалил|банил)|"
    r"сами\s+понимаете\b|"
    r"антибан\s+(для\s+)?(соц|insta|tiktok|telegram)",
    re.IGNORECASE,
)
_SOCIAL_AUTOMATION_RE = re.compile(
    r"автоматизац\w+\s+(в\s+)?(соц|instagram|tiktok|telegram|vk|youtube|threads)|"
    r"автоответ\w*\s+(на\s+)?(коммент|сообщен|reply)|"
    r"\bответ\s+на\s+коммент|"
    r"авторепост|автопостинг|массов\w+\s+(лайк|подписк|коммент|инвайт)|"
    r"автоматическ\w+\s+(ответ|постинг|инвайт|подписк)\s+(в|на)\s+(соц|инст|insta|tg|telegram)",
    re.IGNORECASE,
)


def detect_antispam_bypass(title: str, description: str) -> Optional[str]:
    """Волна 4 п.1.5: явное намерение обхода anti-spam соцсетей → hard reject."""
    text = f"{title}\n{description}"
    if not _ANTISPAM_BYPASS_RE.search(text):
        return None
    if not _SOCIAL_AUTOMATION_RE.search(text):
        return None
    return "обход anti-spam соцсетей (заказчик прямо упоминает блокировки) — против ToS"


# === v5 Идея 40: формализация криминал-категорий (hard reject) ===
# Только реальные юридические риски (УК/КоАП РФ).

CRIMINAL_CATEGORIES_FLAGS = (
    # 1. Букмекерская тематика заблокированных в РФ
    (
        re.compile(
            r"\b1[\s-]?x[\s-]?bet\b|\bmelbet\b|\bмостбет\b|\bmostbet\b|"
            r"\bпари[\s-]?матч\b|\bparimatch\b|\b1win\b|"
            r"букмекер\w*\s+(контор|компани)|"
            r"беттинг\w*\s+(сайт|разработ|бот)|"
            r"казино\s+онлайн\s+(разработ|бот|сайт)",
            re.IGNORECASE,
        ),
        "букмекерство/казино заблокированные в РФ (ст. 14.1.1 КоАП, 171.2 УК)",
    ),
    # 2. Пробив персональных данных / слитые базы (ФЗ-152)
    (
        re.compile(
            r"\bглаз\s+бога\b|\bgetcontact\b|\bgsm[\s-]?info\b|"
            r"пробив\s+(по\s+)?(номер|телефон|имени|паспорт)|"
            r"слит\w+\s+баз\w+\s+(данных|контакт|клиент)|"
            r"бот\s+для\s+пробив|пробив\w+\s+бот",
            re.IGNORECASE,
        ),
        "пробив ПДн из сливов (нарушение ФЗ-152)",
    ),
    # 3. Ворованные / общие / купленные API-ключи
    (
        re.compile(
            r"общ\w+\s+(аккаунт|подписк|key|ключ)\s+(openai|claude|anthropic|midjourney|chatgpt|cursor)|"
            r"куплен\w+\s+(аккаунт|подписк|key|ключ)\s+(openai|claude|chatgpt|cursor)|"
            r"shared\s+(account|api[\s-]?key)|"
            r"использу\w+\s+чуж\w+\s+(ключ|подписк|аккаунт)",
            re.IGNORECASE,
        ),
        "ворованные/общие API-ключи коммерческих сервисов",
    ),
    # 4. Криминал с детьми, оружием, наркотиками
    (
        re.compile(
            r"\bдетск\w+\s+(порно|интим)|"
            r"\bcsam\b|\bcsem\b|"
            r"продаж\w+\s+(оружи|нарко|психотроп|спайс)|"
            r"\bкладмен|\bзакладк\w+\s+(бот|сайт)|"
            r"\bдарквеб[\s-]?(маркет|шоп)|даркнет[\s-]?(маркет|шоп)",
            re.IGNORECASE,
        ),
        "оружие/наркотики/CSAM",
    ),
)


def detect_criminal_categories(title: str, description: str) -> Optional[str]:
    """v5 Идея 40: hard reject реального юридического риска (УК/КоАП РФ).

    Только эти категории остаются как абсолютный reject:
    1) букмекерство/казино в РФ
    2) пробив ПДн из сливов
    3) ворованные/общие API-ключи
    4) оружие/наркотики/CSAM

    Госплатформы+капча (визовые/мемориалы) — уже отбиваются в
    detect_booking_automation.
    """
    text = f"{title}\n{description}"
    for regex, label in CRIMINAL_CATEGORIES_FLAGS:
        m = regex.search(text)
        if m:
            return f"{label}: '{m.group(0).strip()}'"
    return None


# === Волна 4 Группа 2.1: фильтр заказчика против новичков (-5) ===

_NEWBIE_FILTER_RE = re.compile(
    r"не\s+работаю?\s+с\s+(исполнител\w+\s+)?без\s+(заказов|отзыв|опыта)|"
    r"только\s+(с\s+)?(проверенн\w+|опытн\w+|с\s+рейтинг)|"
    r"только\s+(с\s+)?(\d+\+?|более\s+\d+)\s+(заказ|отзыв|зак)|"
    r"только\s+(с\s+)?рейтинг\w+\s+от\s+\d|"
    r"новичк\w+\s+(прошу\s+не|не)\s+(беспокои|отклика)|"
    r"требуется\s+портфолио\s+(из\s+)?(\d+|N|N\+)\s+(работ|проект|кейс)|"
    r"исполнител\w+\s+(уровн\w+\s+)?(восходящ\w+|высш\w+)\s+",
    re.IGNORECASE,
)


def detect_newbie_filter(title: str, description: str) -> tuple[int, str]:
    """Волна 4 п.2.1: заказчик прямо фильтрует новичков. -5.

    Для профиля George (1 отзыв, без медали уровня) такие заказы = заведомый
    отсев, коннект тратится впустую.
    """
    m = _NEWBIE_FILTER_RE.search(f"{title}\n{description}")
    if m:
        return -5, f"фильтр заказчика против новичков: '{m.group(0)[:50]}' — коннект впустую"
    return 0, ""


# === Правки июль 2026, Группа 2: вайбкодинг + заниженный бюджет (-4) ===
# Слово «вайбкодинг» само по себе нейтрально. В связке с бюджетом ниже оценочной
# стоимости объёма (прокси — floor 30 000 ₽) это маркер клиента, который считает
# ИИ-работу дешёвкой: высокий риск торга вниз и конфликта. Если бюджет адекватен —
# это грамотный клиент, штрафа нет.
VIBECODING_LOWBALL_MAX = 30000

_VIBECODING_RE = re.compile(
    r"вайб[\s-]?код\w*|vibe[\s-]?cod\w*",
    re.IGNORECASE,
)


def detect_vibecoding_devaluation(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """Правка июль 2026 п.2: вайбкодинг + заниженный бюджет = обесценивающий клиент.

    -4 если маркер «вайбкодинг/vibe coding» И бюджет < 30 000 ₽. Если бюджет
    адекватен — это грамотный клиент, штрафа нет (0).
    """
    m = _VIBECODING_RE.search(f"{title}\n{description}")
    if not m:
        return 0, ""
    if budget_limit and budget_limit < VIBECODING_LOWBALL_MAX:
        return -4, (
            f"вайбкодинг ('{m.group(0)}') + бюджет {budget_limit:,} ₽ < "
            f"{VIBECODING_LOWBALL_MAX:,} ₽ — обесценивающий клиент, риск торга вниз"
        )
    return 0, ""


# === Правки июль 2026, Группа 3: ценз заказчика по числу отзывов профиля ===
# Заказчик требует «от N отзывов». Если N больше текущего числа отзывов на
# профиле — отклик заведомо отсеется по формальному критерию, коннект сгорит.
# Требует явный предлог-требование (от/минимум/…) или «N+ отзыв» / «только с N
# отзывами», чтобы не путать с отзыв-фармом («соберу 500 отзывов»).
_REVIEWS_CENSUS_RE = re.compile(
    r"(?:от|более|не\s+менее|минимум|не\s+ниже)\s+(\d+)\+?\s*"
    r"(?:положительн\w+\s+)?(?:отзыв|выполненн\w+\s+заказ|заказ\w*\s+в\s+профил)"
    r"|(\d+)\+\s*(?:отзыв|заказ\w*\s+в\s+профил)"
    r"|только\s+(?:с\s+)?(?:исполнител\w+\s+)?(?:от\s+)?(\d+)\+?\s*отзыв",
    re.IGNORECASE,
)


def detect_reviews_census(
    title: str, description: str, profile_reviews_count: int
) -> Optional[str]:
    """Правка июль 2026 п.3: заказчик требует «от N отзывов» больше, чем есть.

    Возвращает причину hard-reject если требуемое число отзывов N больше числа
    отзывов на профиле (profile_reviews_count). Иначе None. При росте профиля
    порог сдвигается сам — значение берётся из конфига.
    """
    required = 0
    for m in _REVIEWS_CENSUS_RE.finditer(f"{title}\n{description}"):
        for g in m.groups():
            if g:
                required = max(required, int(g))
    if required > profile_reviews_count:
        return (
            f"ценз заказчика: требуется от {required} отзывов, "
            f"на профиле {profile_reviews_count} — не проходишь, коннект впустую"
        )
    return None


# === Волна 4 Группа 2.2: перенос инфры на исполнителя при микробюджете (-6) ===

_INFRA_TRANSFER_RE = re.compile(
    r"установк\w+\s+на\s+ваш\s+сервер|развернуть\s+у\s+вас|"
    r"хостинг\s+(с\s+)?ваш\w+\s+сторон|"
    r"работать\s+24/?7\s+у\s+вас|"
    r"бот\s+должен\s+работать\s+24/?7\s+на\s+ваш|"
    r"запустить\s+на\s+ваш\w+\s+сервер",
    re.IGNORECASE,
)
_NO_INFRA_RE = re.compile(
    r"у\s+меня\s+(нет\s+сервер|сервера\s+нет|своего\s+сервер\s+нет)|"
    r"не\s+разбира\w+\s+в\s+(хостинг|сервер)|"
    r"\bне\s+умею\s+(хостинг|серверы|деплой)|"
    r"свой\s+(хостинг|сервер)\s+нет",
    re.IGNORECASE,
)


def detect_infra_transfer_microbudget(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """Волна 4 п.2.2: перенос инфры на исполнителя за разовый микроплатёж. -6.

    Триггер: установка/работа 24/7 у исполнителя + отсутствие инфры у
    заказчика + бюджет ≤3000₽.
    """
    if not budget_limit or budget_limit > 3000:
        return 0, ""
    text = f"{title}\n{description}"
    if not _INFRA_TRANSFER_RE.search(text):
        return 0, ""
    if not _NO_INFRA_RE.search(text):
        return 0, ""
    return -6, (
        f"перенос инфры на исполнителя при бюджете {budget_limit:,} ₽ — "
        f"отрицательная экономика (разовый микроплатёж против бессрочного хостинга)"
    )


# === Волна 4 Группа 4.1: бонус ML/RAG прикладные ===

# Прикладные ML/CV/OCR-маркеры (бонус +2)
# Прикладной ML — зона Георгия (детекция/OCR/прогноз на готовых инструментах).
# Волна 30.06 A2: fine-tuning/LoRA/дообучение УБРАНЫ отсюда — они ML-инженерия (ведро 2).
APPLIED_ML_MARKERS = (
    r"\byolo\b|yolov\d+",
    r"компьютерн\w+\s+зрени|\bcomputer\s+vision\b|\bcv\b\s+(модел|задач)",
    r"детекц\w+\s+(объект|лиц|номер)|классификац\w+\s+(объект|изображен|товар)",
    r"\bocr\b|tesseract|paddleocr|easyocr|"
    r"распознав\w+\s+(текст|документ|таблиц|номер|лиц)",
    r"прогнозиров\w+\s+на\s+данных|прогноз\w+\s+(спрос|цен|временн)|"
    r"\bregression\s+model\b|forecast(ing|ed)?",
    r"ml[\s\-]?модел\w+\s+под\s+задач",
    r"анализ\s+данных\s+с\s+(ml|нейросет)",
    r"\bsklearn\b|\bxgboost\b|\bcatboost\b|\blightgbm\b",
)
_APPLIED_ML_RE = re.compile("|".join(APPLIED_ML_MARKERS), re.IGNORECASE)

# Research/PhD-уровень — бонус НЕ давать
RESEARCH_ML_MARKERS = (
    r"новая\s+архитектур\w+\s+нейросет",
    r"исследовательск\w+\s+(задач|проект)",
    r"phd[\s\-]?уровн",
    r"научн\w+\s+(разработ|статья|публикац)",
    r"опубликовать\s+статью",
    r"кастомн\w+\s+аллокатор|низкоуровнев\w+\s+оптимизац\w+\s+ml",
)
_RESEARCH_ML_RE = re.compile("|".join(RESEARCH_ML_MARKERS), re.IGNORECASE)


# === Волна 30.06 A2: ведро 2 — ML-инженерия и self-host инференс (НЕ зона Георгия) ===
# Обучение/дообучение LLM, self-host инференс, диффузия для генерации картинок.
# Продакшн-кейсов нет. core requirement → дисквалификатор (SKIP), вскользь → -2.
ML_ENGINEERING_MARKERS = (
    r"\bfine[\s\-]?tun\w+|\bfine[\s\-]?тюн\w+|дообучен\w+\s+(модел|llm|нейросет)",
    r"\blora\b|\bqlora\b|\bpeft\b",
    r"обучен\w+\s+(модел|llm|нейросет)\w*\s+(с\s+нул|на\s+свои|под\s+клиент)|обучить\s+(модел|llm)",
    r"\bvllm\b|\btgi\b|text[\s\-]?generation[\s\-]?inference",
    r"\btensorrt\b|\btriton\s+inference|gpu[\s\-]?инференс|инференс\s+на\s+gpu",
    r"квантизац\w+|\bgguf\b|\bawq\b|\bgptq\b|\bbitsandbytes\b",
    r"self[\s\-]?host\w*\s+(llm|модел|инференс)|локальн\w+\s+(llm|инференс\s+модел)|развернуть\s+(llm|llama|mistral|qwen)\s+на\s+сво",
    r"hugging\s*face[\s\S]{0,40}?(обуч|дообуч|train|fine)",
    r"\bflux\.?1?\b|\bstable\s+diffusion\b|\bsdxl\b|\bcomfyui\b|\bcontrolnet\b|"
    r"\bautomatic1111\b|\ba1111\b",
    r"генерац\w+\s+(лиц|аватар|селфи|портрет)\w*\s+через\s+(sd|stable|нейросет|диффуз)",
    r"диффузи\w+\s+модел|обучить\s+lora\s+(на|для)\s+(картин|лиц|стил|фото)",
)
_ML_ENGINEERING_RE = re.compile("|".join(ML_ENGINEERING_MARKERS), re.IGNORECASE)

# Маркеры "вскользь" — маркер ведра 2 в скобках как альтернатива, не core.
_ML_ENG_INCIDENTAL_RE = re.compile(
    r"\((или\s+)?(можно\s+)?(дообуч|fine[\s\-]?tun|обуч)\w*[^)]{0,40}\)|"
    r"как\s+альтернатив\w+\s+(можно\s+)?дообуч|"
    r"опционально\s+(можно\s+)?дообуч",
    re.IGNORECASE,
)


def detect_ml_engineering(title: str, description: str) -> tuple[str, str]:
    """Волна 30.06 A2: ML-инженерия / self-host инференс / диффузия — вне зоны.

    Returns:
        ("hard_reject", reason) — core requirement (обычное совпадение);
        ("penalty", reason) — маркер вскользь (в скобках "или дообучить") → -2;
        ("", "") — нет маркеров ведра 2.
    """
    text = f"{title}\n{description}"
    m = _ML_ENGINEERING_RE.search(text)
    if not m:
        return "", ""
    if _ML_ENG_INCIDENTAL_RE.search(text):
        return "penalty", f"ML-инженерия вскользь ('{m.group(0)[:30]}'): -2"
    return "hard_reject", (
        f"ML-инженерия / self-host инференс / диффузия ('{m.group(0)[:30]}') — "
        f"вне зоны, обучение и self-host моделей без production-кейсов"
    )


def detect_applied_ml_bonus(title: str, description: str) -> tuple[int, str]:
    """Волна 4 п.4.1: бонус +2 за прикладной ML (YOLO/CV/OCR/прогноз).

    НЕ давать бонус если research/PhD-маркеры ИЛИ маркеры ведра 2 (ML-инженерия) —
    те обрабатываются раньше как дисквалификатор (A2).
    """
    text = f"{title}\n{description}"
    if _RESEARCH_ML_RE.search(text) or _ML_ENGINEERING_RE.search(text):
        return 0, ""
    m = _APPLIED_ML_RE.search(text)
    if m:
        return 2, f"прикладной ML ({m.group(0)[:30]}): +2 (сильная зона)"
    return 0, ""


# === v5 Идея 39: no-code под видом разработки ===
# Заказчик использует инженерную терминологию + крошечный бюджет
# = посмотрел туториал по n8n/Make, думает что blocks → product.

NOCODE_UNDER_DEV_VOCAB = (
    r"\bscaffold|\bкаркас\w*|workflow\s+chain|automation\s+chain|"
    r"\bконтракт\w+\s+(между|задач|агент)|task\s+state|workspace\s+structure|"
    r"мульти[\s-]?агент\w+\s+систем|multi[\s-]?agent\s+system|"
    r"task\s+orchestrat|агент[\s-]?оркестр|"
    r"\bautomation\s+(of|для)\s+(\d+|several|нескольк)\s+(agents|агент)"
)
_NOCODE_VOCAB_RE = re.compile(NOCODE_UNDER_DEV_VOCAB, re.IGNORECASE)


def detect_nocode_under_dev(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """v5 Идея 39: инженерные термины + крошечный бюджет = заказчик
    ищет no-code решение (n8n/Make.com/Zapier), не понимая разницу.

    Триггер: vocab-маркер + бюджет 2000-10000 рублей. → -2 с пометкой.
    Большие бюджеты — отдаём в обычный скоринг (заказчик действительно
    готов платить за разработку).
    """
    if budget_limit and (budget_limit < 2000 or budget_limit > 10000):
        return 0, ""
    match = _NOCODE_VOCAB_RE.search(f"{title}\n{description}")
    if not match:
        return 0, ""
    return -2, (
        f"возможно no-code под видом разработки ('{match.group(0)}', "
        f"бюджет {budget_limit:,} ₽) — проверь n8n/Make.com"
    )


# === v5 Идея 31 (мягкая): размытость задачи -1 ===

VAGUE_SCOPE_FLAGS = (
    r"обсудим\s+(подробн|реализ|детал)",
    r"пишите\s+(ваши\s+)?предложен",
    r"стоимост\w+\s+указан\w+\s+формально",
    r"бюджет\s+(уточн|обсуд|приблизительн)",
    r"\bпотом\s+обсудим",
    r"присылайте\s+(ваши\s+)?(оценки|варианты)",
    r"условия\s+(обсуждаем|договорные)",
)
_VAGUE_SCOPE_RE = re.compile("|".join(VAGUE_SCOPE_FLAGS), re.IGNORECASE)


def detect_vague_scope(title: str, description: str) -> tuple[int, str]:
    """v5 Идея 31: размытость ТЗ ('обсудим', 'пишите предложения')."""
    match = _VAGUE_SCOPE_RE.search(f"{title}\n{description}")
    if match:
        return -1, f"размытое ТЗ ('{match.group(0)}'): -1"
    return 0, ""


# === v4 Идея 37: MAX мессенджер как платформа (бонус +1) ===

MAX_MESSENGER_FLAGS = (
    r"\bMAX\b\s+(мессенджер|бот|клиент|чат|messenger|приложен|api|sdk)",
    r"мессенджер\s+max\b",
    r"бот\s+(в|для|на)\s+MAX\b",
    r"max\.ru/[\w/:%-]+",
    r"\bMasterBot\b",
    r"\bpython[\s-]?max[\s-]?bot\b|max[\s-]?bot[\s-]?api",
    r"max[\s-]?bot[\s-]?api[\s-]?client",
    r"mini\s+apps?\s+(в|для)\s+max",
    r"\bdev\.max\.ru\b",
)
_MAX_MESSENGER_RE = re.compile("|".join(MAX_MESSENGER_FLAGS), re.IGNORECASE)


def detect_max_messenger(title: str, description: str) -> tuple[int, str]:
    """v4 Идея 37: MAX как платформа для бота — малоконкурентная ниша, +1."""
    text = f"{title}\n{description}"
    if _MAX_MESSENGER_RE.search(text):
        return 1, "MAX мессенджер — малоконкурентная ниша: +1"
    return 0, ""


# === v4 Идея 33: pirate-агрегаторы Telegram-каналов ===

PIRATE_AGGREGATOR_MARKERS = (
    # 1. Копирование/пересылка с подменой/рерайт + Telegram
    (
        re.compile(
            r"(копирован\w+|пересылк\w+\s+с\s+подмен|рерайт\s+чужих\s+пост|перепост\s+без\s+атрибуц)"
            r"[\s\S]{0,80}?(telegram|телеграм)|"
            r"(telegram|телеграм)[\s\S]{0,80}?(копирован\w+|подмен\w+\s+авторств|рерайт)",
            re.IGNORECASE,
        ),
        "копирование/рерайт + Telegram",
    ),
    # 2. Несколько целевых каналов + независимые источники = сетка
    (
        re.compile(
            r"(несколько|сетк\w+|пул)\s+(целев\w*|свои\w*)\s+канал[\s\S]{0,120}?"
            r"(независим\w*|разн\w*|чужих)\s+источник",
            re.IGNORECASE,
        ),
        "сетка каналов + независимые источники",
    ),
    # 3. Скачивание фото и текста + постинг вместо forward — обход атрибуции
    (
        re.compile(
            r"(скачиван|загруз)\w+\s+(фото|изображен|текст\w*)[\s\S]{0,80}?"
            r"(постинг|публикац|в\s+канал)\s+(а|без)\s+(forward|форвард)|"
            r"(репост|перепост)\s+без\s+(автор|источник|атрибуц)",
            re.IGNORECASE,
        ),
        "постинг без forward (обход атрибуции)",
    ),
    # 4. Глобальные бан-листы пользователей
    (
        re.compile(
            r"глобальн\w+\s+бан[\s-]?лист\w*|общ\w+\s+бан[\s-]?лист\w+\s+(пользоват|каналов)",
            re.IGNORECASE,
        ),
        "глобальный бан-лист",
    ),
    # 5. Django/FastAPI admin + деплой — профессиональная инфраструктура для масштаба
    (
        re.compile(
            r"(django|fastapi)[\s-]?admin[\s\S]{0,80}?(деплой|production|прод|vps|сервер)|"
            r"админк\w+\s+(на\s+)?(django|fastapi)[\s\S]{0,80}?(деплой|production|vps)",
            re.IGNORECASE,
        ),
        "профессиональная админка для масштаба",
    ),
)


def detect_pirate_aggregator(title: str, description: str) -> tuple[int, str]:
    """v4 Идея 33: pirate-агрегатор Telegram-каналов.

    Не hard reject (user обсуждение 11 мая — снизить уровень моральных reject'ов).
    При 3+ из 5 маркеров — штраф -3.
    """
    text = f"{title}\n{description}"
    matched = [name for regex, name in PIRATE_AGGREGATOR_MARKERS if regex.search(text)]
    if len(matched) >= 3:
        return -3, f"pirate-агрегатор Telegram ({len(matched)} маркеров: {', '.join(matched[:3])})"
    return 0, ""


# === v4 hot-fix: "инверсия автора" — заказчик пишет от лица исполнителя ===
# Наблюдённые кейсы: "Разработаю Телеграм БОТ на Python любой сложности,
# для любых целей + поддержка. Сделаю быстро, качественно и не дорого!"
# На Kwork заказчики НЕ пишут "разработаю/сделаю". Если так — это либо
# спам-исполнитель в форме заказа (нарушение ToS), либо мусор.

INVERTED_AUTHOR_FLAGS = (
    r"\bразработ(аю|аем|ает)\b",
    r"\bсделаю\b|\bсделаем\s+(быстро|качественн|за\s+\d)",
    r"\bнапишу\b|\bнапишем\s+(быстро|качественн)",
    r"\bпредлагаю\s+(услуги|разработ|свои)",
    r"любой\s+сложност",
    r"быстро,?\s+качественн\w+",
    r"\+\s*поддержка\b",
    r"для\s+любых\s+целей",
    r"\bобращайтесь\b",
    r"стоимость\s+зависит\s+от",
    r"под\s+ключ\s+за\s+\d+\s*(день|дня|дней|сутки)",
    r"\bвыполню\b|\bвыполним\s+(быстро|качественн|в\s+срок)",
    r"опыт\s+работы\s+\d+\s*(лет|год)",
    r"\bпортфолио\b\s+(есть|готов|могу)",
    r"\bцен[аы]?\s+(договорн|демократичн|приятн|низк)",
)
_INVERTED_AUTHOR_RE = re.compile("|".join(INVERTED_AUTHOR_FLAGS), re.IGNORECASE)


def detect_inverted_author(title: str, description: str) -> Optional[str]:
    """Hard reject: описание написано от лица исполнителя ('разработаю',
    'сделаю', 'любой сложности', 'быстро качественно'), а не заказчика.

    Триггер: 2+ уникальных маркера.
    """
    text = f"{title}\n{description}"
    matches = {m.group(0).lower() for m in _INVERTED_AUTHOR_RE.finditer(text)}
    if len(matches) >= 2:
        sample = ", ".join(sorted(matches)[:3])
        return (
            f"инверсия автора (описание от исполнителя, не заказчика): {sample}"
        )
    return None


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


# Аудит 10.07: ложное срабатывание на отрицании — «НЕ нужен шаблонный лендинг
# на Tilda» рубился по ключу 'Tilda'. Если прямо перед совпадением стоит
# отрицание/отказ — это совпадение пропускаем и ищем следующее.
_NEGATION_BEFORE_RE = re.compile(
    r"(?:не\s+нужен\w*|не\s+надо|не\s+хочу|не\s+рассматрива\w+|без|никак\w+|"
    r"вместо|не\s+на|уйти\s+от|отказ\w*\s+от|не\s+использу\w+)"
    r"\s*(?:шаблонн\w+\s+)?(?:лендинг\w*\s+|сайт\w*\s+|конструктор\w*\s+)?"
    r"(?:на\s+|в\s+)?$",
    re.IGNORECASE,
)


def _hard_reject_reason(title: str, description: str) -> Optional[str]:
    text = f"{title}\n{description}"
    for match in _HARD_REJECT_RE.finditer(text):
        prefix = text[max(0, match.start() - 45):match.start()]
        if _NEGATION_BEFORE_RE.search(prefix):
            continue  # отрицание («не нужен ... на Tilda») — не повод для reject
        return match.group(0)
    return None


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


# === Волна 30.06 C1: связка CRM-платформа + no-code обвязка → hard reject ===
_CRM_PLATFORM_RE = re.compile(
    r"\bamocrm\b|\bамосрм\b|\bamo\s*crm\b|\bамо\b|"
    r"\bbitrix\s*24\b|\bбитрикс\s*24\b",
    re.IGNORECASE,
)
_NOCODE_BUNDLE_RE = re.compile(
    r"\bsalebot\b|\bсейлбот\b|\bwazzup\b|\bваззап\b|\bwappi\b|"
    r"\bmanychat\b|\bменичат\b|\btextback\b|\bsendpulse\s+бот|"
    r"\bpipedrive\b|\bkommo\b",
    re.IGNORECASE,
)


def detect_crm_nocode_bundle(title: str, description: str) -> Optional[str]:
    """Волна 30.06 C1: amoCRM/Bitrix24 + no-code (SaleBot/Wazzup/Wappi/ManyChat)
    как основа → hard reject, даже если в стеке упомянут Claude/OpenAI API.

    Ядро такой работы — кастомизация CRM и связка no-code платформ, не разработка.
    Маркер "Claude API" не превращает это в разработческую задачу.
    """
    text = f"{title}\n{description}"
    if _CRM_PLATFORM_RE.search(text) and _NOCODE_BUNDLE_RE.search(text):
        crm = _CRM_PLATFORM_RE.search(text).group(0)
        nocode = _NOCODE_BUNDLE_RE.search(text).group(0)
        return (
            f"CRM-обвязка ({crm} + {nocode}) — no-code кастомизация, не разработка "
            f"(Claude API в стеке не спасает)"
        )
    return None


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
    r"\bwebsocket\b|\bsocket\.?io\b|\brealtime\b",
    r"server\s+components|\brsc\b",
    r"\bsuspense\b", r"app\s+router", r"\bhydration\b",
    r"web\s+vitals", r"schema\.org", r"\bhreflang\b",
    # Telegram (волна 3 идея 48: + Telethon/Pyrogram)
    r"\baiogram\b", r"telegram\s+mini\s+app|tg[\s-]?mini[\s-]?app|tma\b",
    r"\bmtproto\b", r"\buserbot\b", r"\btelethon\b", r"\bpyrogram\b",
    # CMS/специализированное
    r"\bstrapi\b", r"\bsanity\b", r"headless\s+cms", r"\bdirectus\b", r"\bpayload\s+cms\b",
    # Платежи и интеграции
    r"юkassa\s+marketplace|recurrent\s+(payment|billing)",
    r"\bsplitt?ing\b\s+(платеж|payments?)", r"\bescrow\b", r"webhook\s+sign",
    # Волна 3 идея 48: дополнения
    r"multi[\s-]?workspace", r"gtm[\s-]?логик", r"lead[\s-]?scoring",
    r"hidden\s+mmr", r"\bmatchmaking\b",
    r"\bdkim\b", r"\bspf\b", r"\bdmarc\b",
    r"yandex\s+speechkit|speechkit",
    r"whisper\s+streaming",
    r"\bclickhouse\b", r"\bcassandra\b", r"redis\s+streams",
    r"graphql\s+federation",
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
    # Волна 3 идея 48: типовые «массовые» тематики
    r"калькулятор\s+калори",
    r"бот\s+для\s+записи",
)
_MASS_RE = re.compile("|".join(MASS_TERMS), re.IGNORECASE)


def detect_terminology_specificity(title: str, description: str) -> tuple[int, str]:
    """Волна 3 идея 48: предиктор конкуренции по терминологической специфичности.

    Главный фактор скоринга. Основан на эмпирически подтверждённой логике
    сортировки Kwork (новые отклики сверху → массовые заказы непробиваемы
    из-за потока поздних откликов, узкие — реальная цель).

    Returns:
        (modifier, reason)
          HIGH competition (mass ≥2 и pro ==0, ИЛИ mass ≥3) → -5
          LOW  competition (pro ≥2, ИЛИ pro ≥1 и mass ≤1) → +2
          MEDIUM → 0
    """
    text = f"{title}\n{description}"
    qual_matches = {m.group(0).lower() for m in _QUALIFYING_RE.finditer(text)}
    mass_matches = {m.group(0).lower() for m in _MASS_RE.finditer(text)}
    qual_count = len(qual_matches)
    mass_count = len(mass_matches)

    # HIGH — массовая терминология без квалифицирующей: предсказано 50+ откликов
    if (mass_count >= 2 and qual_count == 0) or mass_count >= 3:
        sample = ", ".join(sorted(mass_matches)[:3])
        return -5, (
            f"HIGH competition (mass={mass_count}/pro={qual_count}: {sample}): -5"
        )

    # LOW — профессиональная терминология: предсказано 5-15 откликов.
    # Волна 4 п.3.1: если квалифицирующий термин виральный (OpenClaw/Hermes/
    # Cursor/Devin и т.п.) — конкуренция всё равно высокая, бонус режется до +1.
    if qual_count >= 2 or (qual_count >= 1 and mass_count <= 1):
        sample = ", ".join(sorted(qual_matches)[:5])
        viral = _VIRAL_TERMS_RE.search(text)
        if viral:
            return 1, (
                f"квалиф ({sample}) НО виральный термин '{viral.group(0)}': +1 "
                f"(а не +2 — туда сбегаются все кто видел ютуб)"
            )
        return 2, (
            f"LOW competition (pro={qual_count}/mass={mass_count}: {sample}): +2"
        )

    return 0, ""


# Волна 4 п.3.1: список виральных терминов — узкие но не редкие.
VIRAL_TERMS = (
    r"openclaw",
    r"\bhermes\s+agent\b|hermes[\s\-]?ai",
    r"\bcodex\b\s+(openai|api)?",
    r"\bcursor\b\s+(ai|composer|agent|ide)?",
    r"\bdevin\b\s+ai",
    r"\bcline\b",
    r"\baider\b",
    r"\bcomfyui\b",
    r"\bautogen\b",
    r"\bcrewai\b",
)
_VIRAL_TERMS_RE = re.compile("|".join(VIRAL_TERMS), re.IGNORECASE)


# === Волна 3 идея 45: класс D (доработка чужого кода) без доступа к коду ===

# Маркеры задачи класса D
_CLASS_D_TASK_RE = re.compile(
    r"\bпочинить\b|\bдоработ\w+\b|\bисправ\w+\b|\bоптимизирова\w+\b|"
    r"перестал\s+работат|\bсломал\w+\b|\bбараб\w+\b|\bбарахл\w+\b|"
    r"есть\s+готов\w+\s+код,?\s+нужно|"
    r"у\s+меня\s+(скрипт|бот|парсер|сайт)\b[\s\S]{0,30}?(надо|нужно)|"
    r"допилить\s+(скрипт|бот|сайт|парсер|проект)|"
    r"переписать\s+(скрипт|бот|парсер)",
    re.IGNORECASE,
)
# Маркеры доступности кода/репозитория
_CODE_ACCESS_RE = re.compile(
    r"github\.com/|gitlab\.com/|bitbucket\.org/|"
    r"\bпришл\w+\s+(код|репозитори|проект|архив)|"
    r"\bоткро(ю|ем)\s+(доступ|репозитори|код)|"
    r"\bдам\s+(доступ|ссылк\w+\s+на\s+код)|"
    r"исходник\w+\s+(прилож|приклад|готов)|"
    r"описание\s+(структуры\s+)?кода",
    re.IGNORECASE,
)


def detect_class_d_blind_fix(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """Волна 3 идея 45: класс D «починить чужое» без видимого кода — асимметричный риск.

    Может оказаться полчаса работы, может — неисправимая ситуация без исходников.
    При низком бюджете риск максимален (день за тысячу), при высоком — есть
    запас на разбирательство.

    Returns:
        (-3, reason) если бюджет < 10k
        (-2, reason) если бюджет 10-30k
        (-1, reason) если бюджет ≥ 30k
        (0, "") если код доступен или это не класс D
    """
    text = f"{title}\n{description}"
    if not _CLASS_D_TASK_RE.search(text):
        return 0, ""
    if _CODE_ACCESS_RE.search(text):
        return 0, ""

    if budget_limit and budget_limit < 10_000:
        return -3, f"класс D без доступа к коду, бюджет <10к ({budget_limit:,}): -3"
    if budget_limit and budget_limit < 30_000:
        return -2, f"класс D без доступа к коду, бюджет <30к ({budget_limit:,}): -2"
    return -1, f"класс D без доступа к коду (бюджет {budget_limit:,}): -1"


# === Волна 3 идея 46: микробюджет + многокомпонентная задача ===

# Маркеры «компонентов» — расширенный список (включает _FUNC_BLOCK_KEYWORDS + еще)
_MICROBUDGET_COMPONENT_RE = re.compile(
    r"\bпарсер\b|\bпарсинг\b|"
    r"\bадминк[аеу]\b|\bадмин[\s-]*панел|"
    r"\bинтеграци\w+|"
    r"\bкаталог\b|\bвитрин\w+|"
    r"\bлич\w+\s+кабинет|"
    r"\bоплат\w+|\bэквайринг\b|подписк\w+\s+(оплат|тариф)|"
    r"\bкалькулятор\b|"
    r"\bдиалог\w+\s+(сценари|с\s+пользоват)|"
    r"\bтелеграм[\s-]?бот|\bтг[\s-]?бот\b|\bwhatsapp\b|\bвотсап\b|\bmax\b\s+мессенджер|"
    r"\bcrm\b\s+(интеграц|подключ)|"
    r"\bgoogle\s+sheets\b|\bgoogle\s+таблиц|"
    r"\bgoogle\s+sheet|\bsheet\s+api|"
    r"\bпрокси\b\s+(использ|подключ|поддерж)|"
    r"\bчекпоинт\w+|\bретрай\w+|"
    r"frontend\b|бэкенд\b|\bbackend\b",
    re.IGNORECASE,
)


def detect_microbudget_multicomponent(
    title: str,
    description: str,
    budget_limit: int,
    farm_mode_active: bool = False,
) -> tuple[int, str]:
    """Волна 3 идея 46: микробюджет + многокомпонентная задача = работа в минус.

    Заказы с 500-5000 ₽ и технически чистой постановкой получают высокий
    скор от Haiku, но при многокомпонентности — почти всегда минус.

    Шкала:
        budget ≤ 1500 + 2+ компонента → -3
        budget ≤ 3000 + 3+ компонента → -3
        budget ≤ 5000 + 4+ компонента → -2

    Исключение для отзыв-фарма: если задача малокомпонентная (≤2),
    штраф не применяется — отзыв стоит больше денег.
    """
    if not budget_limit or budget_limit > 5000:
        return 0, ""

    text = f"{title}\n{description}"
    matches = {m.group(0).lower() for m in _MICROBUDGET_COMPONENT_RE.finditer(text)}
    components = len(matches)

    if budget_limit <= 1500 and components >= 2:
        # Отзыв-фарм исключение: при малом числе компонентов считаем как fast task
        if farm_mode_active and components <= 2:
            return 0, ""
        sample = ", ".join(sorted(matches)[:3])
        return -3, (
            f"микробюджет {budget_limit:,} + {components} компонента "
            f"({sample}): -3"
        )

    if budget_limit <= 3000 and components >= 3:
        if farm_mode_active and components <= 2:
            return 0, ""
        sample = ", ".join(sorted(matches)[:3])
        return -3, (
            f"микробюджет {budget_limit:,} + {components} компонент "
            f"({sample}): -3"
        )

    if budget_limit <= 5000 and components >= 4:
        if farm_mode_active and components <= 2:
            return 0, ""
        sample = ", ".join(sorted(matches)[:3])
        return -2, (
            f"микробюджет {budget_limit:,} + {components} компонент "
            f"({sample}): -2"
        )

    return 0, ""


# === v4 6.1: модификатор скора по конкурентной среде ===

def compute_competition_modifier(
    responses_count: int,
    hired_percent: Optional[int],
    buyer_achievements: int,
    user_projects_count: int = 0,
) -> tuple[int, list[str]]:
    """Модификатор скора по конкурентной среде + уровню покупателя.

    Волна 3 идея 48 (банды откликов сильнее):
        0-10   → +1 (низкая конкуренция, окно прочтения)
        11-30  → 0
        31-50  → -2 (быстро наполняется)
        51+    → -4 (коннект потерян)

    Волна 3 идея 44 (hire_rate-штраф градуируется):
        с медалькой                                      → 0 (риск оправдан)
        проектов < 5 (мало данных)                       → 0
        hire_rate = 0% + ≥5 проектов                     → -4 (подтверждённый собиратель КП)
        hire_rate < 30% + ≥10 проектов                   → -3
        hire_rate < 50%                                  → -1

    Шкала покупателя (бонусы за медальки сохраняются):
        1 ачивка                    → +1
        2+ ачивок                   → +2 (топ-покупатель)
    """
    modifier = 0
    reasons: list[str] = []

    # Отклики. Волна 30.06 B2-guard: НЕ давать +1 за низкий абсолют — в момент
    # показа откликов почти всегда 0-1, это НЕ "низкая конкуренция", а отсутствие
    # данных о темпе (может быть начало мясорубки). Бонус за реально медленный
    # темп начисляется отдельно на автозамере (velocity), не по первому абсолюту.
    if responses_count <= 30:
        pass
    elif responses_count <= 50:
        modifier -= 2
        reasons.append(f"наполнение ({responses_count} откликов): -2")
    else:
        modifier -= 4
        reasons.append(f"переполнено ({responses_count} откликов): -4")

    # Уровень покупателя — медальки сразу дают бонус (риск hire_rate оправдан)
    if buyer_achievements == 1:
        modifier += 1
        reasons.append("медалька покупателя: +1")
    elif buyer_achievements >= 2:
        modifier += 2
        reasons.append(f"{buyer_achievements} медальки покупателя: +2 (топ)")
    else:
        # Волна 3 идея 44: градуированный штраф за hire_rate когда нет медалек
        # Применяется только если есть достаточно данных (≥5 проектов).
        if user_projects_count >= 5 and hired_percent is not None:
            if hired_percent == 0:
                modifier -= 4
                reasons.append(
                    f"hire_rate 0% + {user_projects_count} проектов без медалек: "
                    f"-4 (подтверждённый собиратель КП)"
                )
            elif hired_percent < 30 and user_projects_count >= 10:
                modifier -= 3
                reasons.append(
                    f"hire_rate {hired_percent}% + {user_projects_count} проектов: -3"
                )
            elif hired_percent < 50:
                modifier -= 1
                reasons.append(f"hire_rate {hired_percent}% без медалек: -1")

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
# Волна 30.06 A4: маркеры локальной автоматизации / готового SDK — НЕ построение
# инфраструктуры. Подавляют штраф (управление готовым софтом, не медиасервер).
_STREAMING_LOCAL_RE = re.compile(
    r"\bobs\b|obs[\s\-]?websocket|obs[\s\-]?studio|"
    r"\bлокальн\w*|\bдесктоп\w*|windows[\s\-]?приложен|"
    r"управлени\w+\s+(obs|софт|програм|приложен)|запуск\s+(obs|софт|програм)|"
    r"\bdaily\.co\b|\bagora\b|\bvonage\b|\btwilio\s+video\b|"
    r"готов\w+\s+(видео[\s\-]?)?sdk|интеграц\w+\s+(готов\w+\s+)?видео[\s\-]?sdk",
    re.IGNORECASE,
)


def detect_streaming_low_budget(
    title: str, description: str, budget_limit: int
) -> tuple[int, str]:
    """v3 3.4 + волна 30.06 A4: штраф ТОЛЬКО за построение медиа-инфраструктуры.

    Подавляется если рядом маркеры локальной автоматизации (OBS/десктоп/управление
    готовым софтом) или интеграции готового видео-SDK (Daily.co/Agora) — это не
    построение стриминг-инфраструктуры, а утилита/интеграция.
    """
    if not budget_limit or budget_limit >= 150_000:
        return 0, ""
    text = f"{title}\n{description}"
    match = _STREAMING_RE.search(text)
    if not match:
        return 0, ""
    if _STREAMING_LOCAL_RE.search(text):
        return 0, ""  # OBS/локальное/готовый SDK — не инфраструктура
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

   v5 Идея 41: учитывай Claude Code speedup. Все стоимости в таблице
   выше — для одиночной работы. Разработчик использует Claude Code
   и закрывает задачи в 3-5 раз быстрее (SPEEDUP_FACTOR = 4).
   Поэтому: эффективный expected_cost = expected_cost_solo / 4.

   Если effective_cost / budget_max > 1.5 → штраф -2, в reason укажи
   "budget_scope_v2 (с Claude Code): ~Xк / бюджет Yк".
   Не дублировать с штрафом python-фильтра detect_budget_scope_mismatch
   (его сигнатура другая; считай только если он не сработал).

   ВАЖНО: критерий "технология не в стеке" больше НЕ блокер по умолчанию.
   Claude Code знает большинство стеков. Скип только если технология
   из списка где Claude Code объективно слабее:
   - Embedded и Arduino (нужна работа с железом)
   - Специализированный ML с обучением кастомных моделей
   - 3D-визуализация и игровые движки глубокого уровня
   - Криптотрейдинг боты для бирж

ТИП ЗАКАЗА (для воронки short / long):
- short: верхняя граница бюджета ≤50к, 1-2 функциональных блока,
  junior-friendly, срок исполнения 1 день - 1 неделя. Цель — быстрый отзыв.
- long: верхняя граница ≥100к, 3+ блока, сложная архитектура, срок ≥2 недель.
  Цель — основной доход.
- medium: между ними. Универсальный заказ.

БАЗОВЫЙ СКОР:

Стек (до 4):
+2 прямое совпадение (Next.js/NestJS/TS/PostgreSQL) ИЛИ Python/aiogram бот
+1 серая зона (Vue→React, мобилка)
+1 сильная сторона (AI на своём коде, real-time, админка, дашборд)

ВАЖНО про парсинг (волна 5): парсинг САМ ПО СЕБЕ НЕ плюс и НЕ "класс A".
- Парсинг чужих маркетплейсов/витрин/контактов/поисковой выдачи = серая зона,
  скор НЕ выше 3 (а явные кейсы уже отсечены python-фильтром до Haiku).
- Парсинг собственных данных заказчика или через официальный API под его
  аккаунтом = нейтрально, оценивается по стеку/бюджету как обычная задача.

Бюджет (до 3) — ТОЛЬКО по реальной (нижней) границе, верх Kwork игнорируется:
+3 ≥100к, +2 70-100к, +1 50-70к, 0 <50к

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

=== КРИТЕРИЙ ОТБОРА (волна 5 — смысловое ядро) ===
У разработчика профиль novice: 1 отзыв, без медали уровня, 30 коннектов/мес.
Каждый коннект ценен. Откликаться стоит ТОЛЬКО когда есть РЕАЛЬНЫЙ шанс что нас
прочитают и выберут — а не "технически потяну".

ГЛАВНОЕ: на ШИРОКИХ заказах (массовая тема — "сайт", "бот", "лендинг",
"ИИ-агент для бизнеса", "парсинг") мы СТРУКТУРНО проигрываем. Туда набегают
десятки откликов, включая ветеранов с весом (сотни отзывов, медали, топ-рейтинг).
Заказчик при прочих равных выберет проверенного. Наш отклик утонет, коннект
потрачен в никуда. Поэтому понижать скор широких заказов ДАЖЕ если задача
чистая по стеку и технически нам подходит.

ЦЕЛИМСЯ в УЗКИЕ ниши, где ветеранов нет и большинство фрилансеров пасуют:
специфические профессиональные технологии/задачи (Traccar, M365/Graph API, VBA,
Tarantool, ClickHouse, CatBoost/прикладной ML, ТНВЭД/таможня, узкие интеграции).
Там даже новичка прочитают — конкуренции с весом нет. Это наш реальный шанс.

Различай "широкое мясо" и "узкую нишу" НЕ по сложности задачи, а по тому,
СКОЛЬКО исполнителей с весом туда побегут. Чистый по стеку, но широкий заказ
(массовый телеграм-бот, типовой лендинг) → понижать. Непонятный массам, узкий
заказ → поднимать.

КРИТЕРИЙ-1 (жёсткий скип): если заказчик ставит КЛЮЧЕВЫМ требованием опыт
которого у разработчика объективно нет (high-load на миллионы пользователей,
обучение CV-моделей детекции, real-time телефония, fundamental research) —
скип ДАЖЕ при родном стеке. Прикладные задачи на любом стеке (включая
незнакомый — Rust, Go, VBA, Traccar) закрываются через Claude Code и НЕ
штрафуются за "стек вне профиля".

БОНУСЫ за сильные зоны (поднимать приоритет):
+2 RAG / AI-агенты (RAG, vector search, embeddings, поиск по базе знаний,
   AI-ассистент по документам, function calling, structured output)
+1/+2 прикладной ML (обучение модели под задачу, YOLO, детекция/классификация,
   CatBoost, OCR, прогнозирование, fine-tuning/LORA)
   Оговорка: бонус ТОЛЬКО за прикладные ML/RAG, НЕ за фундаментальный research.

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
  простые Telegram-боты (уведомления, FAQ, калькулятор, приём заявок);
  скрипты автоматизации (обработка файлов, конвертации);
  утилиты для Excel/Google Sheets, Apps Script;
  нишевые скриптовые среды (аудит 10.07): ExtendScript (Photoshop/Illustrator/
  After Effects), Blender Python, OBS-скрипты, AutoHotkey — узких спецов мало,
  конкуренция низкая, с ИИ выполняется уверенно. Такие задачи ПРИОРИТЕТНЫ.
  ВАЖНО (волна 5): парсинг — НЕ автоматически "класс A". Парсинг чужих
  маркетплейсов/витрин/контактов/поисковой выдачи = серая зона (reject,
  обычно отсечён до Haiku). Бонус +1 только за парсинг СОБСТВЕННЫХ данных
  заказчика или официальный API под его аккаунтом.
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

ВАЖНО про бюджет (аудит 10.07, ИИ-ускорение): исполнитель работает с Claude Code —
узкая объективная задача (скрипт, утилита, интеграция, фикс) с замкнутым scope
делается за 1-2 часа. Для ТАКИХ задач бюджет 5-15к = нормальная почасовка:
НЕ снижать скор за "бюджет занижен". Штраф за заниженный бюджет применять только
когда объём явно многодневный (fullstack, несколько экранов, R&D, обучение моделей).

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

=== ПРАВИЛА ВОЛНЫ 5 (приоритетнее формата ниже) ===

13. ЖИВОЙ СТАРТ. НЕ начинать с перечисления стека списком и НЕ со "стек мой".
    Начинать живо и по-человечески: "Здравствуйте. Заказ интересный, готов
    взяться" и сразу к сути. Технологии вплетать в рассуждение об архитектуре,
    НЕ списком. Живые связки ("если что", "как раз"), без канцелярита.
    (Это смягчает п.1: "Вы" и "Здравствуйте" сохраняем, но язык живой, не сухой.)

14. КОРОТКО. 3-5 строк. НЕ пересказывать ТЗ обратно заказчику. Отвечать
    ТОЛЬКО на то что заказчик спросил. Просил цену и срок — дать понять что
    готов назвать (цифры в полях формы), не расписывать "как буду делать".

15. ДЕМПИНГ РАДИ ОТЗЫВА (если is_fast=да). В тексте ЧЕСТНО объяснить низкую
    цену одной фразой: "цену ставлю ниже обычного - набираю отзывы на старте,
    на качестве это не отражается". БЕЗ конкретной цифры в тексте (цифра в
    поле формы). Это снимает сигнал "дёшево = плохо". На BIG-проектах (is_fast=нет)
    демпинг НЕ применять, про отзывы не писать.

16. ДЕБАГ-ЗАДАЧИ (если is_debug=да). Найти причину = решить проблему. НЕ
    выдавать конкретный диагноз и рецепт починки в отклике ДО заказа — иначе
    заказчик починит сам и не заплатит. Показать что знаешь ОБЛАСТЬ и МЕТОД
    диагностики ("вижу несколько вероятных направлений, диагностирую через
    [инструмент/подход]"), но конкретную причину придержать до заказа. На
    задачах разработки (не дебаг) этого ограничения нет.

17. НЕ просить доступы / ТЗ / репозиторий пока заказчик не выбрал. Не забегать
    вперёд. Не повторять то что уже могло быть в диалоге.

18. ДОРАБОТКА ЧУЖОГО КОДА (класс D, "починить/доработать" без видимого кода).
    Если это доработка существующего проекта и репозитория/доступа ещё нет —
    в отклике ОДНОЙ фразой обозначить границу: "оценка финальная после того как
    увижу репозиторий/код". Это защита от расползания скоупа. Без давления,
    спокойно, как норма процесса.

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
FAST-заказ (демпинг уместен): {is_fast}
Дебаг-задача (придержать рецепт): {is_debug}

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

При совпадении 2+ из 4 признаков добавь +2 к стартовому скору и явно отметь это в reason.

ВАЖНО (июль 2026): бонус +2 НЕ начисляй, если предмет заказа сомнителен по сути —
обход капчи/антибота, уникализатор контента, обход площадочных фильтров, no-code как
ядро (n8n/Make/Zapier), серая зона. Измеримость результата НЕ оправдывает запрещённый
предмет: сначала проверь предмет, и только для чистых задач давай бонус."""


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
    user_projects_count: int = 0,
    profile_reviews_count: int = 0,
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

    # v5 откат идеи 32: парсинг коммерческих источников — НЕ hard reject.
    # Оценивается по экономике (budget-scope) с учётом Claude Code speedup.
    # detect_commercial_parsing_v2() остаётся в коде, но не вызывается.

    # v4 hot-fix: инверсия автора (описание написано от лица исполнителя)
    inverted = detect_inverted_author(title, description)
    if inverted:
        logger.info("InvertedAuthorHardReject [%s]: %s", title[:60], inverted)
        return {
            "score": 0, "is_ai": is_ai, "reason": inverted,
            "hard_reject": True, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    # v5 смягчение идеи 31: AI-маркетинг — НЕ hard reject. Оценивается
    # по обычным критериям (бюджет, ясность ТЗ, медальки покупателя).
    # AI_ENGINEERING остаётся как +1 (см. ниже, после Haiku).
    ai_class, ai_markers = classify_ai_task(title, description)

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

    # === Волна 30.06 A2: ML-инженерия / self-host инференс / диффузия ===
    # Core requirement (обучение LLM, vLLM, FLUX/SD) → дисквалификатор. Вскользь
    # ("или можно дообучить") → -2 после Haiku (применяется ниже, ml_eng_penalty).
    ml_eng_action, ml_eng_reason = detect_ml_engineering(title, description)
    if ml_eng_action == "hard_reject":
        logger.info("MLEngineeringHardReject [%s]: %s", title[:60], ml_eng_reason)
        return {
            "score": 0, "is_ai": is_ai, "reason": ml_eng_reason,
            "hard_reject": True, "scope_unclear": False, "no_code_required": None,
            "site_category": "not_site",
            "hire_rate_penalty": False, "hired_percent": hired_percent,
            "breakdown": {},
        }

    # === Волна 4-5 Группа 1: HARD REJECT (серая зона / ToS / юр.риски / мусор) ===
    for fn, label in (
        (detect_marketplace_work, "MarketplaceHardReject"),
        (detect_mobile_app_parsing, "MobileAppParsingHardReject"),
        (detect_browser_imitation_wrapper, "BrowserImitationHardReject"),
        (detect_game_botting, "GameBottingHardReject"),
        (detect_antispam_bypass, "AntispamBypassHardReject"),
        (detect_contact_harvest, "ContactHarvestHardReject"),       # волна 5 (2.1)
        (detect_serp_parsing, "SerpParsingHardReject"),             # волна 5 (1.4)
        (detect_template_placeholders, "TemplatePlaceholderSkip"),  # волна 5 (1.5)
        (detect_pixel_perfect, "PixelPerfectSkip"),                 # волна 5 (1.5)
        (detect_site_donor, "SiteDonorHardReject"),                 # волна 30.06 A3
        (detect_crm_nocode_bundle, "CrmNocodeBundleHardReject"),    # волна 30.06 C1
    ):
        hr_reason = fn(title, description)
        if hr_reason:
            logger.info("%s [%s]: %s", label, title[:60], hr_reason)
            return {
                "score": 0, "is_ai": is_ai, "reason": hr_reason,
                "hard_reject": True, "scope_unclear": False, "no_code_required": None,
                "site_category": "not_site",
                "hire_rate_penalty": False, "hired_percent": hired_percent,
                "breakdown": {},
            }

    # v5 Идея 40: криминал-категории (УК/КоАП РФ) — hard reject
    criminal = detect_criminal_categories(title, description)
    if criminal:
        logger.info("CriminalHardReject [%s]: %s", title[:60], criminal)
        return {
            "score": 0, "is_ai": is_ai, "reason": criminal,
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

    # Правка июль 2026 п.3: ценз заказчика по отзывам — если требуемое N больше
    # числа отзывов профиля, отклик отсеется формально → hard-reject, коннект бережём.
    census_reason = detect_reviews_census(title, description, profile_reviews_count)
    if census_reason:
        logger.info("ReviewsCensusHardReject [%s]: %s", title[:60], census_reason)
        return {
            "score": 0, "is_ai": is_ai, "reason": census_reason,
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

        # === v4 6.1 + волна 3 идея 44: модификатор по конкурентной среде ===
        comp_mod, comp_reasons = compute_competition_modifier(
            responses_count, hired_percent, buyer_achievements,
            user_projects_count=user_projects_count,
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

        # === Волна 3 идея 48: предиктор конкуренции по терминологии ===
        term_mod, term_reason = detect_terminology_specificity(title, description)
        # Исключение: HIGH (-5) при бюджете 100k+, медальке и hire_rate>50% —
        # большой бюджет компенсирует низкую вероятность прочтения, смягчаем до -2.
        if term_mod == -5:
            high_budget = budget_limit_eff and budget_limit_eff >= 100_000
            strong_buyer = buyer_achievements >= 1
            high_hire = hired_percent is not None and hired_percent > 50
            if high_budget and strong_buyer and high_hire:
                old_term_mod = term_mod
                term_mod = -2
                term_reason = (
                    f"{term_reason} → смягчено до -2 "
                    f"(бюджет {budget_limit_eff:,}+, медальки {buyer_achievements}, "
                    f"hire_rate {hired_percent}%)"
                )
                logger.info(
                    "TerminologyHighException [%s]: %d→%d по высокому бюджету+медалькам",
                    title[:60], old_term_mod, term_mod,
                )
        if term_mod != 0:
            old_score = score
            score = max(0, min(10, score + term_mod))
            reason = f"{reason}; {term_reason}" if reason else term_reason
            logger.info(
                "Terminology [%s]: %d→%d — %s",
                title[:60], old_score, score, term_reason,
            )

        # Волна 5 (2.2): пометка узкий / широкий для карточки.
        # term_mod > 0 → узкая ниша (pro-термины), < 0 → широкое мясо (mass).
        if term_mod > 0:
            competition_tier = "narrow"
        elif term_mod < 0:
            competition_tier = "wide"
        else:
            competition_tier = "medium"

        # === Волна 4 п.4.1: AI-инженерия +2 (RAG/embeddings/function calling) ===
        # AI_MARKETING_HARD/SOFT больше не штрафуем — оценка по экономике.
        if ai_class == "AI_ENGINEERING":
            old_score = score
            score = min(10, score + 2)
            ai_note = f"AI-инженерия ({ai_markers[0]}): +2 (сильная зона)"
            reason = f"{reason}; {ai_note}" if reason else ai_note
            logger.info(
                "AIEngineering [%s]: %d→%d — %s",
                title[:60], old_score, score, ai_note,
            )
        elif ai_class in ("AI_MARKETING_HARD", "AI_MARKETING_SOFT"):
            # Только лог — без изменения скора
            logger.info(
                "AIMarketingSignal [%s]: %s (без штрафа по v5)",
                title[:60], ai_class,
            )

        # === Волна 4 п.4.1: прикладной ML/CV/OCR +2 ===
        ml_mod, ml_reason = detect_applied_ml_bonus(title, description)
        if ml_mod:
            old_score = score
            score = min(10, score + ml_mod)
            reason = f"{reason}; {ml_reason}" if reason else ml_reason
            logger.info(
                "AppliedML [%s]: %d→%d — %s",
                title[:60], old_score, score, ml_reason,
            )

        # === Волна 30.06 A2: ML-инженерия вскользь ("или дообучить") -2 ===
        # (core requirement уже отбит как hard reject до Haiku).
        if ml_eng_action == "penalty":
            old_score = score
            score = max(0, score - 2)
            reason = f"{reason}; {ml_eng_reason}" if reason else ml_eng_reason
            logger.info(
                "MLEngineeringPenalty [%s]: %d→%d — %s",
                title[:60], old_score, score, ml_eng_reason,
            )

        # === Волна 4 п.2.1: фильтр заказчика против новичков -5 ===
        nb_mod, nb_reason = detect_newbie_filter(title, description)
        if nb_mod:
            old_score = score
            score = max(0, score + nb_mod)
            reason = f"{reason}; {nb_reason}" if reason else nb_reason
            logger.info(
                "NewbieFilter [%s]: %d→%d — %s",
                title[:60], old_score, score, nb_reason,
            )

        # === Правка июль 2026 п.2: вайбкодинг + заниженный бюджет -4 ===
        vibe_mod, vibe_reason = detect_vibecoding_devaluation(
            title, description, budget_limit_eff,
        )
        if vibe_mod:
            old_score = score
            score = max(0, score + vibe_mod)
            reason = f"{reason}; {vibe_reason}" if reason else vibe_reason
            logger.info(
                "VibecodingDevaluation [%s]: %d→%d — %s",
                title[:60], old_score, score, vibe_reason,
            )

        # === Волна 4 п.2.2: перенос инфры на исполнителя при микробюджете -6 ===
        infra_mod, infra_reason = detect_infra_transfer_microbudget(
            title, description, budget_limit_eff,
        )
        if infra_mod:
            old_score = score
            score = max(0, score + infra_mod)
            reason = f"{reason}; {infra_reason}" if reason else infra_reason
            logger.info(
                "InfraTransferMicrobudget [%s]: %d→%d — %s",
                title[:60], old_score, score, infra_reason,
            )

        # v5 откат идеи 33: pirate-агрегаторы НЕ штрафуем. Только лог-сигнал
        # чтобы видеть факт. Оценка идёт по обычным критериям.
        _pirate_mod, _pirate_reason = detect_pirate_aggregator(title, description)
        if _pirate_mod:
            logger.info(
                "PirateAggregatorSignal [%s]: %s (без штрафа)",
                title[:60], _pirate_reason,
            )

        # === v4 Идея 35 + волна 3 идея 42: несоответствие профиля fullstack ===
        prof_mod, prof_reason = detect_profile_mismatch(title, description)
        if prof_mod:
            old_score = score
            score = max(0, score + prof_mod)
            reason = f"{reason}; {prof_reason}" if reason else prof_reason
            logger.info(
                "ProfileMismatch [%s]: %d→%d — %s",
                title[:60], old_score, score, prof_reason,
            )

        # === Волна 3 идея 43: B2C-услуговые сайты как прокси для CMS ===
        b2c_mod, b2c_reason = detect_b2c_service_site(title, description)
        if b2c_mod:
            old_score = score
            score = max(0, score + b2c_mod)
            reason = f"{reason}; {b2c_reason}" if reason else b2c_reason
            logger.info(
                "B2CServiceSite [%s]: %d→%d — %s",
                title[:60], old_score, score, b2c_reason,
            )

        # === Волна 3 идея 45: класс D (доработка) без доступа к коду ===
        class_d_mod, class_d_reason = detect_class_d_blind_fix(
            title, description, budget_limit_eff,
        )
        if class_d_mod:
            old_score = score
            score = max(0, score + class_d_mod)
            reason = f"{reason}; {class_d_reason}" if reason else class_d_reason
            logger.info(
                "ClassDBlind [%s]: %d→%d — %s",
                title[:60], old_score, score, class_d_reason,
            )

        # === Волна 3 идея 46: микробюджет + многокомпонентная задача ===
        mb_mod, mb_reason = detect_microbudget_multicomponent(
            title, description, budget_limit_eff, farm_mode_active=farm_mode_active,
        )
        if mb_mod:
            old_score = score
            score = max(0, score + mb_mod)
            reason = f"{reason}; {mb_reason}" if reason else mb_reason
            logger.info(
                "MicrobudgetMulticomp [%s]: %d→%d — %s",
                title[:60], old_score, score, mb_reason,
            )

        # v5 откат идеи 36: копирование по референсу — НЕ штраф.
        # Если бюджет адекватный — нормальная работа. Экономика проверяется
        # через budget-scope (см. Идею 41).

        # === v5 Идея 39: no-code под видом разработки ===
        nocode_mod, nocode_reason = detect_nocode_under_dev(
            title, description, budget_limit_eff,
        )
        if nocode_mod:
            old_score = score
            score = max(0, score + nocode_mod)
            reason = f"{reason}; {nocode_reason}" if reason else nocode_reason
            logger.info(
                "NoCodeUnderDev [%s]: %d→%d — %s",
                title[:60], old_score, score, nocode_reason,
            )

        # === v5 Идея 31: размытость ТЗ -1 ===
        vague_mod, vague_reason = detect_vague_scope(title, description)
        if vague_mod:
            old_score = score
            score = max(0, score + vague_mod)
            reason = f"{reason}; {vague_reason}" if reason else vague_reason
            logger.info(
                "VagueScope [%s]: %d→%d — %s",
                title[:60], old_score, score, vague_reason,
            )

        # === v4 Идея 37: MAX мессенджер +1 ===
        max_mod, max_reason = detect_max_messenger(title, description)
        if max_mod:
            old_score = score
            score = min(10, score + max_mod)
            reason = f"{reason}; {max_reason}" if reason else max_reason
            logger.info(
                "MaxMessenger [%s]: %d→%d — %s",
                title[:60], old_score, score, max_reason,
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
            "competition_tier": competition_tier,
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
            "scoring_error": True,
        }


# === Волна 5c: динамика роста откликов ===

# Расписание авто-замеров (минуты от находки заказа): первый час 3 точки + 6ч.
RECHECK_SCHEDULE_MIN = (15, 45, 90, 360)


def next_recheck_delay_min(stage: int) -> Optional[int]:
    """Минуты до следующего замера для данной стадии (0-based). None = замеры окончены."""
    if 0 <= stage < len(RECHECK_SCHEDULE_MIN):
        return RECHECK_SCHEDULE_MIN[stage]
    return None


# Абсолютные пороги конкуренции (перебивают темп — вердикт монотонен по n1).
# Правка июль 2026 (Группа 4): 40→30 — настоящая мясорубка по абсолюту начинается
# с ~30 откликов; ранний кучный всплеск при <15 откликов мясорубкой НЕ считается.
HIGH_OFFERS_ABS = 30  # ≥ этого = мясорубка/скип независимо от темпа (см. память)
LOW_OFFERS_ABS = 15   # узкая ниша (наш кандидат) — только пока откликов меньше


def classify_offer_dynamics(n0: int, n1: int, elapsed_min: int) -> tuple[str, str]:
    """Волна 5c: классификация конкуренции по заказу.

    Учитывает И абсолютное число откликов, И темп роста — абсолют главнее,
    поэтому вердикт монотонен: заказ не «возвращается в норму» при поздней
    перепроверке (откликов только прибавляется).

    Args:
        n0: число откликов при находке.
        n1: число откликов сейчас.
        elapsed_min: минут прошло с находки.

    Returns:
        (verdict, note): verdict ∈ {"fast", "slow", "medium", "fresh"}.
          fast  — мясорубка: ≥30 откликов ИЛИ (≥25/час И ≥15 по абсолюту), коннект утонет.
          fresh — ранний кучный всплеск: <15 откликов, но темп ≥25/час. НЕ мясорубка,
                  а свежий заказ — перепроверить (не сворачивать карточку).
          slow  — узкая ниша: <15 откликов И темп <10/час, наш кандидат.
          medium — всё прочее.

    Логика George: 30 откликов — уже много (скип), за час ~10 → интересно.
    Правка июль 2026 (Группа 4): высокий темп при малом абсолюте (кучность в
    первые минуты) — не клеймо мясорубки, а кандидат на ручную перепроверку.
    """
    delta = max(0, n1 - n0)
    per_hour = delta / elapsed_min * 60 if elapsed_min > 0 else None
    rate_txt = f", +{delta} за {elapsed_min}мин ≈ {per_hour:.0f}/час" if per_hour is not None else ""

    # Абсолют главнее темпа: накопившаяся толпа — мясорубка, даже если рост затих.
    if n1 >= HIGH_OFFERS_ABS:
        return "fast", (
            f"🌊 уже {n1} откликов{rate_txt} — мясорубка, коннект утонет"
        )
    if per_hour is not None and per_hour >= 25:
        # Кучный ранний всплеск при малом абсолюте — не мясорубка, а свежий заказ.
        if n1 < LOW_OFFERS_ABS:
            return "fresh", (
                f"🆕 {n1} откликов, ранний всплеск{rate_txt} — свежий, перепроверить"
            )
        return "fast", (
            f"🌊 быстрый рост ({n1} откликов{rate_txt}) — мясорубка, коннект утонет"
        )
    if per_hour is not None and per_hour < 10 and n1 < LOW_OFFERS_ABS:
        return "slow", (
            f"🎯 {n1} откликов, темп низкий{rate_txt} — узкая ниша, наш кандидат"
        )
    return "medium", (
        f"🟡 {n1} откликов{rate_txt}"
    )


# Волна 5: маркеры дебаг-задачи (придержать рецепт починки до заказа)
_DEBUG_TASK_RE = re.compile(
    r"почему\s+не\s+работает|не\s+работает\s+\w+|перестал\w*\s+работа|"
    r"\bбаг\b|\bошибк\w+|\bне\s+запуска|падает\s+с\s+ошибк|"
    r"\bфикс\w+\b|\bпочини|исправить\s+(баг|ошибк|проблем)|"
    r"\bдебаг|debug|разобраться\s+почему",
    re.IGNORECASE,
)


def is_debug_task(title: str, description: str) -> bool:
    """Волна 5: задача на дебаг (найти причину) — придержать рецепт до заказа."""
    return bool(_DEBUG_TASK_RE.search(f"{title}\n{description}"))


def recommend_dump_price(kwork_price: int, is_fast: bool) -> Optional[int]:
    """Волна 5 (1.1/демпинг): рекомендованная цена для FAST-заказа — ниже
    нижней границы (психология "при прочих равных беру минимальную").

    Возвращает рекомендованную цифру (₽) или None если демпинг неуместен
    (BIG-проект или нет цены).
    """
    if not is_fast or not kwork_price or kwork_price <= 0:
        return None
    # Ниже нижней границы: -20%, округление до сотен, но не ниже 500₽.
    recommended = int(kwork_price * 0.8 // 100 * 100)
    return max(500, recommended)


async def generate_offer_claude(
    title: str,
    description: str,
    budget: str,
    anthropic_api_key: str,
    is_ai: bool = False,
    scope_unclear: bool = False,
    site_category: str = "not_site",
    is_fast: bool = False,
    max_retries: int = 1,
) -> str:
    is_debug = is_debug_task(title, description)
    prompt = OFFER_PROMPT_CLAUDE.format(
        title=title,
        description=description[:1500],
        budget=budget,
        is_ai="да" if is_ai else "нет",
        scope_unclear="да — НЕ давать готовое решение и точную цену" if scope_unclear else "нет",
        site_category=site_category,
        is_fast="да" if is_fast else "нет",
        is_debug="да" if is_debug else "нет",
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
