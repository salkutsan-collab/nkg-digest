# -*- coding: utf-8 -*-
"""
Архив найденных мероприятий - копится, чтобы потом смотреть за месяц/год.

Хранилище: data/archive/ГГГГ-ММ.jsonl, одна строка - одно уникальное событие.
Запись с дедупликацией (upsert) по ключу "название|площадка|дата начала":
  first_seen / last_seen - когда впервые и последний раз попадалось,
  seen_count             - сколько раз встречали,
  published              - попадало ли в пост, в какие дни,
  плюс все поля события (тип, даты, релевантность, персоны, ссылка, описание).

Источники (площадки) ведутся отдельно - data/participants.yaml.

Запуск руками:
  py agents/archive.py --stats            # сводка за все месяцы
  py agents/archive.py --month 2026-06    # сводка за месяц
"""

import os
import re
import sys
import json
import glob
import argparse
import datetime as dt
from collections import Counter

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ARCHIVE_DIR = os.path.join(ROOT, "data", "archive")


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip().lower()


def event_key(e):
    return "|".join([_norm(e.get("title")), _norm(e.get("_participant")),
                     str(e.get("date_start") or "")])


def _month_path(day):
    return os.path.join(ARCHIVE_DIR, f"{day.year}-{day.month:02d}.jsonl")


def _load(path):
    store = {}
    if not os.path.exists(path):
        return store
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                store[rec["key"]] = rec
            except Exception:
                continue
    return store


def _save(path, store):
    os.makedirs(ARCHIVE_DIR, exist_ok=True)
    rows = sorted(store.values(), key=lambda r: (r.get("date_start") or "", r.get("key")))
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


def _record(e, theme, today_iso):
    return {
        "key": event_key(e),
        "title": e.get("title"),
        "participant": e.get("_participant"),
        "category": e.get("_category"),
        "type": e.get("type"),
        "date_start": e.get("date_start"),
        "date_end": e.get("date_end"),
        "time": e.get("time"),
        "url": e.get("_url"),
        "about": e.get("about"),
        "relevance": e.get("relevance"),
        "persons": e.get("persons") or [],
        "themes": [theme] if theme else [],
        "first_seen": today_iso,
        "last_seen": today_iso,
        "seen_count": 1,
        "published": False,
        "published_days": [],
    }


def record_found(events, theme=""):
    """Записать найденные события (upsert по ключу) в архив текущего месяца."""
    if not events:
        return 0
    today = dt.date.today()
    iso = today.isoformat()
    path = _month_path(today)
    store = _load(path)
    n = 0
    for e in events:
        k = event_key(e)
        rec = store.get(k)
        if rec:
            rec["last_seen"] = iso
            rec["seen_count"] = rec.get("seen_count", 1) + 1
            if theme and theme not in rec.get("themes", []):
                rec.setdefault("themes", []).append(theme)
            # дозаполнить поля, если раньше были пустыми
            for f in ("about", "relevance", "date_end", "time", "url"):
                if not rec.get(f) and e.get(f if f != "url" else "_url"):
                    rec[f] = e.get(f if f != "url" else "_url")
        else:
            store[k] = _record(e, theme, iso)
            n += 1
    _save(path, store)
    return n


def mark_published(events, theme=""):
    """Отметить, что события попали в пост."""
    if not events:
        return
    today = dt.date.today()
    iso = today.isoformat()
    path = _month_path(today)
    store = _load(path)
    for e in events:
        rec = store.get(event_key(e))
        if not rec:
            store[event_key(e)] = _record(e, theme, iso)
            rec = store[event_key(e)]
        rec["published"] = True
        if iso not in rec.get("published_days", []):
            rec.setdefault("published_days", []).append(iso)
    _save(path, store)


# ---------- сводка ----------

def stats(month=None):
    paths = ([_month_path_str(month)] if month
             else sorted(glob.glob(os.path.join(ARCHIVE_DIR, "*.jsonl"))))
    all_recs = []
    for p in paths:
        if os.path.exists(p):
            all_recs.extend(_load(p).values())
    if not all_recs:
        print("Архив пуст." if not month else f"За {month} записей нет.")
        return
    pub = sum(1 for r in all_recs if r.get("published"))
    by_type = Counter(r.get("type") or "?" for r in all_recs)
    by_venue = Counter(r.get("participant") or "?" for r in all_recs)
    print(f"Всего уникальных событий: {len(all_recs)} (опубликовано: {pub})")
    print("\nПо типам:")
    for t, c in by_type.most_common():
        print(f"  {c:4d}  {t}")
    print("\nТоп площадок:")
    for v, c in by_venue.most_common(10):
        print(f"  {c:4d}  {v}")


def _month_path_str(month):
    return os.path.join(ARCHIVE_DIR, f"{month}.jsonl")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true")
    ap.add_argument("--month", help="ГГГГ-ММ, например 2026-06")
    args = ap.parse_args()
    stats(args.month)


if __name__ == "__main__":
    main()
