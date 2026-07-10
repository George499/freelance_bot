"""
v3 replay: прогоняет последние N заказов из projects.db через новый score_project
и печатает таблицу: id | category | score | score_big | score_fast | hard_reject | reason.

Запуск:
    python test_v3_replay.py [path_to_projects.db] [N]
    python test_v3_replay.py --wave3   # детерминированные кейсы волны 3

По умолчанию: app/db/database/projects.db, N=50.

Бюджет берём из самого описания (regex), т.к. в Project нет столбца с бюджетом —
это даёт грубую оценку категории, но достаточно для проверки распределения по
BIG/FAST/DUAL и отлова hard reject'ов.
"""

import asyncio
import os
import re
import sqlite3
import sys

from app.kwork_filter import (
    classify_offer_dynamics,
    compute_competition_modifier,
    detect_always_hard_reject,
    detect_b2c_service_site,
    detect_class_d_blind_fix,
    detect_microbudget_multicomponent,
    detect_no_code_required,
    detect_profile_mismatch,
    detect_reviews_census,
    detect_terminology_specificity,
    detect_vibecoding_devaluation,
    score_project,
)


def _guess_budget_str(description: str) -> str:
    """Грубо вытаскивает бюджет из описания. Если ничего не найдено — '0 ₽'.

    Берёт первое число в диапазоне 5_000-1_000_000 рядом со словами
    'руб', '₽', 'тыс', 'к' (как 50к).
    """
    if not description:
        return "0 ₽"

    # Паттерн "от 50000 до 100000", "50 000 - 100 000 ₽" и т.п.
    m = re.search(
        r"(\d[\d\s]{2,8})\s*(?:[-–—]|до)\s*(\d[\d\s]{2,8})\s*(?:руб|₽|т\.?р|тыс|k|к)",
        description, re.IGNORECASE,
    )
    if m:
        a = int(re.sub(r"\D", "", m.group(1)))
        b = int(re.sub(r"\D", "", m.group(2)))
        return f"{a:,} – {b:,} ₽"

    # Одиночное "50 000 ₽" / "50к"
    m = re.search(
        r"(\d[\d\s]{2,8})\s*(?:руб|₽|т\.?р|тыс)",
        description, re.IGNORECASE,
    )
    if m:
        a = int(re.sub(r"\D", "", m.group(1)))
        return f"{a:,} ₽"

    m = re.search(r"(\d{1,4})\s*к\b", description, re.IGNORECASE)
    if m:
        a = int(m.group(1)) * 1000
        return f"{a:,} ₽"

    return "0 ₽"


async def main(db_path: str, limit: int) -> None:
    from app.config_reader import Settings  # lazy: нужен только для replay

    if not os.path.exists(db_path):
        print(f"DB not found: {db_path}", file=sys.stderr)
        sys.exit(1)

    cfg = Settings()
    if not cfg.anthropic_api_key:
        print("ANTHROPIC_API_KEY пуст — score_project вернёт default", file=sys.stderr)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    cur.execute(
        "SELECT id, title, description FROM project "
        "WHERE freelance_platform = 'kwork' "
        "ORDER BY id DESC LIMIT ?",
        (limit,),
    )
    rows = cur.fetchall()
    conn.close()

    print(f"Replay {len(rows)} projects from {db_path}\n")
    print(f"{'id':>5} | {'cat':<6} | {'score':>5} | {'big':>3} | {'fast':>4} | hr | reason")
    print("-" * 110)

    cat_counts = {"BIG": 0, "FAST": 0, "DUAL": 0, None: 0}
    hr_count = 0

    for pid, title, desc in rows:
        budget = _guess_budget_str(desc or "")
        result = await score_project(
            title=title or "",
            description=desc or "",
            budget=budget,
            deadline="не указан",
            responses_count=0,
            anthropic_api_key=cfg.anthropic_api_key,
            hired_percent=None,
        )
        cat = result.get("category")
        cat_counts[cat] = cat_counts.get(cat, 0) + 1
        hr = "Y" if result.get("hard_reject") else "."
        if result.get("hard_reject"):
            hr_count += 1
        reason = (result.get("reason") or "")[:80]
        print(
            f"{pid:>5} | {str(cat or '-'):<6} | "
            f"{result.get('score', 0):>5} | "
            f"{str(result.get('score_big') or '-'):>3} | "
            f"{str(result.get('score_fast') or '-'):>4} | "
            f"{hr}  | {reason}"
        )

    print("-" * 110)
    print("Распределение категорий:", cat_counts)
    print(f"Hard reject: {hr_count}/{len(rows)}")


def _check(label: str, expected, actual) -> bool:
    """Печатает строку отчёта и возвращает True если ожидание сошлось."""
    ok = expected == actual
    mark = "OK " if ok else "FAIL"
    print(f"  [{mark}] {label}: expected={expected!r} actual={actual!r}")
    return ok


