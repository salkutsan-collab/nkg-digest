# -*- coding: utf-8 -*-
"""
Сборщик событий из KudaGo - городской афиши с открытым API (без ключа).

Зачем: наш основной сбор идет по сайтам площадок из базы, поэтому событие на
площадке, которой в базе нет, мы просто не видим. KudaGo дает афишу всего города
и закрывает этот пробел. Плата - мусор в выдаче (студии рисования, кафе,
торговые центры, детские квесты), поэтому берем не поток, а отобранное.

Отбор в четыре шага, все настройки в data/kudago.yaml:
  1. запрашиваем только нужные рубрики;
  2. выбрасываем площадки и события по стоп-словам;
  3. считаем релевантность нашей теме и берем от порога;
  4. ограничиваем число событий на тему.

События приводятся к тому же виду, что и у агента 2 (title, _participant, type,
date_start, date_end, _url, about, relevance), поэтому дальше работают общие
правила публикатора: отбор по типам, порог темы, черный список площадок, архив.

Запуск (Windows - py):
  py agents/kudago.py --sample                # что придет по теме сегодняшнего дня
  py agents/kudago.py --day monday            # то же для конкретного дня недели
  py agents/kudago.py --raw exhibition        # сырая выдача рубрики, для отладки
"""

import os
import re
import sys
import html
import argparse
import datetime as dt

import requests
import yaml

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(ROOT, "data", "kudago.yaml")

API = "https://kudago.com/public-api/v1.4/events/"
HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; nkg-digest/1.0)"}
FIELDS = ("id,slug,title,short_title,dates,place,categories,tags,site_url,"
          "description,price,is_free")


def load_config():
    with open(CONFIG_PATH, encoding="utf-8") as fh:
        return yaml.safe_load(fh) or {}


def _norm(s):
    s = str(s or "").lower().replace("ё", "е")
    return re.sub(r"\s+", " ", s).strip()


def _clean_html(s):
    """Описание приходит с тегами - вычищаем в обычный текст."""
    text = re.sub(r"<[^>]+>", " ", str(s or ""))
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


# ---------- запрос ----------

def fetch(category, since, until, pages=3, page_size=100):
    """Сырые события одной рубрики за окно дат. Ошибка сети не роняет прогон."""
    out = []
    for page in range(1, pages + 1):
        params = {
            "location": (load_config().get("meta") or {}).get("location", "spb"),
            "categories": category,
            "actual_since": int(dt.datetime.combine(since, dt.time.min).timestamp()),
            "actual_until": int(dt.datetime.combine(until, dt.time.max).timestamp()),
            "page": page, "page_size": page_size,
            "expand": "place", "fields": FIELDS, "order_by": "-rank",
        }
        try:
            r = requests.get(API, params=params, headers=HEADERS, timeout=40)
            if r.status_code >= 400:
                print(f"  (KudaGo {category}: код {r.status_code})")
                break
            data = r.json()
        except Exception as e:
            print(f"  (KudaGo {category}: {str(e)[:100]})")
            break
        out += data.get("results") or []
        if not data.get("next"):
            break
    return out


# ---------- отбор ----------

def _denied(raw, cfg):
    """Причина отказа или пустая строка, если событие проходит."""
    place = raw.get("place") if isinstance(raw.get("place"), dict) else {}
    pname = _norm(place.get("title"))
    if not pname:
        return "нет площадки"
    for w in cfg.get("deny_place") or []:
        if _norm(w) in pname:
            return f"площадка по стоп-слову «{w}»"
    title = _norm(raw.get("title")) + " " + _norm(raw.get("short_title"))
    for w in cfg.get("deny_title") or []:
        if _norm(w) in title:
            return f"название по стоп-слову «{w}»"
    about = _norm(_clean_html(raw.get("description")))
    for w in cfg.get("deny_about") or []:
        if _norm(w) in about:
            return f"реклама в описании («{w}»)"
    tags = {_norm(t) for t in (raw.get("tags") or [])}
    for w in cfg.get("deny_tags") or []:
        if _norm(w) in tags:
            return f"метка «{w}»"
    return ""


def match_known(place_name, known, cfg=None):
    """Сопоставить площадку KudaGo с нашей базой: сначала по вхождению названия,
    потом по списку соответствий из data/kudago.yaml (place_aliases).
    Угадывание по общим словам не используется - оно давало ложные совпадения
    («Музей истории религии» подтягивался к Музею истории Петербурга).
    Возвращает (наше название, категория) или (None, "")."""
    pn = _norm(place_name)
    if not pn:
        return None, ""
    for name, cat in known.items():
        n = _norm(name)
        if len(n) >= 5 and (n == pn or n in pn or pn in n):
            return name, cat
    aliases = (cfg or load_config()).get("place_aliases") or {}
    for name, keys in aliases.items():
        if name not in known:
            continue
        for key in keys or []:
            if _norm(key) and _norm(key) in pn:
                return name, known.get(name, "")
    return None, ""


