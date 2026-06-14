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
import re
import sys
import json
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
    about = (e.get("about") or "").strip()
    if about:
        line += f"\n  {about}"
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
            row = f"- {title}{till} - {e['_participant']}"
            about = (e.get("about") or "").strip()
            if about:
                row += f"\n  {about}"
            lines.append(row)
        lines.append("")
    return lines


def build_post(title, start, end, sections, subtitle=None, streetart_md=None):
    head = f"{start.day} {a2.MONTHS[start.month]} - {end.day} {a2.MONTHS[end.month]}"
    tag = f"{subtitle} · {head}" if subtitle else head
    lines = [f"# {title}", "", f"_{tag}_", ""]
    total = sum(len(evs) for _, evs in sections)
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


GASTRO_WORDS = ("гастро", "кухн", "еда", "фуд", "кофе", "вино", "ужин",
                "дегустац", "ресторан", "завтрак", "напитк", "food")


def is_gastro(e):
    if e.get("type") == "гастро" or "гастроном" in (e.get("_category") or ""):
        return True
    text = (e.get("title", "") + " " + (e.get("about") or "")).lower()
    return any(w in text for w in GASTRO_WORDS)


def relevance(e):
    r = e.get("relevance")
    return r if isinstance(r, (int, float)) else 2


def rank_and_cap(events, limit):
    """Сначала самые релевантные теме, затем по дате. Не больше limit штук."""
    ranked = sorted(events, key=lambda e: (-relevance(e),
                    a2._date(e.get("date_start")) or dt.date.max))
    return ranked[:limit]


HEADLINE_SYSTEM = (
    "Ты редактор культурной афиши Петербурга. Придумываешь короткий образный "
    "заголовок к подборке событий. Главное: бери яркие слова и образы прямо из "
    "названий событий (например 'хаос', 'неопределённость', 'любовь', 'пустые "
    "вещи') и обыгрывай их в одной живой фразе - желательно через контраст или "
    "связку. Можно построить как образ, потом двоеточие, потом суть. Простой "
    "деловой русский язык, без пафоса и рекламных слов (не пиши 'праздник', "
    "'не пропустите', 'настоящий'), без жаргона и англицизмов, без буквы е с "
    "точками и без длинного тире. Одна строка, не длиннее 10 слов. Только сам "
    "заголовок, без кавычек и пояснений."
)


def make_headline(events, theme):
    if not events:
        return None
    titles = "; ".join(e["title"] for e in events[:12])
    hint = theme.get("intro_hint", "")
    user = (f"Тема дня: {theme.get('title', '')}. {hint}.\n"
            f"Названия событий: {titles}.\n"
            "Придумай образный заголовок, обыграв образы из самих названий.")
    try:
        h = llm.chat(HEADLINE_SYSTEM, user, temperature=0.6, max_tokens=80)
        return h.strip().strip('"').splitlines()[0].strip().rstrip(" .,;")
    except Exception:
        return None


def headline_and_subtitle(events, theme, use_llm):
    """Заголовок-образ + подзаголовок с темой. Если модели нет - тема как заголовок."""
    if use_llm and events and llm.available():
        h = make_headline(events, theme)
        if h:
            return h, theme.get("title")
    return theme.get("title", "Афиша НКГ"), None


# ---------- режимы ----------

def theme_window(theme, target):
    """Окно дат под тему: неделя вперёд, ближайшие выходные или следующая неделя."""
    if theme.get("mode") == "weekly_all":
        return next_week(target)
    if theme.get("weekend_only"):
        return upcoming_weekend(target)
    return target, target + dt.timedelta(days=6)


def limit_for(theme):
    meta = load_themes().get("meta", {})
    if theme.get("mode") == "weekly_all":
        return meta.get("limit_weekly", 20)
    return meta.get("limit_daily", 10)


def collect_for_theme(theme, target, use_llm):
    """Собрать и отранжировать события темы (без обрезки лимитом). -> (events, start, end)."""
    base = a2.load_base()
    participants = select_participants(base, theme.get("categories") or [])
    cat_by_name = {p["name"]: p.get("category", "") for p in participants}
    start, end = theme_window(theme, target)
    print(f"Площадок по теме: {len(participants)}, окно {start} - {end}")
    if use_llm and not llm.available():
        print("Ключ модели не найден - событий не собрать.")
        use_llm = False
    events = a2.collect(participants, start, end, use_llm=use_llm) if use_llm else []
    for e in events:
        e["_category"] = cat_by_name.get(e["_participant"], "")
    events = filter_types(events, theme.get("event_types") or [])
    minrel = theme.get("min_relevance", 0)
    if minrel:
        events = [e for e in events if relevance(e) >= minrel]
    events = sorted(events, key=lambda e: (-relevance(e),
                    a2._date(e.get("date_start")) or dt.date.max))
    print(f"После отбора по теме: событий {len(events)}")
    return events, start, end


