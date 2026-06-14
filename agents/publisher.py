# -*- coding: utf-8 -*-
"""
Публикатор - тематические посты по дням недели.

Смотрит в data/themes.yaml, какая тема у сегодняшнего дня, собирает пост
только по ней и (по флагу) отправляет в Telegram.

Дни (по умолчанию):
  Пн - стрит-арт, выставки, галереи      Чт - маркеты, коллаборации, события
  Вт - театры и кино                      Пт - выходные и гастрономия (+ выбор персоны)
  Ср - лекции и мастер-классы             Сб - персона недели
                                          Вс - анонс следующей недели

Запуск (Windows - py):
  py agents/publisher.py --self-test          # показать, что и когда публикуется
  py agents/publisher.py --day monday --no-llm # собрать конкретный день без модели
  py agents/publisher.py                        # сегодняшний день, с моделью
  py agents/publisher.py --send                 # собрать и отправить в канал
"""

import os
import sys
import argparse
import datetime as dt

from ruamel.yaml import YAML

import agent2_digest as a2
import llm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
THEMES_PATH = os.path.join(ROOT, "data", "themes.yaml")
DIGEST_DIR = os.path.join(ROOT, "digests")

WEEKDAY_KEYS = ["monday", "tuesday", "wednesday", "thursday",
                "friday", "saturday", "sunday"]


# ---------- конфиг ----------

def load_themes():
    y = YAML(typ="safe")
    with open(THEMES_PATH, encoding="utf-8") as fh:
        return y.load(fh)


def theme_for(day_key, themes):
    return (themes.get("days") or {}).get(day_key, {})


# ---------- окна дат ----------

def upcoming_weekend(today):
    """Ближайшие суббота и воскресенье (включая сегодня, если уже выходные)."""
    days_to_sat = (5 - today.weekday()) % 7
    sat = today + dt.timedelta(days=days_to_sat)
    return sat, sat + dt.timedelta(days=1)


def next_week(today):
    """Следующая неделя: понедельник - воскресенье."""
    days_to_mon = (7 - today.weekday()) % 7 or 7
    mon = today + dt.timedelta(days=days_to_mon)
    return mon, mon + dt.timedelta(days=6)


# ---------- сборка текста ----------

def event_line(e):
    t = (e.get("time") or "").strip()
    prefix = f"{t} - " if t else ""
    title = e["title"]
    url = e.get("_url")
    title_md = f"[{title}]({url})" if url else title
    place = e.get("place") or e.get("_participant")
    line = f"- {prefix}{title_md} - {e['_participant']}"
    if place and place != e["_participant"]:
        line += f" ({place})"
    return line


def render_events(events, start, end):
    """Список событий по дням + раздел 'идут и продолжаются'. Со ссылками."""
    lines = []
    ongoing = [e for e in events
               if a2._date(e.get("date_end"))
               and (a2._date(e.get("date_start")) or end) < start]
    dated = [e for e in events if e not in ongoing]

    if dated:
        dated.sort(key=lambda e: (a2._date(e["date_start"]) or end, e.get("time") or ""))
        cur = None
        for e in dated:
            d = a2._date(e["date_start"])
            if d != cur:
                if cur is not None:
                    lines.append("")
                cur = d
                lines.append(f"**{a2.ru_date(d)}**")
            lines.append(event_line(e))
        lines.append("")

    if ongoing:
        lines += ["## Идут и продолжаются", ""]
        ongoing.sort(key=lambda e: a2._date(e.get("date_end")) or end)
        for e in ongoing:
            de = a2._date(e.get("date_end"))
            till = f" (до {de.day} {a2.MONTHS[de.month]})" if de else ""
            url = e.get("_url")
            title = f"[{e['title']}]({url})" if url else e["title"]
            lines.append(f"- {title}{till} - {e['_participant']}")
        lines.append("")
    return lines