def guess_relevance(raw, cfg, known_places):
    """Насколько событие про нашу тему: 1 (мимо) - 5 (точно наше).

    Своя площадка из базы - плюс балл: значит место мы уже отобрали руками.
    Дальше считаем слова темы в названии, описании и метках."""
    place = raw.get("place") if isinstance(raw.get("place"), dict) else {}
    text = " ".join([_norm(raw.get("title")), _norm(raw.get("short_title")),
                     _norm(_clean_html(raw.get("description"))),
                     " ".join(_norm(t) for t in (raw.get("tags") or []))])
    hits = {w for w in (cfg.get("topic_words") or []) if _norm(w) in text}
    score = 2
    if match_known(place.get("title"), known_places, cfg)[0]:
        score += 1
    if hits:
        score += 1
    if len(hits) >= 3:
        score += 1
    tags = {_norm(t) for t in (raw.get("tags") or [])}
    if "отдых и развлечения" in tags and not hits:
        score -= 1
    return max(1, min(5, score))


def _day(value):
    """Дата из строки «2026-08-26» или из метки времени. None - если не вышло.
    У бессрочных событий KudaGo ставит заведомо далекие метки - их отбрасываем."""
    if isinstance(value, str) and value:
        try:
            return dt.date.fromisoformat(value[:10])
        except Exception:
            return None
    if isinstance(value, (int, float)):
        try:
            day = dt.datetime.fromtimestamp(value).date()
            return day if 2000 <= day.year <= 2100 else None
        except Exception:
            return None
    return None


def _dates(raw, since, until):
    """Даты события в пределах окна: (начало, конец, время).
    Когда список полей урезан, KudaGo отдает не строки дат, а метки времени."""
    fits = []
    for d in raw.get("dates") or []:
        sd = _day(d.get("start_date")) or _day(d.get("start"))
        ed = _day(d.get("end_date")) or _day(d.get("end"))
        if not sd and not ed:
            continue      # дат нет вовсе (бессрочная экспозиция) - не наше
        # у идущих выставок KudaGo часто не указывает начало (ставит служебную
        # метку года первого): такое событие оставляем, публикатор покажет его
        # в разделе «Идут и продолжаются» по дате окончания
        if ed and ed < since:
            continue      # уже закончилось
        if sd and sd > until:
            continue      # еще не началось
        time = d.get("start_time") or ""
        if not time and (d.get("schedules") or []):
            time = (d["schedules"][0] or {}).get("start_time") or ""
        fits.append((sd, ed, (time or "")[:5]))
    if not fits:
        return "", "", ""
    # у события бывает много записей дат (сеансы, продления). Берем ту, что
    # начинается в нашем окне, иначе самую близкую по окончанию.
    inside = [f for f in fits if f[0] and since <= f[0] <= until]
    sd, ed, time = (min(inside, key=lambda f: f[0]) if inside
                    else min(fits, key=lambda f: f[1] or dt.date.max))
    return (sd.isoformat() if sd else "", ed.isoformat() if ed else "", time)


def to_event(raw, cfg, known_places, since, until, prefer=None):
    """Привести событие KudaGo к нашему виду. None - если не подходит.

    prefer - рубрика, из которой событие пришло: у KudaGo рубрик у события
    несколько, и без подсказки экскурсия может определиться как спектакль."""
    cats = raw.get("categories") or []
    types = cfg.get("categories") or {}
    our_type = (types.get(prefer) if prefer in cats else None)         or next((types[c] for c in cats if c in types), None)
    if not our_type:
        return None
    start, end, time = _dates(raw, since, until)
    if not start and not end:
        return None
    place = raw.get("place") if isinstance(raw.get("place"), dict) else {}
    title = (raw.get("short_title") or raw.get("title") or "").strip()
    our_name, our_cat = match_known(place.get("title"), known_places, cfg)
    rel = guess_relevance(raw, cfg, known_places)
    # выставка, открывшаяся в наше окно, интереснее той, что идет полгода
    if cfg.get("fresh_bonus", True) and start:
        try:
            if since <= dt.date.fromisoformat(start) <= until:
                rel = min(5, rel + 1)
        except Exception:
            pass
    return {
        "title": title[:1].upper() + title[1:] if title else title,
        "type": our_type,
        "date_start": start,
        "date_end": end,
        "time": time,
        "about": _clean_html(raw.get("description"))[:400],
        "relevance": rel,
        "persons": [],
        "_participant": our_name or (place.get("title") or "").strip(),
        "_category": our_cat,
        "_url": raw.get("site_url") or "",
        "_address": place.get("address") or "",
        "_source": "kudago",
        "_kudago_place": (place.get("title") or "").strip(),
    }


