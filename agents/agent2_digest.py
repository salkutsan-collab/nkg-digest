# -*- coding: utf-8 -*-
"""
Агент 2 - "Дайджест событий НКГ".

Что делает:
  1. Берет участников из базы (data/participants.yaml).
  2. Заходит на их страницы афиши, забирает текст.
  3. С помощью модели (GigaChat) достает из текста события на ближайшую неделю:
     название, дата, время, место.
  4. Собирает аккуратный дайджест и кладет его в файл digests/.
     (Отправка в Telegram - следующий шаг.)

Запуск (Windows - py):
  py agents/agent2_digest.py --self-test     # показать формат на примере, без ключа и сети
  py agents/agent2_digest.py --no-llm        # дайджест со ссылками на афиши, без модели
  py agents/agent2_digest.py                  # полный сбор моделью (нужен ключ GigaChat)
  py agents/agent2_digest.py --mode weekly    # более широкий обзор недели
"""

import os
import sys
import re
import json
import argparse
import datetime as dt
import concurrent.futures as cf

import requests
from bs4 import BeautifulSoup
from ruamel.yaml import YAML

import llm
import prefs

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_PATH = os.path.join(ROOT, "data", "participants.yaml")
DIGEST_DIR = os.path.join(ROOT, "digests")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NKG-Digest/1.0)"}

MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]
WEEKDAYS = ["Пн", "Вт", "Ср", "Чт", "Пт", "Сб", "Вс"]


# ---------- работа с базой ----------

def load_base():
    y = YAML(typ="safe")
    with open(BASE_PATH, encoding="utf-8") as fh:
        return y.load(fh)


def pick_source(p):
    """Откуда забирать события: сначала страница афиши, потом сайт, потом соцсеть."""
    if p.get("events_url"):
        return p["events_url"]
    web = [s["url"] for s in p.get("sources", []) if s.get("type") == "website"]
    if web:
        return web[0]
    other = [s["url"] for s in p.get("sources", []) if s.get("url")]
    return other[0] if other else None


# ---------- загрузка и чистка страницы ----------

def fetch_text(url, limit=7000):
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code >= 400 or "text/html" not in r.headers.get("Content-Type", ""):
            return ""
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        text = soup.get_text("\n", strip=True)
        text = re.sub(r"\n{2,}", "\n", text)
        return text[:limit]
    except Exception:
        return ""


# ---------- извлечение событий моделью ----------

EXTRACT_SYSTEM = (
    "Ты помощник, который вытаскивает афишу культурных событий из текста сайта. "
    "Отвечай ТОЛЬКО валидным JSON без пояснений."
)


def extract_events(name, page_text, start, end):
    """Спросить модель: какие события идут/начинаются в окне [start, end]."""
    if not page_text.strip():
        return []
    prompt = (
        f"Организация: {name}.\n"
        f"Сегодня {start.isoformat()}. Нужны события в период с {start.isoformat()} "
        f"по {end.isoformat()} включительно (выставки считаются, если идут в этот период).\n\n"
        "Из текста ниже найди реальные мероприятия и верни JSON-массив объектов с полями:\n"
        '  "title"      - название,\n'
        '  "type"       - вид: выставка | лекция | мастер-класс | концерт | спектакль | '
        "кино | маркет | экскурсия | гастро | фестиваль | другое,\n"
        '  "date_start" - дата начала в формате YYYY-MM-DD (если год не указан, считай ближайший),\n'
        '  "date_end"   - дата окончания YYYY-MM-DD или null,\n'
        '  "time"       - время "ЧЧ:ММ" или null,\n'
        '  "place"      - место/адрес или null,\n'
        '  "persons"    - массив имен людей, связанных с событием (художник, куратор, '
        "лектор, режиссер, спикер); если никого нет - пустой массив,\n"
        '  "about"      - одно короткое предложение по делу, о чем событие '
        "(без рекламных слов и пафоса), или null,\n"
        '  "relevance"  - целое от 0 до 5: насколько событие связано именно с '
        "дизайном, визуальным искусством, художниками, архитектурой "
        "(5 - напрямую про это; 3 - частично; 0 - не связано, например обычный "
        "кассовый фильм или спектакль без отношения к искусству и дизайну).\n"
        + (prefs.as_prompt() + "\n" if prefs.as_prompt() else "")
        + "Бери только то, у чего понятна дата и что попадает в окно. "
        "Если ничего нет - верни [].\n\n"
        f"ТЕКСТ СТРАНИЦЫ:\n{page_text}"
    )
    try:
        raw = llm.chat(EXTRACT_SYSTEM, prompt, temperature=0.1, max_tokens=1500)
    except Exception as e:
        print(f"  [{name}] ошибка модели: {str(e)[:120]}")
        return []
    return _parse_json_events(raw)


