# -*- coding: utf-8 -*-
"""
Агент 4 - "Персона недели" (с карточкой и фото на одобрение).

Как работает:
  - Всю неделю публикатор копит, кого чаще упоминают в событиях, и какие это
    события - файл data/person_tally.json.
  - В пятницу (накануне субботы) агент собирает ЧЕРНОВИК карточки самого
    упоминаемого: портрет и работы (из Википедии/Викисклада), краткая биография
    и связанные события недели. Шлёт владельцу в личку с альтернативами.
    Владелец отвечает: ОК/1 - оставить, 2 или 3 - выбрать другого героя.
  - В субботу агент смотрит ответ, собирает финальную карточку и публикует
    в канал (альбом фото + текст). Нет ответа - берёт самого упоминаемого.

Память:
  data/person_tally.json   - счётчик упоминаний и события по неделям
  data/person_choice.json  - кандидаты, черновик карточки и выбор владельца

Запуск руками:
  py agents/agent4_person.py --show
  py agents/agent4_person.py --propose --send
  py agents/agent4_person.py --publish --send
"""

import os
import re
import sys
import json
import argparse
import datetime as dt

import llm
import images

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TALLY_PATH = os.path.join(ROOT, "data", "person_tally.json")
CHOICE_PATH = os.path.join(ROOT, "data", "person_choice.json")
DIGEST_DIR = os.path.join(ROOT, "digests")


# ---------- неделя и память ----------

def week_key(day):
    y, w, _ = day.isocalendar()
    return f"{y}-W{w:02d}"


def _load(path, default):
    if not os.path.exists(path):
        return default
    try:
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return default


def _save(path, data):
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(data, fh, ensure_ascii=False, indent=1)


def _norm(name):
    return re.sub(r"\s+", " ", str(name)).strip()


def _as_rec(v):
    """Привести запись к виду {count, events} (старый формат был просто числом)."""
    if isinstance(v, int):
        return {"count": v, "events": []}
    if isinstance(v, dict):
        return {"count": v.get("count", 0), "events": v.get("events", [])}
    return {"count": 0, "events": []}


def record_events(events, day):
    """Записать упоминания персон из событий (имя -> счётчик + сами события)."""
    tally = _load(TALLY_PATH, {})
    bucket = {k: _as_rec(v) for k, v in tally.get(week_key(day), {}).items()}
    for e in events:
        label = {"t": e.get("title"), "p": e.get("_participant"), "u": e.get("_url")}
        for nm in (e.get("persons") or []):
            name = _norm(nm)
            if len(name) < 3 or name.lower() in ("null", "none"):
                continue
            rec = bucket.setdefault(name, {"count": 0, "events": []})
            rec["count"] += 1
            if label not in rec["events"]:
                rec["events"].append(label)
    tally[week_key(day)] = bucket
    for old in sorted(tally)[:-6]:
        del tally[old]
    _save(TALLY_PATH, tally)


def top_persons(day, n=3):
    tally = _load(TALLY_PATH, {})
    bucket = {k: _as_rec(v) for k, v in tally.get(week_key(day), {}).items()}
    items = sorted(bucket.items(), key=lambda kv: -kv[1]["count"])
    return items[:n]


# ---------- биография и карточка ----------

FACTS_SYSTEM = (
    "Ты редактор культурной афиши Петербурга. Пишешь краткую справку о человеке "
    "из мира искусства и дизайна простым деловым русским языком, без пафоса и "
    "рекламных слов, без жаргона и англицизмов, без буквы е с точками и без "
    "длинного тире. Пиши только то, в чём уверен; не выдумывай даты и факты."
)


def _facts(name):
    user = (
        f"Напиши краткую справку о персоне: {name} - деятель культуры Петербурга. "
        "3-5 предложений: кто это, чем известен, какой вклад внес в современное "
        "искусство или дизайн, упомяни 2-3 значимые работы или проекта. "
        "Только проверенное, без выдумок. Без вступлений и заголовков."
    )
    try:
        return llm.chat(FACTS_SYSTEM, user, temperature=0.4, max_tokens=600).strip()
    except Exception as e:
        print(f"  модель не дала справку: {str(e)[:100]}")
        return ""


def _events_md(evlist):
    lines = []
    for ev in evlist[:6]:
        t, u, p = ev.get("t"), ev.get("u"), ev.get("p")
        if not t:
            continue
        title = f"[{t}]({u})" if u else t
        lines.append(f"- {title}" + (f" - {p}" if p else ""))
    return "\n".join(lines)


def _card_body(facts, evlist):
    parts = []
    if facts:
        parts.append(facts)
    ev = _events_md(evlist)
    if ev:
        parts.append("События недели с участием героя:\n" + ev)
    return "\n\n".join(parts) if parts else "Справку добавим позже."


def _photo_urls(imgs):
    urls = [imgs.get("portrait")] + (imgs.get("works") or [])
    return [u for u in urls if u]


# ---------- пятница: черновик карточки владельцу ----------

