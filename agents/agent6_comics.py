# -*- coding: utf-8 -*-
"""
Агент 6 - "Комиксы Петербурга" (раз в неделю, ТОЛЬКО в личку владельцу).

Раз в неделю (понедельник) ищет в вебе всё про комиксы в Санкт-Петербурге:
выставки, встречи и лекции, маркеты и ярмарки, магазины комиксов и места, где
комиксы продаются, фестивали. Присылает подборку владельцу в личный чат Telegram.
В каналы НЕ публикуется.

Движок - веб-поиск (OpenAI/Anthropic), как у "Разбора дня" (agent5_feature).

Запуск (Windows - py):
  py agents/agent6_comics.py --sample   # собрать и НАПЕЧАТАТЬ, ничего не отправляя
  py agents/agent6_comics.py --send      # собрать и прислать владельцу в личку
"""

import os
import re
import sys
import json
import argparse
import datetime as dt

import agent5_feature as feat   # переиспользуем веб-поиск и чистку стиля
import notify_telegram as nt

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGEST_DIR = os.path.join(ROOT, "digests")
SENT_PATH = os.path.join(ROOT, "data", "comics_sent.json")   # что уже присылали

# Сколько прошлых подборок помним и передаем модели как «это уже было».
KEEP_WEEKS = 6

COMICS_SYSTEM = (
    "Ты редактор петербургского культурного канала. Готовишь для редактора подборку "
    "про комиксы в Санкт-Петербурге, активно пользуясь веб-поиском. Ищи: выставки про "
    "комиксы и графические романы; встречи, лекции, клубы и мастер-классы; маркеты и "
    "ярмарки, где продают комиксы; магазины комиксов и места, где комиксы представлены; "
    "фестивали. Опирайся ТОЛЬКО на найденное в вебе - ничего не выдумывай. Проверяй, что "
    "места и события ДЕЙСТВУЮЩИЕ сейчас (2026); закрытые не включай. По каждому пункту: "
    "что это, когда (для событий - даты), адрес и ссылка. "
    "Стиль: простой человеческий язык, без жаргона и англицизмов, без рекламных слов и "
    "пафоса, без буквы е с точками и без длинного тире. Сгруппируй по разделам "
    "(Выставки; Встречи и лекции; Маркеты и ярмарки; Магазины; Фестивали) - пустые "
    "разделы пропускай. В конце строкой 'Источники:' перечисли ссылки, которыми пользовался."
)


def load_sent():
    """Названия из прошлых подборок - чтобы не присылать одно и то же неделями."""
    if not os.path.exists(SENT_PATH):
        return []
    try:
        with open(SENT_PATH, encoding="utf-8") as fh:
            return (json.load(fh) or {}).get("weeks") or []
    except Exception:
        return []


def _titles_of(body):
    """Названия пунктов подборки: строки списка без пояснений."""
    out = []
    for line in str(body or "").splitlines():
        line = line.strip()
        if not line.startswith(("-", "*", "•")):
            continue
        item = re.sub(r"^[-*•]\s*", "", line)
        item = re.split(r"[.,(]|\s+-\s+|:", item, maxsplit=1)[0].strip(" *_«»\"")
        if 3 < len(item) < 90:
            out.append(item)
    return out[:20]


def save_sent(body, day=None):
    """Запомнить, что было в подборке этой недели."""
    day = day or dt.date.today()
    weeks = [w for w in load_sent() if w.get("date") != day.isoformat()]
    weeks.append({"date": day.isoformat(), "titles": _titles_of(body)})
    weeks = weeks[-KEEP_WEEKS:]
    os.makedirs(os.path.dirname(SENT_PATH), exist_ok=True)
    with open(SENT_PATH, "w", encoding="utf-8") as fh:
        json.dump({"weeks": weeks}, fh, ensure_ascii=False, indent=1)
    print(f"Комиксы: в память записано пунктов - {len(weeks[-1]['titles'])}")


def already_sent_titles():
    seen = []
    for w in load_sent():
        for t in w.get("titles") or []:
            if t not in seen:
                seen.append(t)
    return seen


def build():
    prov = feat.feature_provider()
    today = dt.date.today()
    # то, что уже присылали, отдаем модели: пусть ищет новое, а повторы берет
    # только если это действительно важное место или событие идет прямо сейчас
    was = already_sent_titles()
    avoid = ""
    if was:
        avoid = ("\n\nЭто уже присылали в прошлых подборках, поэтому НЕ повторяй, "
                 "если только событие не идет прямо сейчас и не является главным: "
                 + "; ".join(was[:40]) + ".")
    user = (f"Сегодня {today.isoformat()}. Собери подборку про комиксы в Санкт-Петербурге "
            "на ближайшие 1-2 недели (события) и действующие места (магазины, точки "
            "продаж). Сгруппируй по разделам, по правилам из системной инструкции."
            + avoid)
    if prov == "openai":
        return feat._openai_research(user, system=COMICS_SYSTEM)
    if prov == "anthropic":
        return feat._anthropic_research(user, system=COMICS_SYSTEM)
    print("Комиксы: нужен веб-поиск (OPENAI_API_KEY/ANTHROPIC_API_KEY) - Yandex тут не годится.")
    return ""


def run(send=False):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from notify_telegram import md_to_tg
    body = build()
    if not body:
        print("Комиксы: подборка не собралась.")
        return
    body = feat.style_clean(body)
    today = dt.date.today()
    os.makedirs(DIGEST_DIR, exist_ok=True)
    with open(os.path.join(DIGEST_DIR, f"{today.isoformat()}-comics.md"),
              "w", encoding="utf-8") as fh:
        fh.write(body + "\n")
    msg = "<b>Комиксы в Петербурге - подборка недели</b>\n\n" + md_to_tg(body)

    if not send:
        print("\n----- ПРЕДПРОСМОТР (не отправлено) -----\n")
        print(msg)
        print("\n----- конец -----")
        return

    parts = nt.split_chunks(msg)
    sent = False
    # Telegram - владельцу в личку (если задан)
    if nt.owner_chat():
        try:
            for p in parts:
                nt.send_text(p, chat=nt.owner_chat())
            print("Комиксы: отправлено в Telegram-личку владельцу.")
            sent = True
        except Exception as e:
            print(f"  (Telegram-личка: {str(e)[:100]})")
    # Max - в личку получателям по user_id (секрет MAX_DM_RECIPIENTS)
    try:
        import notify_max as nm
        for uid in nm.dm_recipients():
            for p in parts:
                nm.send_dm(p, uid)
            print(f"Комиксы: отправлено в Max-личку (user_id={uid}).")
            sent = True
    except Exception as e:
        print(f"  (Max-личка: {str(e)[:100]})")
    if sent:
        save_sent(body)
    else:
        print("Получатели не настроены (TELEGRAM_OWNER_CHAT_ID / MAX_DM_RECIPIENTS).")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="напечатать, не отправляя")
    ap.add_argument("--send", action="store_true", help="прислать владельцу в личку")
    args = ap.parse_args()
    run(send=args.send and not args.sample)


if __name__ == "__main__":
    main()