def build_events_post(theme, ranked, start, end, selection, use_llm, today, save_seen=False):
    """Собрать готовый пост: выбранные владельцем события или авто-топ по лимиту."""
    if selection:
        chosen = [ranked[i - 1] for i in selection if 1 <= i <= len(ranked)]
    else:
        chosen = ranked[:limit_for(theme)]
    print(f"В пост войдёт событий: {len(chosen)}"
          + (" (по выбору владельца)" if selection else " (авто-топ)"))

    # копим персон недели (для субботы) - из того, что реально вошло в пост
    try:
        import agent4_person
        agent4_person.record_events(chosen, today)
    except Exception as ex:
        print(f"  (персоны не записаны: {str(ex)[:80]})")

    if theme.get("gastronomy"):
        gastro = [e for e in chosen if is_gastro(e)]
        rest = [e for e in chosen if not is_gastro(e)]
        sections = [(None, rest), ("Гастрономия и где поесть", gastro)]
    else:
        sections = [(None, chosen)]

    title, subtitle = headline_and_subtitle(chosen, theme, use_llm)
    streetart_md = None
    if theme.get("include_streetart"):
        streetart_md = collect_streetart(use_llm, save_seen=save_seen)
    return build_post(title, start, end, sections,
                      subtitle=subtitle, streetart_md=streetart_md)


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


PREVIEW_PATH = os.path.join(ROOT, "data", "preview.json")
PREVIEW_MAX = 25  # сколько событий показываем владельцу на выбор


def _preview_message(theme, ranked, start, end, limit):
    head = f"{start.day} {a2.MONTHS[start.month]} - {end.day} {a2.MONTHS[end.month]}"
    lines = [f"<b>Предпросмотр: {theme.get('title','')}</b>", f"{head}", ""]
    for i, e in enumerate(ranked, 1):
        d = a2._date(e.get("date_start"))
        when = f"{d.day}.{d.month:02d}" if d else "?"
        about = (e.get("about") or "").strip()
        lines.append(f"{i}. [{when}] {e['title']} - {e.get('_participant','')}"
                     + (f" - {about}" if about else ""))
    lines += ["", f"Ответьте номерами через запятую, что оставить (например 1,3,5). "
              f"Не ответите до полуночи - утром выйдет авто-топ {limit} по релевантности."]
    return "\n".join(lines)


def run_preview(theme, day_key, target, use_llm, send):
    """Накануне: собрать список и прислать владельцу на выбор (или предложить персону)."""
    if theme.get("mode") == "person":
        import agent4_person
        agent4_person.propose(dt.date.today(), send=send)
        return
    ranked, start, end = collect_for_theme(theme, target, use_llm)
    shown = ranked[:PREVIEW_MAX]
    data = {"target": target.isoformat(), "day_key": day_key,
            "start": start.isoformat(), "end": end.isoformat(),
            "events": shown, "since": 0}
    if send and shown:
        msg = _preview_message(theme, shown, start, end, limit_for(theme))
        sent = _dm_owner(msg)
        if sent:
            try:
                import notify_telegram
                data["since"] = notify_telegram.current_update_id() or 0
            except Exception:
                pass
            print(f"Предпросмотр отправлен владельцу ({len(shown)} событий).")
        else:
            print("TELEGRAM_OWNER_CHAT_ID не задан - предпросмотр не ушёл.")
    with open(PREVIEW_PATH, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)
    print(f"Список сохранён: {PREVIEW_PATH}")


def _dm_owner(md):
    try:
        import notify_telegram as nt
        if not nt.owner_chat():
            return False
        for part in nt.split_chunks(nt.md_to_tg(md)):
            nt.send_text(part, chat=nt.owner_chat())
        return True
    except Exception as e:
        print(f"  (личное сообщение не ушло: {str(e)[:80]})")
        return False


def _parse_selection(text, n):
    nums = [int(x) for x in re.findall(r"\d+", text or "")]
    sel = [x for x in nums if 1 <= x <= n]
    return sel or None


def _load_preview(today, day_key):
    data = _load_json(PREVIEW_PATH)
    if not data:
        return None
    if data.get("target") != today.isoformat() or data.get("day_key") != day_key:
        return None
    return data


def _load_json(path):
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return None


