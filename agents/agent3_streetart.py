# -*- coding: utf-8 -*-
"""
Агент 3 - "Стрит-арт радар".

Что делает:
  1. Берет список Telegram-каналов из data/streetart_sources.yaml.
  2. Читает их свежие посты через веб-витрину t.me/s/<канал> (без токена).
  3. Оставляет посты с признаками новинки (мурал, граффити, арт-объект и т. п.)
     за последние N дней.
  4. Помнит, о чем уже рассказывал (data/streetart_seen.json), чтобы не повторяться.
  5. Если есть ключ модели - просит ее коротко описать находку и отсеять
     то, что не про Петербург. Без ключа работает по словам-признакам.
  6. Складывает блок "Новое на стенах города" в digests/ и (по флагу) шлет в Telegram.

Запуск (Windows - py):
  py agents/agent3_streetart.py --self-test    # формат на примере, без сети и ключа
  py agents/agent3_streetart.py --no-llm        # только по словам-признакам
  py agents/agent3_streetart.py                  # с моделью (короткие описания, отбор по городу)
  py agents/agent3_streetart.py --days 14        # окно поиска в днях (по умолчанию 7)
  py agents/agent3_streetart.py --send           # отправить результат в Telegram
"""

import os
import re
import sys
import json
import html
import argparse
import datetime as dt
import concurrent.futures as cf

import requests
from bs4 import BeautifulSoup
from ruamel.yaml import YAML

import llm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SOURCES_PATH = os.path.join(ROOT, "data", "streetart_sources.yaml")
SEEN_PATH = os.path.join(ROOT, "data", "streetart_seen.json")
DIGEST_DIR = os.path.join(ROOT, "digests")

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; NKG-StreetArt/1.0)"}
MONTHS = ["", "января", "февраля", "марта", "апреля", "мая", "июня",
          "июля", "августа", "сентября", "октября", "ноября", "декабря"]

# Слова, по которым узнаем, что пост про Петербург (для каналов "вся Россия")
SPB_HINTS = ["петербург", "питер", "спб", "ленинград", "ленобласт",
             "васильевск", "невск", "петроградск", "купчино", "выборгск"]


# ---------- конфиг и память ----------

def load_sources():
    y = YAML(typ="safe")
    with open(SOURCES_PATH, encoding="utf-8") as fh:
        return y.load(fh)