def _parse_json_events(raw):
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    out = []
    if isinstance(data, list):
        for it in data:
            if isinstance(it, dict) and it.get("title") and it.get("date_start"):
                out.append(it)
    return out


# ---------- фильтр по неделе ----------

def _date(s):
    try:
        return dt.date.fromisoformat(str(s)[:10])
    except Exception:
        return None


def in_window(ev, start, end):
    ds = _date(ev.get("date_start"))
    if not ds:
        return False
    de = _date(ev.get("date_end"))
    if de:
        return ds <= end and de >= start
    return start <= ds <= end


# ---------- сбор ----------

def collect(participants, start, end, use_llm, workers=6):
    events = []

    def work(p):
        url = pick_source(p)
        if not url:
            return p, []
        text = fetch_text(url)
        if not use_llm:
            return p, []
        evs = extract_events(p["name"], text, start, end)
        return p, evs

    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for p, evs in ex.map(work, participants):
            kept = [e for e in evs if in_window(e, start, end)]
            for e in kept:
                e["_participant"] = p["name"]
                e["_address"] = p.get("address", "")
                e["_url"] = p.get("events_url") or pick_source(p)
            print(f"  {p['name']}: событий {len(kept)}")
            events.extend(kept)
    return events


# ---------- сборка текста дайджеста ----------

def ru_date(d):
    return f"{WEEKDAYS[d.weekday()]}, {d.day} {MONTHS[d.month]}"


def build_markdown(events, start, end, mode, intro=None):
    title = "Афиша НКГ: что на неделе" if mode == "daily" else "Обзор недели НКГ"
    head = f"{start.day} {MONTHS[start.month]} - {end.day} {MONTHS[end.month]}"
    lines = [f"# {title}", "", f"_{head}_", ""]

    if intro:
        lines += [intro.strip(), ""]
    else:
        places = len({e["_participant"] for e in events})
        lines += [f"Событий: {len(events)}, площадок: {places}.", ""]

    if not events:
        lines += ["На этой неделе событий не нашлось. Проверьте источники в базе.", ""]
        return "\n".join(lines)

    # "идущие" - то, что началось раньше окна, но еще продолжается (выставки, программы)
    ongoing = [e for e in events
               if _date(e.get("date_end")) and (_date(e.get("date_start")) or end) < start]
    dated = [e for e in events if e not in ongoing]

    if dated:
        lines += ["## События по дням", ""]
        dated.sort(key=lambda e: (_date(e["date_start"]) or end, e.get("time") or ""))
        cur = None
        for e in dated:
            d = _date(e["date_start"])
            if d != cur:
                if cur is not None:
                    lines.append("")
                cur = d
                lines.append(f"**{ru_date(d)}**")
            t = (e.get("time") or "").strip()
            place = e.get("place") or e.get("_participant")
            prefix = f"{t} - " if t else ""
            line = f"- {prefix}{e['title']} - {e['_participant']}"
            if place and place != e["_participant"]:
                line += f" ({place})"
            lines.append(line)
        lines.append("")

    if ongoing:
        lines += ["## Идут и продолжаются", ""]
        ongoing.sort(key=lambda e: _date(e.get("date_end")) or end)
        for e in ongoing:
            de = _date(e.get("date_end"))
            till = f" (до {de.day} {MONTHS[de.month]})" if de else ""
            lines.append(f"- {e['title']}{till} - {e['_participant']}")
        lines.append("")

    return "\n".join(lines)


def build_links_digest(participants, start, end):
    """Запасной дайджест без модели: список площадок и ссылки на их афиши."""
    head = f"{start.day} {MONTHS[start.month]} - {end.day} {MONTHS[end.month]}"
    lines = [f"# Афиша НКГ: куда смотреть на неделе", "", f"_{head}_", "",
             "Список площадок и их афиш (события собираются вручную, "
             "автосбор моделью подключим следующим шагом):", ""]
    by_cat = {}
    for p in participants:
        by_cat.setdefault(p.get("category", "прочее"), []).append(p)
    for cat, items in by_cat.items():
        lines += [f"## {cat}", ""]
        for p in items:
            url = p.get("events_url") or pick_source(p)
            if url:
                lines.append(f"- [{p['name']}]({url}) - {p.get('address','')}")
            else:
                lines.append(f"- {p['name']} - {p.get('address','')}")
        lines.append("")
    return "\n".join(lines)


