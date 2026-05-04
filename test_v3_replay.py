"""
v3 replay: прогоняет последние N заказов из projects.db через новый score_project
и печатает таблицу: id | category | score | score_big | score_fast | hard_reject | reason.

Запуск:
    python test_v3_replay.py [path_to_projects.db] [N]

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

from app.config_reader import Settings
from app.kwork_filter import score_project


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


if __name__ == "__main__":
    db_path = sys.argv[1] if len(sys.argv) > 1 else "app/db/database/projects.db"
    limit = int(sys.argv[2]) if len(sys.argv) > 2 else 50
    asyncio.run(main(db_path, limit))
