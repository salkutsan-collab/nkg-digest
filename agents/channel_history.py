# -*- coding: utf-8 -*-
"""
История канала: что реально стоит в Telegram-канале и сколько это просмотрели.

Зачем отдельно от журнала публикаций (postlog):
  postlog          - что бот ПЫТАЛСЯ отправить (пишется в момент отправки);
  channel_history  - что в канале ЛЕЖИТ на самом деле, плюс просмотры.

Откуда данные: канал публичный, поэтому у него есть веб-витрина t.me/s/<канал> -
там весь архив с текстом, датой, номером сообщения и счетчиком просмотров.
Тем же приемом агент 3 читает стрит-арт каналы. Bot API историю канала не отдает
(метода нет), а витрина отдает - и без токена.

Чего витрина НЕ дает: реакции и репосты. Для них нужен клиент MTProto
(личный аккаунт, api_id/api_hash) - это отдельное решение.

Хранилище: data/channel_history.jsonl, одна строка - один пост канала,
обновление по номеру сообщения (просмотры при повторном прогоне освежаются).

Запуск (Windows - py):
  py agents/channel_history.py --backfill          # пройти всю историю канала
  py agents/channel_history.py --refresh 40        # освежить просмотры последних постов
  py agents/channel_history.py --stats             # сводка: посты и просмотры
"""

import os
import re
import sys
import json
import time
import argparse
import datetime as dt
from collections import Counter, defaultdict

import requests
from bs4 import BeautifulSoup

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")
HISTORY_PATH = os.path.join(DATA_DIR, "channel_history.jsonl")

MSK = dt.timezone(dt.timedelta(hours=3))
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nkg-digest/1.0)"}

# по первой строке поста понятно, что это за рубрика
KIND_MARKERS = [
    ("разбор дня", "feature"),
    ("персона недели", "person"),
    ("рекомендация дня", "recommend"),
    ("новое на стенах города", "streetart"),
]

KIND_NAMES = {
    "digest": "дайджест дня",
    "feature": "разбор дня",
    "person": "персона недели",
    "streetart": "стрит-арт",
    "recommend": "рекомендация дня",
    "photo": "фото-зацеп",
    "part": "продолжение поста",
    "?": "не распознано",
}


def channel_handle():
    """Публичное имя канала. Берется из TELEGRAM_CHANNEL или из настроек бота."""
    name = os.environ.get("TELEGRAM_CHANNEL") or ""
    if not name:
        chat = os.environ.get("TELEGRAM_CHAT_ID") or ""
        if chat.startswith("@"):
            name = chat
    if not name:
        try:
            import notify_telegram as nt
            token, chat = nt._cfg()
            r = requests.post(
                f"https://api.telegram.org/bot{token}/getChat",
                json={"chat_id": chat}, timeout=30).json()
            name = (r.get("result") or {}).get("username") or ""
        except Exception:
            name = ""
    return name.lstrip("@")


# ---------- разбор витрины ----------

def _views(text):
    """«2», «1.2K», «15M» - в число."""
    s = (text or "").strip().upper().replace(",", ".")
    mult = {"K": 1000, "M": 1000000}
    try:
        if s and s[-1] in mult:
            return int(float(s[:-1]) * mult[s[-1]])
        return int(float(s))
    except Exception:
        return None


def _theme_titles():
    """Заголовки тематических дайджестов - по ним узнается рубрика digest."""
    try:
        import publisher as pub
        return [str(t.get("title") or "").lower()
                for t in (pub.load_themes() or {}).get("days", {}).values()]
    except Exception:
        return []


# Подзаголовок дайджеста - диапазон дат («14 июня - 20 июня»), иногда с темой дня
# через точку-разделитель. Сам заголовок дайджеста каждый раз новый (его пишет
# модель), поэтому рубрику узнаем именно по этой строке.
RANGE_RE = re.compile(r"^(?:.*·\s*)?\d{1,2}\s+[а-яё]+\s*[-–]\s*\d{1,2}\s+[а-яё]+\s*$",
                      re.IGNORECASE)