def propose(day, send=True):
    cands = top_persons(day, 3)
    if not cands:
        msg = "Персона недели: за неделю не набралось упоминаний. Пропускаем."
        print(msg)
        if send:
            _safe(lambda nt: nt.send_to_owner(msg))
        return None

    subject, info = cands[0]
    print(f"Кандидаты: " + ", ".join(f"{n} ({i['count']})" for n, i in cands))
    facts = _facts(subject) if llm.available() else ""
    imgs = images.find_person_images(subject)

    choice = {
        "week": week_key(day),
        "candidates": [n for n, _ in cands],
        "events": {n: i["events"] for n, i in cands},
        "subject": subject,
        "facts": facts,
        "images": imgs,
        "chosen": None,
        "since": 0,
    }

    body = _card_body(facts, info["events"])
    alts = [f"{i+1}) {n}" for i, (n, _) in enumerate(cands)]
    draft = (f"# Черновик: персона недели - {subject}\n\n{body}\n\n"
             f"_Кандидаты: {'; '.join(alts)}._\n"
             "Оставить этого героя - ответьте ОК или 1. Другой - ответьте 2 или 3. "
             "Не ответите до полуночи - в субботу выйдет этот вариант.")

    if send:
        ok = _safe(lambda nt: nt.send_to_owner("Готовлю персону недели, черновик ниже."))
        if ok:
            urls = _photo_urls(imgs)
            if urls:
                _safe(lambda nt: nt.send_photos(urls, caption=f"<b>{subject}</b>",
                                                chat=nt.owner_chat()))
            _safe(lambda nt: _send_owner_md(nt, draft))
            choice["since"] = _safe(lambda nt: nt.current_update_id()) or 0
            print("Черновик отправлен владельцу.")
        else:
            print("TELEGRAM_OWNER_CHAT_ID не задан - черновик не ушёл.")
    _save(CHOICE_PATH, choice)
    return choice


# ---------- суббота: публикация ----------

def _resolve(choice):
    """Кого публикуем и как выбран: ответ владельца (ОК/1/2/3) или авто."""
    cands = choice.get("candidates") or []
    subject = choice.get("subject") or (cands[0] if cands else None)
    since = choice.get("since", 0)
    replies = _safe(lambda nt: nt.owner_messages_since(since)) or []
    pick = None
    for t in replies:
        s = t.strip().lower()
        if s in ("ок", "ok", "да", "1"):
            pick = subject
        elif s in ("2", "3"):
            idx = int(s) - 1
            if 0 <= idx < len(cands):
                pick = cands[idx]
    if pick:
        return pick, "по вашему выбору", (pick == subject)
    return subject, "автоматически (самый упоминаемый за неделю)", True


def publish(use_llm=True, send=False):
    today = dt.date.today()
    choice = _load(CHOICE_PATH, {})
    if not choice or choice.get("week") != week_key(today):
        # черновика нет (или устарел) - соберём по горячим следам
        cands = top_persons(today, 3)
        if not cands:
            return _publish_text("# Персона недели\n\nНа этой неделе мало упоминаний.",
                                 today, send)
        subj = cands[0][0]
        choice = {"candidates": [n for n, _ in cands], "subject": subj,
                  "facts": "", "images": {}, "since": 0,
                  "events": {n: i["events"] for n, i in cands}}

    name, how, same = _resolve(choice)
    events = (choice.get("events") or {}).get(name, [])

    # если герой сменился или справки/картинок нет - добираем
    facts = choice.get("facts") if same else ""
    imgs = choice.get("images") if same else {}
    if not facts and use_llm and llm.available():
        facts = _facts(name)
    if not imgs:
        imgs = images.find_person_images(name)

    body = _card_body(facts, events)
    note = f"\n\n_Герой выбран {how}._"
    full_md = f"# Персона недели: {name}\n\n{body}{note}"

    os.makedirs(DIGEST_DIR, exist_ok=True)
    with open(os.path.join(DIGEST_DIR, f"{today.isoformat()}-person.md"),
              "w", encoding="utf-8") as fh:
        fh.write(full_md + "\n")
    print(f"Персона недели: {name} ({how})")

    choice["chosen"] = name
    _save(CHOICE_PATH, choice)

    if send:
        import broadcast
        urls = _photo_urls(imgs)
        sent = False
        if urls:
            sent = broadcast.send_photos(urls, caption=f"<b>Персона недели: {name}</b>")
        # текст: с заголовком, если фото не ушли; иначе только тело + примечание
        text_md = full_md if not sent else f"{body}{note}"
        broadcast.send_markdown(text_md)
    return full_md


def _publish_text(md, today, send):
    os.makedirs(DIGEST_DIR, exist_ok=True)
    with open(os.path.join(DIGEST_DIR, f"{today.isoformat()}-person.md"),
              "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    if send:
        import broadcast
        broadcast.send_markdown(md)
    return md


# ---------- мелочи ----------

def _send_owner_md(nt, md):
    tg = nt.md_to_tg(md)
    for part in nt.split_chunks(tg):
        nt.send_text(part, chat=nt.owner_chat())


def _safe(fn):
    try:
        import notify_telegram
        return fn(notify_telegram)
    except SystemExit as e:
        print(f"Telegram: {e}")
    except Exception as e:
        print(f"Telegram: {str(e)[:120]}")
    return None


def show():
    today = dt.date.today()
    print(f"Неделя {week_key(today)}. Топ упоминаний:")
    for name, info in top_persons(today, 10):
        print(f"  {info['count']:3d}  {name}  (событий: {len(info['events'])})")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--propose", action="store_true")
    ap.add_argument("--publish", action="store_true")
    ap.add_argument("--show", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--send", action="store_true")
    args = ap.parse_args()

    if args.show:
        show()
    elif args.propose:
        propose(dt.date.today(), send=args.send)
    elif args.publish:
        publish(use_llm=not args.no_llm, send=args.send)
    else:
        ap.print_help()


if __name__ == "__main__":
    main()