def capture_preferences(since):
    """Записать новые правила из текстовых ответов владельца (не номеров)."""
    try:
        import notify_telegram as nt
        import prefs
        added = []
        for text in nt.owner_messages_since(since):
            if prefs.looks_like_preference(text) and prefs.add_preference(text):
                added.append(text.strip())
        if added:
            print("Запомнил предпочтения: " + "; ".join(added))
            try:
                nt.send_text("Запомнил на будущее: " + "; ".join(added),
                             chat=nt.owner_chat())
            except Exception:
                pass
        return added
    except Exception as e:
        print(f"  (предпочтения не записаны: {str(e)[:80]})")
        return []


def _owner_selection(since, n):
    """Прочитать ответ владельца (номера) после отправки предпросмотра."""
    try:
        import notify_telegram as nt
        for text in reversed(nt.owner_messages_since(since)):
            sel = _parse_selection(text, n)
            if sel:
                return sel
    except Exception:
        pass
    return None


def run_publish(theme, day_key, today, use_llm, send):
    """Утро дня публикации: взять список из предпросмотра (или собрать заново) и опубликовать."""
    preview = _load_preview(today, day_key)
    if preview:
        ranked = preview["events"]
        start = dt.date.fromisoformat(preview["start"])
        end = dt.date.fromisoformat(preview["end"])
        since = preview.get("since", 0)
        selection = _owner_selection(since, len(ranked))
        capture_preferences(since)  # запомнить правила из ответа владельца
        print("Использую список из предпросмотра."
              + (f" Ваш выбор: {selection}" if selection else " Ответа нет - авто-топ."))
    else:
        ranked, start, end = collect_for_theme(theme, today, use_llm)
        selection = None
        print("Предпросмотра нет - собрал заново, авто-топ.")
    md = build_events_post(theme, ranked, start, end, selection,
                           use_llm, today, save_seen=send)
    return md


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
         "date_end": "2026-06-30", "_participant": "Музей стрит-арта", "relevance": 5,
         "about": "Большая выставка про историю петербургского уличного искусства.",
         "_url": "https://streetartmuseum.ru/category/meropriyatiya"},
        {"title": "Новая графика", "type": "выставка", "date_start": "2026-06-16",
         "time": "12:00", "_participant": "MYTH Gallery", "relevance": 5,
         "about": "Молодые художники показывают эксперименты с печатной графикой.",
         "_url": "https://mythgallery.art/exibitions"},
    ]
    md = build_post("Город как холст: стрит-арт и новая графика недели",
                    dt.date(2026, 6, 15), dt.date(2026, 6, 21),
                    [(None, sample)],
                    subtitle="Понедельник: стрит-арт, выставки и галереи",
                    streetart_md="## Новое на стенах города\n\n- Новый мурал в Купчино "
                                 "- [ЛЕНСТРИТ](https://t.me/lenstreet/1)")
    print("\n" + md)


# ---------- запуск ----------

def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--day", choices=WEEKDAY_KEYS, help="какой день (по умолчанию сегодня)")
    ap.add_argument("--preview", action="store_true",
                    help="режим предпросмотра: собрать список ЗАВТРАШНЕГО дня и прислать владельцу")
    ap.add_argument("--no-llm", action="store_true", help="без модели (мало что соберётся)")
    ap.add_argument("--self-test", action="store_true", help="показать график и формат")
    ap.add_argument("--send", action="store_true", help="отправить (пост в канал или список владельцу)")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    themes = load_themes()
    today = dt.date.today()
    use_llm = not args.no_llm

    # ----- режим предпросмотра: готовим ЗАВТРАШНИЙ день -----
    if args.preview:
        target = today + dt.timedelta(days=1)
        day_key = args.day or WEEKDAY_KEYS[target.weekday()]
        theme = theme_for(day_key, themes)
        if not theme:
            print(f"На {day_key} тема не задана.")
            return
        print(f"Предпросмотр на {day_key} ({target}), тема: {theme.get('title')}")
        run_preview(theme, day_key, target, use_llm, send=args.send)
        return

    # ----- режим публикации: сегодняшний день -----
    day_key = args.day or WEEKDAY_KEYS[today.weekday()]
    theme = theme_for(day_key, themes)
    if not theme:
        print(f"На {day_key} тема не задана в themes.yaml.")
        return
    mode = theme.get("mode", "events")
    print(f"День: {day_key}, режим: {mode}, тема: {theme.get('title')}")

    if mode == "person":
        import agent4_person
        agent4_person.publish(use_llm=use_llm, send=args.send)
        return

    md = run_publish(theme, day_key, today, use_llm, send=args.send)

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
