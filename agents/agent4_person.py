# -*- coding: utf-8 -*-
"""
Агент 4 - "Персона недели".

Идея:
  - В течение недели публикатор копит, кого чаще упоминают в событиях
    (художники, кураторы, лекторы, режиссеры) - файл data/person_tally.json.
  - В пятницу агент берёт топ-3 и присылает их ВЛАДЕЛЬЦУ в личку (нужен
    TELEGRAM_OWNER_CHAT_ID). Владелец отвечает боту 1, 2 или 3.
  - В субботу агент смотрит ответ владельца, берёт выбранную персону,
    просит модель написать несколько фактов и публикует пост в канал.
    Если ответа нет - берёт самого упоминаемого и помечает это в посте.

Память:
  data/person_tally.json   - счётчик упоминаний по неделям
  data/person_choice.json  - кандидаты текущей недели и выбор владельца

Запуск (обычно вызывается публикатором, но можно и руками):
  py agents/agent4_person.py --propose         # собрать топ-3 и отправить владельцу
  py agents/agent4_person.py --publish          # опубликовать персону субботы
  py agents/agent4_person.py --show             # показать текущий счётчик
"""

import os
import re
import sys
import json
import argparse
import datetime as dt
from collections import Counter

import llm

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


def add_persons(persons, day):
    """Прибавить упоминания персон к счётчику текущей недели."""
    if not persons:
        return
    tally = _load(TALLY_PATH, {})
    wk = week_key(day)
    bucket = tally.setdefault(wk, {})
    for p in persons:
        name = _norm(p)
        if len(name) < 3 or name.lower() in ("null", "none"):
            continue
        bucket[name] = bucket.get(name, 0) + 1
    # держим только последние 6 недель
    for old in sorted(tally)[:-6]:
        del tally[old]
    _save(TALLY_PATH, tally)


def top_persons(day, n=3):
    tally = _load(TALLY_PATH, {})
    bucket = tally.get(week_key(day), {})
    return Counter(bucket).most_common(n)


# ---------- пятница: предложить владельцу ----------

def propose(day, send=True):
    cands = top_persons(day, 3)
    if not cands:
        msg = "Персона недели: за неделю не набралось упоминаний (мало данных). Пропускаем."
        print(msg)
        if send:
            _safe(lambda nt: nt.send_to_owner(msg))
        return None

    choice = {"week": week_key(day), "candidates": [c[0] for c in cands],
              "counts": {c[0]: c[1] for c in cands}, "chosen": None}
    _save(CHOICE_PATH, choice)

    lines = ["<b>Персона недели - выберите героя субботнего поста</b>", ""]
    for i, (name, cnt) in enumerate(cands, 1):
        lines.append(f"{i}) {name} - упоминаний: {cnt}")
    lines += ["", "Ответьте на это сообщение цифрой 1, 2 или 3.",
              "Если не ответите - в субботу возьму первого из списка."]
    text = "\n".join(lines)
    print("Кандидаты недели:", ", ".join(f"{n} ({c})" for n, c in cands))
    if send:
        ok = _safe(lambda nt: nt.send_to_owner(text))
        print("Отправлено владельцу в личку." if ok else
              "TELEGRAM_OWNER_CHAT_ID не задан - предложение не ушло.")
    return choice


# ---------- суббота: опубликовать ----------

FACTS_SYSTEM = (
    "Ты редактор культурной афиши Петербурга. Пишешь короткую справку о человеке "
    "из мира культуры простым деловым русским языком, без пафоса и рекламных слов, "
    "без жаргона и англицизмов, без буквы е с точками и без длинного тире. "
    "Пиши только то, в чём уверен; не выдумывай даты, награды и факты. "
    "Если о человеке известно мало - сделай справку короче."
)


def _facts(name, hint=""):
    user = (
        f"Напиши короткую справку о персоне: {name}. "
        f"Это деятель культуры, связанный с событиями Петербурга. {hint} "
        "Дай 3-4 коротких факта по существу (кто это, чем известен, чем интересен), "
        "по одному в строке, каждый начинай с '- '. Без вступлений и выводов."
    )
    return llm.chat(FACTS_SYSTEM, user, temperature=0.4, max_tokens=500)


def _resolve_choice(day):
    """Какую персону публиковать: по ответу владельца, по файлу или первого из топа."""
    choice = _load(CHOICE_PATH, {})
    cands = choice.get("candidates") or [c[0] for c in top_persons(day, 3)]
    if not cands:
        return None, None
    # 1) явный ответ владельца боту (1/2/3)
    reply = _safe(lambda nt: nt.latest_reply_from_owner())
    if reply and reply.isdigit():
        idx = int(reply) - 1
        if 0 <= idx < len(cands):
            return cands[idx], "по вашему выбору"
    # 2) выбор, записанный в файл вручную
    if choice.get("chosen"):
        return choice["chosen"], "по вашему выбору"
    # 3) запасной вариант - самый упоминаемый
    return cands[0], "автоматически (самый упоминаемый за неделю)"


def publish(use_llm=True, send=False):
    today = dt.date.today()
    name, how = _resolve_choice(today)
    if not name:
        md = "# Персона недели\n\nНа этой неделе персона не определилась - мало упоминаний."
        print("Персона не определилась.")
    else:
        facts = ""
        if use_llm and llm.available():
            try:
                facts = _facts(name)
            except Exception as e:
                print(f"Модель не дала справку: {str(e)[:100]}")
        body = facts.strip() if facts.strip() else "- Подробную справку добавим позже."
        note = f"\n\n_Герой выбран {how}._" if how else ""
        md = f"# Персона недели: {name}\n\n{body}{note}"
        print(f"Персона недели: {name} ({how})")
        # зафиксируем выбор
        choice = _load(CHOICE_PATH, {})
        choice["chosen"] = name
        _save(CHOICE_PATH, choice)

    os.makedirs(DIGEST_DIR, exist_ok=True)
    out = os.path.join(DIGEST_DIR, f"{today.isoformat()}-person.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    print(f"Готово: {out}")

    if send:
        _safe(lambda nt: nt.send_markdown(md))
    return md


# ---------- мелочи ----------

def _safe(fn):
    """Вызвать функцию notify_telegram, не падая, если что-то не так."""
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
    for name, cnt in top_persons(today, 10):
        print(f"  {cnt:3d}  {name}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--propose", action="store_true", help="собрать топ-3 и отправить владельцу")
    ap.add_argument("--publish", action="store_true", help="опубликовать персону субботы")
    ap.add_argument("--show", action="store_true", help="показать текущий счётчик")
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