def collect(since, until, theme=None, known_places=None, verbose=True):
    """События KudaGo за окно дат, отобранные и приведенные к нашему виду."""
    cfg = load_config()
    # known_places: словарь {название площадки: категория} либо просто список названий
    if isinstance(known_places, dict):
        known = dict(known_places)
    else:
        known = {n: "" for n in (known_places or [])}
    want_types = set(theme.get("event_types") or []) if theme else set()
    minrel = max(int(cfg.get("min_relevance") or 0),
                 int((theme or {}).get("min_relevance") or 0))
    minrel_new = int(cfg.get("min_relevance_new_place") or minrel)
    pages = int(cfg.get("pages_per_category") or 3)

    # рубрики: только те, что дают нужные теме типы событий
    types_map = cfg.get("categories") or {}
    cats = [c for c in (cfg.get("use_categories") or [])
            if not want_types or types_map.get(c) in want_types]
    if not cats:
        if verbose:
            print("KudaGo: по типам события этой темы рубрик нет.")
        return []

    seen, events, dropped = set(), [], {}
    for cat in cats:
        for raw in fetch(cat, since, until, pages=pages):
            reason = _denied(raw, cfg)
            if reason:
                dropped[reason] = dropped.get(reason, 0) + 1
                continue
            e = to_event(raw, cfg, known, since, until, prefer=cat)
            if not e:
                reason = ("нет даты в нашем окне"
                          if _dates(raw, since, until) == ("", "", "")
                          else "тип события нам не нужен")
                dropped[reason] = dropped.get(reason, 0) + 1
                continue
            if not e["_participant"]:
                dropped["без площадки"] = dropped.get("без площадки", 0) + 1
                continue
            # для незнакомой площадки порог выше: свою мы уже отобрали руками
            own = bool(e.get("_category")) or e["_participant"] in known
            need = minrel if own else max(minrel, minrel_new)
            if e["relevance"] < need:
                key = f"релевантность ниже {need}" + ("" if own else ", площадка не наша")
                dropped[key] = dropped.get(key, 0) + 1
                continue
            key = (_norm(e["title"]), _norm(e["_participant"]))
            if key in seen:
                continue
            seen.add(key)
            events.append(e)
    events.sort(key=lambda e: (-e["relevance"], e["date_start"]))
    per_place = int(cfg.get("max_per_place") or 0)
    if per_place:
        counted, trimmed = {}, []
        for e in events:
            k = _norm(e["_participant"])
            if counted.get(k, 0) >= per_place:
                continue
            counted[k] = counted.get(k, 0) + 1
            trimmed.append(e)
        if len(trimmed) < len(events):
            dropped[f"сверх {per_place} событий с одной площадки"] = len(events) - len(trimmed)
        events = trimmed
    limit = int(cfg.get("limit_per_theme") or 25)
    if verbose:
        print(f"KudaGo: рубрики {', '.join(cats)}; взято {len(events)}"
              f"{' (обрезано до ' + str(limit) + ')' if len(events) > limit else ''}")
        for reason, n in sorted(dropped.items(), key=lambda kv: -kv[1])[:6]:
            print(f"  отброшено {n:4d}: {reason}")
    return events[:limit]


def collect_for_theme(theme, start, end, known_places=None, verbose=True):
    """Обертка под публикатор: события KudaGo по окну и типам темы."""
    return collect(start, end, theme=theme, known_places=known_places, verbose=verbose)


# ---------- запуск руками ----------

def _sample(day_key=None):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import publisher as pub
    import agent2_digest as a2
    today = dt.date.today()
    day_key = day_key or pub.WEEKDAY_KEYS[today.weekday()]
    themes = pub.load_themes()
    theme = pub.theme_for(day_key, themes)
    if not theme or theme.get("mode") == "person":
        print(f"День {day_key}: тема без сбора событий.")
        return
    start, end = pub.theme_window(theme, today)
    names = {p.get("name"): p.get("category", "")
             for p in a2.load_base().get("participants", [])}
    print(f"Тема: {theme.get('title')}\nОкно: {start} - {end}\n"
          f"Типы: {', '.join(theme.get('event_types') or ['любые'])}\n")
    events = collect_for_theme(theme, start, end, known_places=names)
    print(f"\nСобытий к добавлению: {len(events)}\n")
    for e in events:
        known = "своя площадка" if e.get("_category") or e["_participant"] in names else "новая площадка"
        when = e["date_start"] + (f" - {e['date_end']}" if e["date_end"] else "")
        print(f"  [{e['relevance']}] {e['title']}")
        print(f"      {e['_participant']} ({known}) | {e['type']} | {when}")
        print(f"      {e['_url']}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="что придет по теме сегодня")
    ap.add_argument("--day", help="день недели: monday...sunday")
    ap.add_argument("--raw", help="сырая выдача рубрики (для отладки)")
    args = ap.parse_args()

    if args.raw:
        today = dt.date.today()
        rows = fetch(args.raw, today, today + dt.timedelta(days=14), pages=1, page_size=20)
        print(f"Сырых событий: {len(rows)}")
        for r in rows[:20]:
            place = r.get("place") if isinstance(r.get("place"), dict) else {}
            print(f"  {r.get('short_title') or r.get('title')} | "
                  f"{place.get('title')} | {r.get('categories')}")
        return
    _sample(args.day)


if __name__ == "__main__":
    main()
