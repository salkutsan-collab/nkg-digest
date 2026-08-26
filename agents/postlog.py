# -*- coding: utf-8 -*-
"""
Журнал публикаций канала и счетчик подписчиков.

Два хранилища, по одной строке JSON на запись (append-only, git-дружелюбно):
  data/posts.jsonl        - каждый пост: когда, какой рубрики, в какой мессенджер,
                            ушел или нет, сколько сообщений и фото, id сообщений,
                            текст ошибки при сбое.
  data/subscribers.jsonl  - раз в сутки число подписчиков каждого канала.

Пишется из agents/broadcast.py, поэтому покрывает ВСЕ посты каналов сразу:
дайджест, разбор дня, персону недели, стрит-арт, рекомендацию.

Время - московское (сервер GitHub живет по UTC, а сводка нужна по нашим суткам).

Запуск руками (Windows - py):
  py agents/postlog.py --stats             # сводка за все время
  py agents/postlog.py --month 2026-08     # сводка за месяц
  py agents/postlog.py --subscribers       # снять число подписчиков сейчас
"""

import os
import sys
import json
import argparse
import datetime as dt
from collections import Counter, defaultdict

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
POSTS_PATH = os.path.join(DATA_DIR, "posts.jsonl")
SUBS_PATH = os.path.join(DATA_DIR, "subscribers.jsonl")

MSK = dt.timezone(dt.timedelta(hours=3))

# понятные названия рубрик для сводки
KIND_NAMES = {
    "digest": "дайджест дня",
    "feature": "разбор дня",
    "person": "персона недели",
    "streetart": "стрит-арт",
    "recommend": "рекомендация дня",
    "?": "без рубрики",
}


def _now():
    return dt.datetime.now(MSK)


def _append(path, rec):
    """Дописать строку в журнал. Журнал не должен ронять публикацию,
    поэтому любая ошибка записи только печатается."""
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return True
    except Exception as e:
        print(f"  (журнал: запись не удалась: {str(e)[:120]})")
        return False


def _read(path):
    out = []
    if not os.path.exists(path):
        return out
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except Exception:
                continue
    return out


# ---------- запись ----------

def log_post(kind, channel, ok, parts=0, photos=0, chars=0,
             message_ids=None, error="", extra=None):
    """Записать один пост (отдельная запись на каждый мессенджер).

    kind    - рубрика: digest / feature / person / streetart / recommend
    channel - telegram / max
    ok      - ушло ли в этот мессенджер
    parts   - сколько сообщений заняло (длинный текст режется на части)
    photos  - сколько фото отправлено
    chars   - длина текста в знаках
    """
    now = _now()
    rec = {
        "ts": now.isoformat(timespec="seconds"),
        "date": now.date().isoformat(),
        "weekday": now.isoweekday(),
        "kind": kind or "?",
        "channel": channel,
        "ok": bool(ok),
        "parts": int(parts or 0),
        "photos": int(photos or 0),
        "chars": int(chars or 0),
        "message_ids": [i for i in (message_ids or []) if i],
    }
    if error:
        rec["error"] = str(error)[:300]
    if extra:
        rec.update({k: v for k, v in extra.items() if v not in (None, "")})
    _append(POSTS_PATH, rec)
    return rec


# ---------- подписчики ----------

def _counted_today():
    """Каналы, по которым число подписчиков за сегодня уже снято."""
    today = _now().date().isoformat()
    return {r.get("channel") for r in _read(SUBS_PATH) if r.get("date") == today}


def _telegram_count():
    import notify_telegram
    return notify_telegram.member_count()


def _max_count():
    import notify_max
    if not notify_max.configured():
        return None
    return notify_max.member_count()


def snapshot_subscribers(force=False):
    """Снять число подписчиков каналов и записать (один раз в сутки на канал).
    Возвращает словарь {канал: число}. Сбой одного канала не мешает другому."""
    done = set() if force else _counted_today()
    out = {}
    for channel, fn in (("telegram", _telegram_count), ("max", _max_count)):
        if channel in done:
            continue
        try:
            n = fn()
        except SystemExit as e:   # notify_* кидают SystemExit, если нет токена
            print(f"  (подписчики {channel}: не настроен: {str(e)[:120]})")
            continue
        except Exception as e:
            print(f"  (подписчики {channel}: не посчитать: {str(e)[:120]})")
            continue
        if n is None:
            continue
        now = _now()
        _append(SUBS_PATH, {"ts": now.isoformat(timespec="seconds"),
                            "date": now.date().isoformat(),
                            "channel": channel, "count": int(n)})
        out[channel] = int(n)
    return out


# ---------- сводка ----------

def _in_month(rec, month):
    return (not month) or str(rec.get("date", "")).startswith(month)