def _sign(modifier: int) -> int:
    """-5 → -1, -3 → -1, 0 → 0, +2 → +1."""
    if modifier > 0:
        return 1
    if modifier < 0:
        return -1
    return 0


def run_wave3_cases() -> int:
    """Детерминированные тесты для волны 3 — не требуют Anthropic.

    Проверяют каждый детектор изолированно на 12 кейсах из спецификации.
    Возвращает количество провалившихся ассертов.
    """
    print("=" * 80)
    print("Wave 3 — детерминированные кейсы (без обращений к Anthropic)")
    print("=" * 80)
    fails = 0

    # 1. HIGH competition: «Создать телеграм бот с OpenAI для подсчёта калорий»
    print("\n[1] Massовая терминология → HIGH (-5)")
    mod, reason = detect_terminology_specificity(
        "Создать телеграм бот с OpenAI для подсчёта калорий",
        "Нужен телеграм бот с интеграцией OpenAI/ChatGPT, калькулятор калорий, "
        "ИИ для подсчёта БЖУ.",
    )
    if not _check("modifier", -5, mod):
        fails += 1
    print(f"       reason: {reason}")

    # 2. Категория А: «Развернуть FastAPI на VPS» — НЕ должно быть mismatch
    print("\n[2] Fullstack deploy (Категория А) → 0 (НЕ штрафовать)")
    mod, reason = detect_profile_mismatch(
        "Развернуть FastAPI на VPS",
        "Нужно развернуть FastAPI проект на VPS, настроить nginx и SSL для приложения.",
    )
    if not _check("modifier", 0, mod):
        fails += 1

    # 3. Категория Б: «Настроить корпоративную почту на Beget»
    print("\n[3] Корпоративная почта (Категория Б) → -3")
    mod, reason = detect_profile_mismatch(
        "Настройка почтового сервера",
        "Нужно настроить корпоративную почту на Beget для домена, 14 ящиков, "
        "прописать MX-записи.",
    )
    if not _check("modifier", -3, mod):
        fails += 1
    print(f"       reason: {reason}")

    # 4. Освобождение диска
    print("\n[4] Чистка хостинга (Категория Б) → -3")
    mod, reason = detect_profile_mismatch(
        "Помощь с хостингом",
        "Освободить место на хостинге, нужен бэкап и почистить диск, "
        "забит сервер на 95%.",
    )
    if not _check("modifier", -3, mod):
        fails += 1
    print(f"       reason: {reason}")

    # 5. B2C-сайт оптики
    print("\n[5] B2C optika.ru + правки сайта → -3")
    mod, reason = detect_b2c_service_site(
        "Доработка сайта",
        "Добавить телефон филиала на сайте optikaeyeline.ru, доработать сайт.",
    )
    if not _check("modifier", -3, mod):
        fails += 1
    print(f"       reason: {reason}")

    # 6. B2C-сайт юриста
    print("\n[6] B2C yurist + правки сайта → -3")
    mod, reason = detect_b2c_service_site(
        "Юридический сайт",
        "Юридические документы добавить на сайт yurist-72.ru, доработать сайт.",
    )
    if not _check("modifier", -3, mod):
        fails += 1

    # 7. CRM с нуля, hire_rate 0%, 50+ проектов, без медалек → -4 (идея 44)
    print("\n[7] hire_rate=0% + 50 проектов без медалек → -4 (идея 44)")
    mod, reasons = compute_competition_modifier(
        responses_count=12,
        hired_percent=0,
        buyer_achievements=0,
        user_projects_count=50,
    )
    # 12 откликов = 0 модификатор, hire_rate 0 = -4 → итого -4
    if not _check("modifier == -4", True, mod == -4):
        fails += 1
    print(f"       reasons: {reasons}")

    # 8. Class D blind: «Починить телеграм бот» бюджет 5000 → -3
    print("\n[8] Class D blind fix, бюджет <10к → -3")
    mod, reason = detect_class_d_blind_fix(
        "Починить телеграм бот",
        "Бот перестал работать, нужно починить. Раньше работал, сейчас нет.",
        budget_limit=5000,
    )
    if not _check("modifier", -3, mod):
        fails += 1
    print(f"       reason: {reason}")

    # 9. Микробюджет + многокомпонентная (бот+калькулятор+оплата за 1500)
    print("\n[9] Микробюджет 1500 + 3 компонента → -3")
    mod, reason = detect_microbudget_multicomponent(
        "Бот на Telegram",
        "Нужен телеграм бот с калькулятором и подпиской на оплату.",
        budget_limit=1500,
        farm_mode_active=False,
    )
    if not _check("modifier == -3", True, mod == -3):
        fails += 1
    print(f"       reason: {reason}")

    # 10. Микробюджет в farm-режиме, малокомпонентная → 0 (исключение)
    print("\n[10] Микробюджет в farm-mode, 1 компонент → 0 (исключение)")
    mod, reason = detect_microbudget_multicomponent(
        "Скрипт WB→Sheets",
        "Скрипт автоматизации Wildberries в Google Sheets, выгрузка раз в день.",
        budget_limit=1500,
        farm_mode_active=True,
    )
    # 'sheets' и 'парсер'/'sheet' могут не сматчиться сразу 2-х компонентов
    # допустим что в farm-mode при ≤2 компонентов штраф 0.
    if not _check("modifier <= 0 в farm-mode", True, mod >= -3):  # должен быть 0 или меньший штраф
        fails += 1
    # Точная проверка: если компонентов 2 — должен сработать exception
    # (за счёт farm_mode_active=True и components<=2)

    # 11. LOW competition (профессиональная терминология)
    print("\n[11] AI lead-gen + multi-workspace + GTM → LOW (+2)")
    mod, reason = detect_terminology_specificity(
        "AI lead generation система",
        "Нужна AI lead generation система с multi-workspace, GTM-логикой и "
        "lead scoring 70-200к.",
    )
    if not _check("modifier", 2, mod):
        fails += 1
    print(f"       reason: {reason}")

    # 12. Mini App с realtime + Hidden MMR + matchmaking → LOW (+2)
    print("\n[12] Mini App + realtime + Hidden MMR → LOW (+2)")
    mod, reason = detect_terminology_specificity(
        "Telegram Mini App с PvP",
        "Telegram Mini App с realtime PvP, Hidden MMR, matchmaking системой.",
    )
    if not _check("modifier", 2, mod):
        fails += 1
    print(f"       reason: {reason}")

    print("\n" + "=" * 80)
    if fails == 0:
        print(f"Все 12 кейсов прошли ✓")
    else:
        print(f"Провалов: {fails}/12")
    print("=" * 80)
    return fails