def build_post(title, start, end, sections, intro=None, streetart_md=None):
    head = f"{start.day} {a2.MONTHS[start.month]} - {end.day} {a2.MONTHS[end.month]}"
    lines = [f"# {title}", "", f"_{head}_", ""]
    total = sum(len(evs) for _, evs in sections)
    if intro:
        lines += [intro.strip(), ""]
    if total == 0 and not streetart_md:
        lines += ["По этой теме на ближайшие дни событий не нашлось.", ""]
        return "\n".join(lines)
    for subtitle, evs in sections:
        if not evs:
            continue
        if subtitle:
            lines += [f"## {subtitle}", ""]
        lines += render_events(evs, start, end)
    if streetart_md:
        lines += [streetart_md.strip(), ""]
    return "\n".join(lines)


# ---------- отбор событий ----------

def select_participants(base, categories):
    ps = [p for p in base["participants"] if p.get("status") != "dead"]
    if categories:
        cats = set(categories)
        ps = [p for p in ps if p.get("category") in cats]
    return ps


def filter_types(events, types):
    if not types:
        return events
    keep = set(types)
    return [e for e in events if (e.get("type") or "другое") in keep]


def is_gastro(e):
    return e.get("type") == "гастро" or "гастроном" in (e.get("_category") or "")


# ---------- режимы ----------

def run_events(theme, today, use_llm, save_seen=False):
    """Тематический день: собрать события по категориям и видам."""
    base = a2.load_base()
    categories = theme.get("categories") or []
    participants = select_participants(base, categories)
    # запомним категорию у каждого участника - пригодится для гастро-раздела
    cat_by_name = {p["name"]: p.get("category", "") for p in participants}

    if theme.get("weekend_only"):
        start, end = upcoming_weekend(today)
    else:
        start, end = today, today + dt.timedelta(days=6)

    print(f"Площадок по теме: {len(participants)}, окно {start} - {end}")
    if use_llm and not llm.available():
        print("Ключ модели не найден - событий по тексту не собрать. Запустите с ключом.")
        use_llm = False

    events = a2.collect(participants, start, end, use_llm=use_llm) if use_llm else []
    for e in events:
        e["_category"] = cat_by_name.get(e["_participant"], "")
    events = filter_types(events, theme.get("event_types") or [])

    # копим персон недели (для субботы)
    try:
        import agent4_person
        persons = [name for e in events for name in (e.get("persons") or [])]
        agent4_person.add_persons(persons, today)
    except Exception as ex:
        print(f"  (персоны не записаны: {str(ex)[:80]})")

    # секции
    if theme.get("gastronomy"):
        gastro = [e for e in events if is_gastro(e)]
        rest = [e for e in events if not is_gastro(e)]
        sections = [(None, rest), ("Гастрономия и где поесть", gastro)]
    else:
        sections = [(None, events)]

    intro = a2.maybe_intro(events, start, end, "daily") if (use_llm and events) else None

    streetart_md = None
    if theme.get("include_streetart"):
        streetart_md = collect_streetart(use_llm, save_seen=save_seen)

    return build_post(theme.get("title", "Афиша НКГ"), start, end,
                      sections, intro=intro, streetart_md=streetart_md)


def collect_streetart(use_llm, save_seen=False):
    """Подтянуть блок стрит-арт радара. При save_seen помечает показанное в память."""
    try:
        import agent3_streetart as a3
        sources = a3.load_sources()
        cands = a3.gather(sources, days=7)
        seen = a3.load_seen()
        fresh = [c for c in cands if c["url"] not in seen]
        items = []
        today_iso = dt.date.today().isoformat()
        if use_llm and llm.available():
            for c in fresh[:8]:
                v = a3.judge(c)
                seen[c["url"]] = today_iso
                if v and v.get("relevant"):
                    items.append({**c, "summary": v.get("summary"), "place": v.get("place")})
        else:
            for c in fresh[:8]:
                seen[c["url"]] = today_iso
                items.append(c)
        if save_seen:
            a3.save_seen(seen)
        if not items:
            return None
        lines = ["## Новое на стенах города", ""]
        for it in items:
            summ = it.get("summary") or a3.first_sentence(it["text"])
            place = it.get("place")
            line = f"- {summ}"
            if place and place.lower() not in summ.lower():
                line += f" ({place})"
            line += f" - [{it['_channel']}]({it['url']})"
            lines.append(line)
        return "\n".join(lines)
    except Exception as ex:
        print(f"  (стрит-арт блок пропущен: {str(ex)[:80]})")
        return None


