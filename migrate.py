"""Перенос/обновление из старой базы birthday_calendar.db из командной строки.

    python migrate.py старый.db data/birthdays.db           # создать или слить в существующую
    python migrate.py старый.db data/birthdays.db --dry-run # только показать, что изменится

То же самое умеет бот: администратор просто отправляет ему файл .db.
Логика слияния описана в app/importer.py.
"""
from __future__ import annotations

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.importer import NotLegacyDB, make_backup, merge_legacy  # noqa: E402

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("src")
    ap.add_argument("dst")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    os.makedirs(os.path.dirname(os.path.abspath(a.dst)), exist_ok=True)
    if os.path.exists(a.dst) and not a.dry_run:
        print("Резервная копия:", make_backup(a.dst, os.path.join(os.path.dirname(a.dst) or ".", "backups"),
                                              tag="import"))
    try:
        rep = merge_legacy(a.src, a.dst, apply=not a.dry_run)
    except NotLegacyDB as e:
        sys.exit(str(e))
    print(("ПРОБНЫЙ ПРОГОН — ничего не записано\n" if a.dry_run else "") + rep.text())