def maybe_intro(events, start, end, mode):
    """Короткое человеческое вступление от модели (факты не выдумываем)."""
    if not llm.available() or not events:
        return None
    titles = "; ".join(f"{e['title']} ({e['_participant']})" for e in events[:12])
    system = (
        "Ты редактор афиши. Пиши простым деловым русским языком, спокойным тоном, "
        "без рекламных оборотов и пафоса (не используй слова вроде 'праздник', "
        "'настоящий', 'не пропустите'), без жаргона и англицизмов, без буквы е с "
        "точками и без длинного тире. Ровно 1-2 предложения."
    )
    user = (
        f"Напиши короткое нейтральное вступление к дайджесту культурных событий "
        f"Петербурга на период {start.isoformat()} - {end.isoformat()}. "
        f"Можно назвать пару заметных площадок или тем. Не перечисляй все, не выдумывай. "
        f"Вот часть событий: {titles}"
    )
    try:
        return llm.chat(system, user, temperature=0.5, max_tokens=300)
    except Exception:
        return None


# ---------- самопроверка (без ключа и сети) ----------

def self_test():
    start = dt.date(2026, 6, 15)
    end = start + dt.timedelta(days=6)
    events = [
        {"title": "Лекция о современной графике", "type": "лекция",
         "date_start": "2026-06-16", "time": "19:00",
         "_participant": "Эрарта", "_address": "29-я линия В. О., 2"},
        {"title": "Маркет локального дизайна", "type": "маркет",
         "date_start": "2026-06-21", "time": "12:00",
         "_participant": "Севкабель Порт", "place": "Кожевенная линия, 40",
         "_address": "Кожевенная линия, 40"},
        {"title": "Выставка «Город как холст»", "type": "выставка",
         "date_start": "2026-05-20", "date_end": "2026-06-30",
         "_participant": "Музей стрит-арта", "_address": "шоссе Революции, 84"},
    ]
    md = build_markdown(events, start, end, "daily",
                        intro="На этой неделе - разговор о графике, маркет дизайна "
                              "и большая выставка про городские стены.")
    print(md)


# ---------- запуск ----------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "weekly"], default="daily")
    ap.add_argument("--no-llm", action="store_true", help="без модели: дайджест со ссылками")
    ap.add_argument("--self-test", action="store_true", help="показать формат на примере")
    ap.add_argument("--limit", type=int, default=0, help="ограничить число участников (отладка)")
    ap.add_argument("--send", action="store_true", help="отправить готовый дайджест в Telegram")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    base = load_base()
    participants = [p for p in base["participants"] if p.get("status") != "dead"]
    if args.limit:
        participants = participants[:args.limit]

    today = dt.date.today()
    span = 6 if args.mode == "daily" else 9
    start, end = today, today + dt.timedelta(days=span)
    os.makedirs(DIGEST_DIR, exist_ok=True)

    use_llm = not args.no_llm
    if use_llm and not llm.available():
        print("Ключ модели не найден - перехожу в режим ссылок (--no-llm).")
        print("Чтобы включить автосбор, задайте GIGACHAT_CREDENTIALS (см. .env.example).")
        use_llm = False

    if not use_llm:
        md = build_links_digest(participants, start, end)
        out = os.path.join(DIGEST_DIR, f"{today.isoformat()}-{args.mode}-links.md")
        with open(out, "w", encoding="utf-8") as fh:
            fh.write(md + "\n")
        print(f"Готово (режим ссылок): {out}")
        if args.send:
            _send(md)
        return

    print(f"Собираю события ({len(participants)} площадок, провайдер {llm.provider()})...")
    events = collect(participants, start, end, use_llm=True)
    intro = maybe_intro(events, start, end, args.mode)
    md = build_markdown(events, start, end, args.mode, intro=intro)
    out = os.path.join(DIGEST_DIR, f"{today.isoformat()}-{args.mode}.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    print(f"Готово: {out}  (событий: {len(events)})")
    if args.send:
        _send(md)


def _send(md):
    try:
        import broadcast
        broadcast.send_markdown(md)
    except SystemExit as e:
        print(f"Отправка не удалась: {e}")
    except Exception as e:
        print(f"Отправка не удалась: {str(e)[:160]}")


if __name__ == "__main__":
    main()