def run_weekly_all(theme, today, use_llm):
    """Воскресенье: вся следующая неделя по всем направлениям."""
    base = a2.load_base()
    participants = select_participants(base, [])
    start, end = next_week(today)
    print(f"Все площадки: {len(participants)}, следующая неделя {start} - {end}")
    if use_llm and not llm.available():
        use_llm = False
    events = a2.collect(participants, start, end, use_llm=use_llm) if use_llm else []
    intro = a2.maybe_intro(events, start, end, "weekly") if (use_llm and events) else None
    return build_post(theme.get("title", "Следующая неделя"), start, end,
                      [(None, events)], intro=intro)


# ---------- самопроверка ----------

def self_test():
    themes = load_themes()
    print("Тематический график публикаций:\n")
    today = dt.date(2026, 6, 15)  # понедельник
    for i, key in enumerate(WEEKDAY_KEYS):
        d = today + dt.timedelta(days=i)
        t = theme_for(key, themes)
        mode = t.get("mode", "events")
        print(f"  {a2.WEEKDAYS[d.weekday()]} {d.day:02d}.{d.month:02d}  "
              f"[{mode}]  {t.get('title', '(нет темы)')}")
    print("\nПример поста (понедельник, без сети) - формат со ссылками:")
    sample = [
        {"title": "Город как холст", "type": "выставка", "date_start": "2026-05-20",
         "date_end": "2026-06-30", "_participant": "Музей стрит-арта",
         "_url": "https://streetartmuseum.ru/category/meropriyatiya"},
        {"title": "Новая графика", "type": "выставка", "date_start": "2026-06-16",
         "time": "12:00", "_participant": "MYTH Gallery",
         "_url": "https://mythgallery.art/exibitions"},
    ]
    md = build_post("Понедельник: стрит-арт, выставки и галереи",
                    dt.date(2026, 6, 15), dt.date(2026, 6, 21),
                    [(None, sample)],
                    streetart_md="## Новое на стенах города\n\n- Новый мурал в Купчино "
                                 "- [ЛЕНСТРИТ](https://t.me/lenstreet/1)")
    print("\n" + md)


# ---------- запуск ----------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", choices=WEEKDAY_KEYS, help="какой день собрать (по умолчанию сегодня)")
    ap.add_argument("--no-llm", action="store_true", help="без модели (мало что соберётся)")
    ap.add_argument("--self-test", action="store_true", help="показать график и формат")
    ap.add_argument("--send", action="store_true", help="отправить пост в Telegram")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    themes = load_themes()
    today = dt.date.today()
    day_key = args.day or WEEKDAY_KEYS[today.weekday()]
    theme = theme_for(day_key, themes)
    if not theme:
        print(f"На {day_key} тема не задана в themes.yaml.")
        return
    use_llm = not args.no_llm
    mode = theme.get("mode", "events")
    print(f"День: {day_key}, режим: {mode}, тема: {theme.get('title')}")

    if mode == "person":
        import agent4_person
        agent4_person.publish(use_llm=use_llm, send=args.send)
        return

    if mode == "weekly_all":
        md = run_weekly_all(theme, today, use_llm)
    else:
        md = run_events(theme, today, use_llm, save_seen=args.send)
        # в пятницу заодно предлагаем владельцу персону недели
        if day_key == "friday":
            try:
                import agent4_person
                agent4_person.propose(today, send=args.send)
            except Exception as ex:
                print(f"  (предложение персоны не отправлено: {str(ex)[:80]})")

    os.makedirs(DIGEST_DIR, exist_ok=True)
    out = os.path.join(DIGEST_DIR, f"{today.isoformat()}-{day_key}.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    print(f"Готово: {out}")

    if args.send:
        try:
            import notify_telegram
            notify_telegram.send_markdown(md)
        except SystemExit as e:
            print(f"Отправка не удалась: {e}")
        except Exception as e:
            print(f"Отправка не удалась: {str(e)[:160]}")


if __name__ == "__main__":
    main()