def _post_keys(recs, channel=None):
    """Один пост = одна рубрика за день в одном мессенджере. Нужно потому, что
    пост может уйти несколькими сообщениями (фото отдельно, длинный текст частями),
    и по строкам журнала его легко посчитать дважды."""
    return {(r.get("date"), r.get("kind"), r.get("channel")) for r in recs
            if (channel is None or (r.get("channel") or "telegram") == channel)}


def stats(month=None):
    posts = [r for r in _read(POSTS_PATH) if _in_month(r, month)]
    subs = [r for r in _read(SUBS_PATH) if _in_month(r, month)]
    label = f"за {month}" if month else "за все время"
    if not posts and not subs:
        print(f"Журнал публикаций пуст ({label}).")
        return

    ok = [r for r in posts if r.get("ok")]
    bad = [r for r in posts if not r.get("ok")]
    print(f"Публикации {label}: постов в Telegram-канале "
          f"{len(_post_keys(ok, 'telegram'))}, "
          f"отправок сообщений {len(posts)} "
          f"(удачных {len(ok)}, со сбоем {len(bad)}).")

    by_ch = Counter(ch for _, _, ch in _post_keys(ok))
    if by_ch:
        print("\nПостов доставлено по мессенджерам:")
        for c, n in by_ch.most_common():
            print(f"  {n:4d}  {c}")

    by_kind = Counter(k for _, k, _ in _post_keys(ok, "telegram"))
    if by_kind:
        print("\nПо рубрикам (посты в Telegram-канале):")
        for k, n in by_kind.most_common():
            print(f"  {n:4d}  {KIND_NAMES.get(k, k)}")

    photos = sum(int(r.get("photos") or 0) for r in ok
                 if (r.get("channel") or "telegram") == "telegram")
    if photos:
        print(f"\nФото отправлено (Telegram): {photos}")

    # дни считаем по Telegram, иначе один пост удвоится на два мессенджера
    days = Counter(d for d, _, ch in _post_keys(ok, "telegram") if d)
    if days:
        ds = sorted(days)
        print(f"\nДней с публикациями: {len(ds)} (с {ds[0]} по {ds[-1]}), "
              f"постов в среднем за день: {sum(days.values()) / len(ds):.1f}")
        print("Последние 14 дней:")
        for d in ds[-14:]:
            print(f"  {d}  {days[d]} пост(ов)")
        miss = _missing_days(ds[0], ds[-1], set(ds))
        print(f"\nДней без публикаций внутри периода: {len(miss)}")
        if miss:
            print("  " + ", ".join(miss))

    if bad:
        print("\nСбои доставки (последние 10):")
        for r in bad[-10:]:
            print(f"  {r.get('date')} {r.get('channel')} "
                  f"{KIND_NAMES.get(r.get('kind'), r.get('kind'))}: "
                  f"{(r.get('error') or 'без пояснения')[:100]}")

    _subs_report(subs)


def _missing_days(first, last, have):
    a = dt.date.fromisoformat(first)
    b = dt.date.fromisoformat(last)
    out = []
    while a <= b:
        if a.isoformat() not in have:
            out.append(a.isoformat())
        a += dt.timedelta(days=1)
    return out


def _subs_report(subs):
    if not subs:
        print("\nПодписчики: замеров пока нет.")
        return
    print("\nПодписчики:")
    by_ch = defaultdict(list)
    for r in subs:
        by_ch[r.get("channel") or "?"].append(r)
    for ch, rows in by_ch.items():
        rows.sort(key=lambda r: r.get("date") or "")
        first, last = rows[0], rows[-1]
        delta = int(last.get("count", 0)) - int(first.get("count", 0))
        sign = "+" if delta > 0 else ""
        print(f"  {ch}: сейчас {last.get('count')} ({last.get('date')}), "
              f"было {first.get('count')} ({first.get('date')}), "
              f"изменение {sign}{delta}")
        tail = rows[-7:]
        if len(tail) > 1:
            print("    " + ", ".join(f"{r.get('date')}: {r.get('count')}" for r in tail))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--stats", action="store_true", help="сводка")
    ap.add_argument("--month", help="ГГГГ-ММ, например 2026-08")
    ap.add_argument("--subscribers", action="store_true",
                    help="снять число подписчиков сейчас")
    ap.add_argument("--force", action="store_true",
                    help="снять подписчиков, даже если за сегодня уже снимали")
    args = ap.parse_args()

    if args.subscribers:
        got = snapshot_subscribers(force=args.force)
        if got:
            for ch, n in got.items():
                print(f"{ch}: {n} подписчиков - записано.")
        else:
            print("Новых замеров нет (за сегодня уже снято или каналы не настроены).")
        return
    stats(args.month)


if __name__ == "__main__":
    main()