def classify(text, has_photo, theme_titles):
    """Определить рубрику поста: по первой строке, а дайджест - по строке с датами."""
    body = (text or "").strip()
    if not body:
        return "photo" if has_photo else "?"
    lines = [ln.strip() for ln in body.splitlines() if ln.strip()]
    first = lines[0].lower()
    for marker, kind in KIND_MARKERS:
        if first.startswith(marker) or marker in first[:40]:
            return kind
    for title in theme_titles:
        if title and (first.startswith(title[:20]) or title in first):
            return "digest"
    if any(RANGE_RE.match(ln) for ln in lines[:3]):
        return "digest"
    # дайджест мог уехать несколькими сообщениями: продолжение без заголовка
    if body.count("•") >= 2 or body.count("http") >= 2:
        return "part"
    return "?"


def fetch_page(handle, before=None):
    """Одна страница витрины. Возвращает (посты, наименьший номер сообщения)."""
    url = f"https://t.me/s/{handle}"
    params = {"before": before} if before else None
    r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    if r.status_code >= 400:
        raise RuntimeError(f"t.me/s/{handle}: код {r.status_code}")
    soup = BeautifulSoup(r.text, "html.parser")
    posts, min_id = [], None
    for msg in soup.select("div.tgme_widget_message"):
        post = msg.get("data-post") or ""
        m = re.search(r"/(\d+)$", post)
        if not m:
            continue
        mid = int(m.group(1))
        min_id = mid if min_id is None else min(min_id, mid)
        txt_el = msg.select_one(".tgme_widget_message_text")
        text = txt_el.get_text("\n", strip=True) if txt_el else ""
        time_el = msg.select_one("time[datetime]")
        iso = time_el.get("datetime") if time_el else None
        views_el = msg.select_one(".tgme_widget_message_views")
        photos = len(msg.select(".tgme_widget_message_photo_wrap"))
        when = None
        if iso:
            try:
                when = dt.datetime.fromisoformat(iso).astimezone(MSK)
            except Exception:
                when = None
        posts.append({
            "id": mid,
            "ts": when.isoformat(timespec="seconds") if when else None,
            "date": when.date().isoformat() if when else None,
            "text_head": (text.splitlines()[0][:120] if text else ""),
            # вторая строка: в «Разборе дня» там название события и площадка
            "text_sub": (text.splitlines()[1][:160]
                         if text and len(text.splitlines()) > 1 else ""),
            "_text": text,
            "chars": len(text),
            "photos": photos,
            "views": _views(views_el.get_text() if views_el else None),
            "url": f"https://t.me/{handle}/{mid}",
        })
    return posts, min_id


# ---------- хранилище ----------