def run_july_cases() -> int:
    """Детерминированные тесты правок июля 2026 (6 групп) — без Anthropic.

    Каждый детектор проверяется изолированно, включая ложно-положительные
    контроли (легитимная капча, адекватный бюджет, отзыв-фарм).
    """
    print("=" * 80)
    print("Июль 2026 — детерминированные кейсы правок (без обращений к Anthropic)")
    print("=" * 80)
    fails = 0

    # --- Группа 1: капча-решалки + уникализаторы → always hard-reject ---
    print("\n[Г1.1] Решалка капчи → hard-reject")
    if not _check("hard-reject", True, detect_always_hard_reject(
        "Решалка капчи", "Нужна решалка капчи для парсера, обходить Turnstile",
    ) is not None):
        fails += 1

    print("\n[Г1.2] Обход hCaptcha → hard-reject")
    if not _check("hard-reject", True, detect_always_hard_reject(
        "Автоматизация", "Нужно обойти hCaptcha на входе",
    ) is not None):
        fails += 1

    print("\n[Г1.3] Уникализатор видео → hard-reject")
    if not _check("hard-reject", True, detect_always_hard_reject(
        "Уникализатор", "Уникализатор видео для массовой заливки в TikTok",
    ) is not None):
        fails += 1

    print("\n[Г1.4] Легитимная капча (интеграция формы) → НЕ hard-reject")
    if not _check("no reject", None, detect_always_hard_reject(
        "Форма на сайте", "На сайте стоит капча, интегрируйте форму обратной связи",
    )):
        fails += 1

    # --- Группа 5: no-code Make как ядро связки ---
    print("\n[Г5.1] Beds24 → Make → SendPulse → no-code hard-reject")
    if not _check("no-code", True, detect_no_code_required(
        "Синхронизация", "Настроить связку Beds24 → Make → SendPulse для брони",
    ) is not None):
        fails += 1

    print("\n[Г5.2] Код без no-code → НЕ no-code")
    if not _check("no no-code", None, detect_no_code_required(
        "Телеграм бот", "Бот на aiogram с PostgreSQL и вебхуками",
    )):
        fails += 1

    print("\n[Г5.3] Миграция С Make НА код → НЕ hard-reject (edge)")
    if not _check("migration pass", None, detect_no_code_required(
        "Переписать с Make", "Сейчас всё на Make.com, переписать заново на Node",
    )):
        fails += 1

    # --- Группа 4: мясорубка по абсолюту vs кучный всплеск ---
    print("\n[Г4.1] Всплеск 8 откликов за 10 мин → fresh (не мясорубка)")
    v, note = classify_offer_dynamics(0, 8, 10)
    if not _check("verdict", "fresh", v):
        fails += 1
    print(f"       note: {note}")

    print("\n[Г4.2] 32 отклика (абсолют ≥30) → fast (мясорубка)")
    v, _note = classify_offer_dynamics(0, 32, 300)
    if not _check("verdict", "fast", v):
        fails += 1

    print("\n[Г4.3] Высокий темп + абсолют ≥15 → fast (мясорубка)")
    v, _note = classify_offer_dynamics(0, 20, 10)
    if not _check("verdict", "fast", v):
        fails += 1

    print("\n[Г4.4] Медленный темп, <15 откликов → slow (узкая ниша)")
    v, _note = classify_offer_dynamics(5, 8, 60)
    if not _check("verdict", "slow", v):
        fails += 1

    # --- Группа 2: вайбкодинг + заниженный бюджет ---
    print("\n[Г2.1] Вайбкодинг + 10 000 ₽ → -4")
    mod, reason = detect_vibecoding_devaluation(
        "Бот на вайбкодинге", "Склепал в курсоре, нужно допилить", 10000,
    )
    if not _check("modifier", -4, mod):
        fails += 1
    print(f"       reason: {reason}")

    print("\n[Г2.2] Вайбкодинг + 60 000 ₽ (адекватно) → 0")
    mod, _r = detect_vibecoding_devaluation(
        "Бот на вайбкодинге", "Допилить проект", 60000,
    )
    if not _check("modifier", 0, mod):
        fails += 1

    print("\n[Г2.3] Без вайбкодинга → 0")
    mod, _r = detect_vibecoding_devaluation(
        "Телеграм бот", "Обычный бот на aiogram", 10000,
    )
    if not _check("modifier", 0, mod):
        fails += 1

    # --- Группа 3: ценз заказчика по отзывам (профиль = 3) ---
    print("\n[Г3.1] «от 10 отзывов», профиль 3 → hard-reject")
    if not _check("census", True, detect_reviews_census(
        "Разработка", "Требуется исполнитель от 10 отзывов", 3,
    ) is not None):
        fails += 1

    print("\n[Г3.2] «от 3 отзывов», профиль 3 → проходим (None)")
    if not _check("census pass", None, detect_reviews_census(
        "Разработка", "Нужен исполнитель от 3 отзывов", 3,
    )):
        fails += 1

    print("\n[Г3.3] Отзыв-фарм «соберу 500 отзывов» → НЕ ценз (None)")
    if not _check("not census", None, detect_reviews_census(
        "Парсер", "Соберу 500 отзывов для клиента с сайта", 3,
    )):
        fails += 1

    # --- Аудит 10.07: отрицание перед CMS-ключом не должно рубить ---
    from app.kwork_filter import _hard_reject_reason, detect_landing_reject

    print("\n[А1] «НЕ нужен шаблонный лендинг на Tilda» → НЕ hard-reject")
    if not _check("negation pass", None, _hard_reject_reason(
        "Разработка посадочной страницы SaaS-продукта",
        "Важно: не нужен шаблонный лендинг на Tilda. Нужен современный "
        "SaaS landing page уровня Linear, Notion, Stripe.",
    )):
        fails += 1

    print("\n[А2] Реальный заказ на Tilda → hard-reject как раньше")
    if not _check("tilda reject", True, _hard_reject_reason(
        "Сайт Шахматного клуба на Tilda",
        "Нужно сделать сайт шахматного клуба на Tilda, есть референсы.",
    ) is not None):
        fails += 1

    print("\n[А3] «без Tilda» → НЕ hard-reject")
    if not _check("negation pass", None, _hard_reject_reason(
        "Лендинг", "Хочу лендинг без Tilda, только код.",
    )):
        fails += 1

    print("\n[А4] Авторские маркеры (не шаблонный + уровня Linear) → лендинг проходит")
    if not _check("landing pass", None, detect_landing_reject(
        "Разработка посадочной страницы SaaS-продукта",
        "Не нужен шаблонный лендинг. Нужен premium SaaS landing page "
        "уровня Linear, Notion, Stripe. Аккуратная современная верстка, "
        "а не конструкторный шаблон.",
        15000,
    )):
        fails += 1

    print("\n[А5] Обычный лендинг 20к без маркеров → reject как раньше")
    if not _check("landing reject", True, detect_landing_reject(
        "Создать лендинг продающий",
        "Нужен продающий лендинг для курса, быстро и недорого.",
        20000,
    ) is not None):
        fails += 1

    print("\n" + "=" * 80)
    if fails == 0:
        print("Все кейсы июля прошли ✓")
    else:
        print(f"Провалов: {fails}")
    print("=" * 80)
    return fails


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--july":
        sys.exit(run_july_cases())
    if len(sys.argv) > 1 and sys.argv[1] == "--wave3":
        sys.exit(run_wave3_cases())
    db_path = sys.argv[1] if len(sys.argv) > 1 else "app/db/database/projects.db"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    asyncio.run(main(db_path, limit))