def load_seen():
    if not os.path.exists(SEEN_PATH):
        return {}
    try:
        with open(SEEN_PATH, encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {}


def save_seen(seen):
    # храним только последние ~120 дней, чтобы файл не разрастался
    cutoff = (dt.date.today() - dt.timedelta(days=120)).isoformat()
    pruned = {u: d for u, d in seen.items() if d >= cutoff}
    with open(SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(pruned, fh, ensure_ascii=False, indent=0)


# ---------- чтение Telegram-канала ----------

def fetch_posts(handle, limit=30):
    """Свежие посты канала через t.me/s/<канал>. Возвращает список словарей."""
    url = f"https://t.me/s/{handle}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code >= 400:
            return []
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return []
    posts = []
    for msg in soup.select("div.tgme_widget_message"):
        txt_el = msg.select_one(".tgme_widget_message_text")
        text = txt_el.get_text("\n", strip=True) if txt_el else ""
        date_el = msg.select_one("a.tgme_widget_message_date")
        link = date_el.get("href") if date_el else None
        time_el = msg.select_one("time[datetime]")
        iso = time_el.get("datetime") if time_el else None
        day = None
        if iso:
            try:
                day = dt.date.fromisoformat(iso[:10])
            except Exception:
                day = None
        if text and link:
            posts.append({"text": text, "url": link, "date": day})
    return posts[-limit:]


# ---------- отбор ----------

def has_keyword(text, keywords):
    low = text.lower()
    return any(k.lower() in low for k in keywords)


def looks_spb(text):
    low = text.lower()
    return any(h in low for h in SPB_HINTS)


def gather(sources, days, workers=6):
    """Собрать посты-кандидаты со всех каналов за окно в днях."""
    keywords = sources.get("keywords", [])
    channels = sources.get("channels", [])
    today = dt.date.today()
    start = today - dt.timedelta(days=days)

    def work(ch):
        posts = fetch_posts(ch["handle"])
        out = []
        for p in posts:
            if p["date"] and p["date"] < start:
                continue
            if not has_keyword(p["text"], keywords):
                continue
            # для каналов "вся Россия" требуем явного намека на Петербург
            if ch.get("scope") == "ru" and not looks_spb(p["text"]):
                continue
            p["_channel"] = ch.get("name", ch["handle"])
            p["_handle"] = ch["handle"]
            out.append(p)
        return ch, out

    found = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for ch, out in ex.map(work, channels):
            print(f"  {ch.get('name', ch['handle'])}: кандидатов {len(out)}")
            found.extend(out)
    return found


# ---------- описание моделью ----------

JUDGE_SYSTEM = (
    "Ты редактор городской афиши. Определяешь, сообщает ли пост о НОВОЙ уличной "
    "работе в Петербурге или Ленинградской области: мурал, граффити, арт-объект, "
    "паблик-арт, инсталляция на улице. Пиши простым деловым русским языком, без "
    "пафоса и рекламных слов, без жаргона и англицизмов, без буквы е с точками и "
    "без длинного тире. Отвечай ТОЛЬКО валидным JSON без пояснений."
)


def judge(post):
    """Спросить модель: это про новую уличную работу в Петербурге? Дать описание."""
    prompt = (
        "Вот пост из Telegram-канала про уличное искусство.\n"
        "Верни JSON-объект с полями:\n"
        '  "relevant" - true, если пост сообщает о новой или недавней уличной '
        "работе (мурал, граффити, арт-объект, инсталляция) в Петербурге или области; "
        "false, если это анонс лекции, продажа, мерч, опрос, общая новость, другой город;\n"
        '  "summary"  - 1 короткое предложение что и где появилось (без воды);\n'
        '  "place"    - район или адрес, если упомянут, иначе null.\n\n'
        f"ТЕКСТ ПОСТА:\n{post['text'][:1500]}"
    )
    try:
        raw = llm.chat(JUDGE_SYSTEM, prompt, temperature=0.1, max_tokens=400)
    except Exception as e:
        print(f"  модель не ответила: {str(e)[:100]}")
        return None
    m = re.search(r"\{.*\}", raw, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


# ---------- сборка текста ----------

def first_sentence(text, n=160):
    """Короткая выжимка из поста, когда модель недоступна."""
    s = re.sub(r"\s+", " ", text).strip()
    cut = re.split(r"(?<=[.!?])\s", s)
    head = cut[0] if cut else s
    return (head[:n] + "...") if len(head) > n else head


def build_markdown(items, days):
    head = f"за последние {days} дн."
    lines = ["# Новое на стенах города", "", f"_{head}_", ""]
    if not items:
        lines += ["Новых уличных работ за период не нашлось.", ""]
        return "\n".join(lines)
    lines += [f"Свежих находок: {len(items)}.", ""]
    for it in items:
        place = it.get("place")
        summary = it.get("summary") or first_sentence(it["text"])
        line = f"- {summary}"
        if place and place.lower() not in summary.lower():
            line += f" ({place})"
        line += f" - [{it['_channel']}]({it['url']})"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


# ---------- запуск ----------

def run(days, use_llm, do_send, save):
    sources = load_sources()
    print(f"Каналов в списке: {len(sources.get('channels', []))}. Окно: {days} дн.")
    candidates = gather(sources, days)
    print(f"Всего кандидатов по словам: {len(candidates)}")

    seen = load_seen()
    today_iso = dt.date.today().isoformat()
    fresh = [c for c in candidates if c["url"] not in seen]
    print(f"Новых (не показанных раньше): {len(fresh)}")

    items = []
    if use_llm and llm.available():
        print(f"Отбор моделью ({llm.provider()})...")
        for c in fresh:
            verdict = judge(c)
            seen[c["url"]] = today_iso  # помечаем как разобранный в любом случае
            if verdict and verdict.get("relevant"):
                items.append({**c, "summary": verdict.get("summary"),
                              "place": verdict.get("place")})
    else:
        if use_llm:
            print("Ключ модели не найден - отбираю только по словам (--no-llm).")
        for c in fresh:
            seen[c["url"]] = today_iso
            items.append(c)

    md = build_markdown(items, days)
    os.makedirs(DIGEST_DIR, exist_ok=True)
    out = os.path.join(DIGEST_DIR, f"{today_iso}-streetart.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    print(f"Готово: {out}  (находок: {len(items)})")

    if save:
        save_seen(seen)

    if do_send and items:
        _send(md)
    elif do_send:
        print("Отправка пропущена: новых находок нет.")
    return items


def _send(md):
    try:
        import broadcast
        broadcast.send_markdown(md)
    except SystemExit as e:
        print(f"Отправка не удалась: {e}")
    except Exception as e:
        print(f"Отправка не удалась: {str(e)[:160]}")


def self_test():
    sample = [
        {"text": "Новый мурал появился на брандмауэре в Купчино: художник расписал "
                 "торец дома портретом. Адрес - Бухарестская улица.",
         "url": "https://t.me/lenstreet/1", "_channel": "ЛЕНСТРИТ",
         "place": "Купчино, Бухарестская ул."},
        {"text": "Во дворе на Васильевском острове установили новый арт-объект из металла.",
         "url": "https://t.me/streetartmuseum/2", "_channel": "Музей стрит-арта",
         "place": "Васильевский остров"},
    ]
    for s in sample:
        s["summary"] = first_sentence(s["text"])
    print(build_markdown(sample, days=7))


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7, help="окно поиска в днях")
    ap.add_argument("--no-llm", action="store_true", help="без модели, только по словам")
    ap.add_argument("--self-test", action="store_true", help="формат на примере, без сети")
    ap.add_argument("--send", action="store_true", help="отправить находки в Telegram")
    ap.add_argument("--no-save", action="store_true", help="не записывать память (для отладки)")
    args = ap.parse_args()

    if args.self_test:
        self_test()
        return

    run(days=args.days, use_llm=not args.no_llm,
        do_send=args.send, save=not args.no_save)


if __name__ == "__main__":
    main()