def load():
    """Прочитать историю: {номер сообщения: запись}."""
    store = {}
    if not os.path.exists(HISTORY_PATH):
        return store
    with open(HISTORY_PATH, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
                store[int(rec["id"])] = rec
            except Exception:
                continue
    return store


def save(store):
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(HISTORY_PATH, "w", encoding="utf-8") as fh:
        for mid in sorted(store):
            fh.write(json.dumps(store[mid], ensure_ascii=False) + "\n")


def collect(pages=60, before=None, pause=0.7):
    """Пройти витрину назад по страницам и обновить историю.
    Возвращает (добавлено, обновлено)."""
    handle = channel_handle()
    if not handle:
        print("Не удалось узнать публичное имя канала (нужен TELEGRAM_CHANNEL "
              "или доступ к getChat).")
        return 0, 0
    print(f"Канал: t.me/s/{handle}")
    theme_titles = _theme_titles()
    store = load()
    added = updated = 0
    for page in range(pages):
        try:
            posts, min_id = fetch_page(handle, before)
        except Exception as e:
            print(f"  (страница не прочиталась: {str(e)[:120]})")
            break
        if not posts:
            break
        for p in posts:
            p["kind"] = classify(p.pop("_text", ""), p.get("photos"), theme_titles)
            old = store.get(p["id"])
            if old:
                # просмотры меняются со временем, рубрика - когда умнеет классификатор
                if old.get("views") != p.get("views"):
                    old["views"] = p.get("views")
                    old["views_checked"] = dt.datetime.now(MSK).date().isoformat()
                    updated += 1
                if old.get("kind") != p.get("kind"):
                    old["kind"] = p.get("kind")
                    updated += 1
            else:
                p["views_checked"] = dt.datetime.now(MSK).date().isoformat()
                store[p["id"]] = p
                added += 1
        print(f"  страница {page + 1}: постов {len(posts)}, "
              f"номера с {min_id} (всего в истории {len(store)})")
        if min_id is None or min_id <= 1:
            break
        before = min_id
        time.sleep(pause)
    save(store)
    print(f"История: добавлено {added}, обновлено просмотров {updated}, "
          f"всего постов {len(store)}")
    return added, updated


def refresh(last=40):
    """Освежить просмотры у свежих постов (одна-две страницы витрины)."""
    pages = max(1, round(last / 13) + 1)   # на странице витрины около 13 постов
    return collect(pages=pages)


# ---------- сводка ----------

def report(month=None):
    """Что стоит в канале и сколько просмотров. Вызывается и из postlog --stats."""
    store = load()
    recs = [r for r in store.values()
            if not month or str(r.get("date") or "").startswith(month)]
    if not recs:
        print("История канала пуста - запустите: py agents/channel_history.py --backfill")
        return

    # продолжения длинного поста и отдельные фото - части, а не самостоятельные посты
    main = [r for r in recs if r.get("kind") not in ("part", "photo")]
    views = [r.get("views") for r in main if isinstance(r.get("views"), int)]
    print(f"\nВ канале: {len(recs)} сообщений, из них самостоятельных постов {len(main)}.")
    if views:
        print(f"Просмотры: всего {sum(views)}, в среднем на пост "
              f"{sum(views) / len(views):.1f}, лучший {max(views)}.")

    by_kind = Counter(r.get("kind") or "?" for r in main)
    print("\nПостов по рубрикам:")
    for k, n in by_kind.most_common():
        vs = [r.get("views") for r in main
              if r.get("kind") == k and isinstance(r.get("views"), int)]
        avg = f", просмотров в среднем {sum(vs) / len(vs):.1f}" if vs else ""
        print(f"  {n:4d}  {KIND_NAMES.get(k, k)}{avg}")

    by_month = defaultdict(int)
    for r in main:
        if r.get("date"):
            by_month[r["date"][:7]] += 1
    if len(by_month) > 1:
        print("\nПостов по месяцам:")
        for m in sorted(by_month):
            print(f"  {m}: {by_month[m]}")

    days = {r.get("date") for r in main if r.get("date")}
    if days:
        ds = sorted(days)
        print(f"\nДней с публикациями: {len(ds)} (с {ds[0]} по {ds[-1]}).")
        miss = []
        a, b = dt.date.fromisoformat(ds[0]), dt.date.fromisoformat(ds[-1])
        while a <= b:
            if a.isoformat() not in days:
                miss.append(a.isoformat())
            a += dt.timedelta(days=1)
        print(f"Дней без публикаций внутри периода: {len(miss)}")
        if miss:
            print("  " + ", ".join(miss))

    top = sorted((r for r in main if isinstance(r.get("views"), int)),
                 key=lambda r: -r["views"])[:5]
    if top:
        print("\nСамые просматриваемые посты:")
        for r in top:
            print(f"  {r.get('views'):5d}  {r.get('date')}  "
                  f"{KIND_NAMES.get(r.get('kind'), r.get('kind'))}: "
                  f"{(r.get('text_head') or '')[:70]}")
            print(f"         {r.get('url')}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from llm import _load_dotenv
    _load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--backfill", action="store_true", help="пройти всю историю канала")
    ap.add_argument("--refresh", type=int, metavar="N",
                    help="освежить просмотры последних N постов")
    ap.add_argument("--stats", action="store_true", help="сводка по каналу")
    ap.add_argument("--month", help="ГГГГ-ММ для сводки")
    ap.add_argument("--pages", type=int, default=60, help="ограничение страниц витрины")
    args = ap.parse_args()

    if args.backfill:
        collect(pages=args.pages)
        report(args.month)
        return
    if args.refresh:
        refresh(args.refresh)
        report(args.month)
        return
    report(args.month)


if __name__ == "__main__":
    main()
