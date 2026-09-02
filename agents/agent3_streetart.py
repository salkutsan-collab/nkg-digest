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


# Сколько дней помним разобранные посты и сами работы. Работы помним дольше:
# об одном и том же мурале разные каналы пишут с разницей в недели.
POST_MEMORY_DAYS = 120
WORK_MEMORY_DAYS = 240


def _load_state():
    """Память радара: {posts: {адрес: дата}, works: [{summary, place, date}]}.
    Старый формат файла - просто {адрес: дата}, его читаем как posts."""
    if not os.path.exists(SEEN_PATH):
        return {"posts": {}, "works": []}
    try:
        with open(SEEN_PATH, encoding="utf-8") as fh:
            raw = json.load(fh)
    except Exception:
        return {"posts": {}, "works": []}
    if isinstance(raw, dict) and ("posts" in raw or "works" in raw):
        return {"posts": raw.get("posts") or {}, "works": raw.get("works") or []}
    return {"posts": raw if isinstance(raw, dict) else {}, "works": []}


def load_seen():
    """Адреса постов, которые уже разбирали."""
    return _load_state()["posts"]


def load_works():
    """Работы, о которых уже сообщали. Нужны потому, что об одной работе пишут
    разные каналы в разные дни, и памяти по адресу поста для этого мало."""
    return _load_state()["works"]


def save_seen(seen, works=None):
    """Записать память. Старые записи вычищаем, чтобы файл не разрастался."""
    today = dt.date.today()
    post_cut = (today - dt.timedelta(days=POST_MEMORY_DAYS)).isoformat()
    work_cut = (today - dt.timedelta(days=WORK_MEMORY_DAYS)).isoformat()
    state = {
        "posts": {u: d for u, d in (seen or {}).items() if d >= post_cut},
        "works": [w for w in (works if works is not None else load_works())
                  if str(w.get("date") or "") >= work_cut],
    }
    with open(SEEN_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=0)


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


def vk_groups(sources):
    """Паблики ВК из настроек - только если задан ключ доступа."""
    groups = sources.get("vk_groups") or []
    if not groups:
        return []
    try:
        import vk_source
    except Exception as e:
        print(f"  (ВК не подключился: {str(e)[:80]})")
        return []
    if not vk_source.configured():
        print(f"  (ВК пропущен: не задан VK_TOKEN, групп в списке {len(groups)})")
        return []
    return groups


def gather(sources, days, workers=6):
    """Собрать посты-кандидаты со всех источников за окно в днях.
    Телеграм читается витриной t.me/s без доступа, ВК - через API по ключу."""
    keywords = sources.get("keywords", [])
    channels = sources.get("channels", [])
    today = dt.date.today()
    start = today - dt.timedelta(days=days)

    def work(ch):
        # у ВК источник задается адресом сообщества (domain), у телеграма - handle
        if ch.get("_source") == "vk":
            import vk_source
            posts = vk_source.fetch_posts(ch.get("domain") or ch.get("handle"))
        else:
            posts = fetch_posts(ch["handle"])
        out = []
        artist = ch.get("kind") == "artist"
        for p in posts:
            if p["date"] and p["date"] < start:
                continue
            by_keyword = has_keyword(p["text"], keywords)
            # канал художника - это лента его работ, и слова-признаки там часто
            # не нужны («Ну, Кот! Адрес: Светлановский проспект, 6»), поэтому
            # такие посты пропускаем к модели без фильтра по словам
            if not by_keyword and not artist:
                continue
            p["_by_keyword"] = by_keyword
            # для каналов "вся Россия" требуем явного намека на Петербург
            if ch.get("scope") == "ru" and not looks_spb(p["text"]):
                continue
            p["_channel"] = ch.get("name") or ch.get("handle") or ch.get("domain")
            p["_handle"] = ch.get("handle") or ch.get("domain")
            out.append(p)
        return ch, out

    found = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        for ch, out in ex.map(work, channels):
            print(f"  {ch.get('name', ch['handle'])}: кандидатов {len(out)}")
            found.extend(out)
    # ВК читаем по одному: у API предел три запроса в секунду
    for g in vk_groups(sources):
        ch, out = work(dict(g, _source="vk"))
        print(f"  ВК {ch.get('name') or ch.get('domain')}: кандидатов {len(out)}")
        found.extend(out)
    found.extend(vk_found_by_search(sources, days))
    return found


def vk_found_by_search(sources, days):
    """Поиск по всему ВК: находки не привязаны к списку групп. Отбор внутри
    vk_source (тема, Петербург, стоп-слова, повторы, город автора)."""
    cfg = sources.get("vk_search") or {}
    queries = cfg.get("queries") or []
    if not queries:
        return []
    try:
        import vk_source
    except Exception as e:
        print(f"  (ВК поиск не подключился: {str(e)[:80]})")
        return []
    if not vk_source.configured():
        return []
    posts = vk_source.search_posts(
        queries,
        days=min(int(cfg.get("days") or days), max(days, 1)),
        limit=int(cfg.get("limit") or 40),
        keywords=sources.get("keywords") or [],
        deny=cfg.get("deny_words") or [],
        spb_hints=SPB_HINTS)
    start = dt.date.today() - dt.timedelta(days=days)
    out = []
    for p in posts:
        if p.get("date") and p["date"] < start:
            continue
        p["_channel"] = f"ВК: {p.get('_author') or 'поиск'}"
        p["_handle"] = "vk-search"
        p["_by_keyword"] = True      # слово темы проверено при поиске
        out.append(p)
    print(f"  ВК поиск по {len(queries)} запросам: кандидатов {len(out)}")
    return out


# ---------- описание моделью ----------

JUDGE_SYSTEM = (
    "Ты редактор городской афиши. Определяешь, сообщает ли пост о НОВОЙ уличной "
    "работе в Петербурге или Ленинградской области: мурал, арт-объект, "
    "паблик-арт, уличная инсталляция, стрит-арт. ГРАФФИТИ НЕ ПОДХОДИТ - посты про "
    "граффити (теги, бомбинг, надписи на стенах) считай нерелевантными. Пиши "
    "простым деловым русским языком, без пафоса и рекламных слов, без жаргона и "
    "англицизмов, без буквы е с точками и без длинного тире. Отвечай ТОЛЬКО "
    "валидным JSON без пояснений."
)


def judge(post):
    """Спросить модель: это про новую уличную работу в Петербурге? Дать описание."""
    prompt = (
        "Вот пост из Telegram-канала про уличное искусство.\n"
        "Верни JSON-объект с полями:\n"
        '  "relevant" - true, если пост сообщает о новой или недавней уличной '
        "работе (мурал, арт-объект, уличная инсталляция, стрит-арт) в Петербурге или области; "
        "false, если это граффити (теги, бомбинг, надписи на стенах), анонс лекции, "
        "продажа, мерч, опрос, общая новость, другой город;\n"
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


def _words(text):
    return set(re.findall(r"[а-яa-z0-9]{4,}",
                          str(text or "").lower().replace("ё", "е")))


def _place(item):
    return str(_as_text(item.get("place"))).lower().replace("ё", "е").strip()


# Адрес - главное удостоверение уличной работы: у мурала на Некрасова, 52 адрес
# один, как бы про него ни написали. Ловим «улица Некрасова, 52», «пр. Мориса
# Тореза, д. 39/2», «Кожевенная линия, 34».
STREET_WORDS = ("улица", "улице", "ул", "проспект", "проспекте", "пр", "просп",
                "набережная", "набережной", "наб", "переулок", "переулке", "пер",
                "линия", "линии", "шоссе", "площадь", "площади", "пл", "бульвар")
ADDRESS_RE = re.compile(
    r"(?:(?:" + "|".join(STREET_WORDS) + r")\.?\s+([а-яa-z\-]{4,})|"
    r"([а-яa-z\-]{4,})\s+(?:" + "|".join(STREET_WORDS) + r")\.?)"
    r"[^\d]{0,12}(\d+[а-я]?(?:/\d+)?)", re.IGNORECASE)


# Адрес пишут и без слова «улица»: «появилась на Некрасова, 52». Такой вид
# ловим отдельно и доверяем ему меньше - слишком легко принять за адрес дату.
LOOSE_RE = re.compile(r"([а-яa-z\-]{5,}),\s*(\d{1,3}[а-я]?(?:/\d+)?)",
                      re.IGNORECASE)
NOT_STREET = ("работа", "работы", "выставка", "выставки", "город", "города",
              "января", "февраля", "марта", "апреля", "мая", "июня", "июля",
              "августа", "сентября", "октября", "ноября", "декабря", "метро",
              "дома", "здании", "фасаде", "проект", "проекта", "художник",
              "художника", "автор", "автора", "серия", "серии")


def _text_of(item):
    text = " ".join(str(x) for x in (_as_text(item.get("place")),
                                     _as_text(item.get("summary")),
                                     item.get("text") or ""))
    return text.lower().replace("ё", "е")


def addresses(item, loose=False):
    """Адреса из сообщения в сравнимом виде («некрасова 52»).
    loose=False - только с указанием типа улицы, это надежный признак.
    loose=True - плюс вид «Некрасова, 52», его берем с оговоркой."""
    text = _text_of(item)
    out = set()
    for m in ADDRESS_RE.finditer(text):
        street = (m.group(1) or m.group(2) or "").strip("-")
        house = m.group(3)
        if street and house:
            out.add(f"{street[:12]} {house}")
    if not loose:
        return out
    for m in LOOSE_RE.finditer(text):
        street, house = m.group(1).strip("-"), m.group(2)
        if street in NOT_STREET:
            continue
        out.add(f"{street[:12]} {house}")
    return out


# Сколько дней совпадение адреса считаем признаком одной и той же работы.
# Дальше по времени по тому же адресу может появиться уже другая работа
# (легальную стену перекрашивают), и запрет был бы неверным.
ADDRESS_SAME_DAYS = 45          # адрес с типом улицы: «улица Некрасова, 52»
ADDRESS_LOOSE_DAYS = 30         # адрес без типа: «Некрасова, 52»


def _day_of(item):
    v = item.get("date")
    if isinstance(v, dt.date):
        return v
    try:
        return dt.date.fromisoformat(str(v)[:10])
    except Exception:
        return None


def _days_apart(a, b):
    """Разница в днях между сообщениями. None - если дат нет."""
    da, db = _day_of(a), _day_of(b)
    return abs((da - db).days) if da and db else None


def same_work(a, b):
    """Об одной ли работе эти два сообщения.

    Сверяем не строку: одну новость разные каналы пишут своими словами.
    Признаки по силе: совпал адрес и сообщения рядом по времени (главное),
    совпало место из разбора модели, просто много общих слов."""
    wa = _words(a.get("summary") or a.get("text"))
    wb = _words(b.get("summary") or b.get("text"))
    if not wa or not wb:
        return False
    common = len(wa & wb) / len(wa | wb)
    apart = _days_apart(a, b)
    if addresses(a) & addresses(b) and common >= 0.1:
        if apart is None or apart <= ADDRESS_SAME_DAYS:
            return True
    if addresses(a, loose=True) & addresses(b, loose=True) and common >= 0.1:
        if apart is not None and apart <= ADDRESS_LOOSE_DAYS:
            return True
    same_place = _place(a) and _place(a) == _place(b)
    return common >= 0.6 or (same_place and common >= 0.35)


def is_known_work(item, works):
    """Сообщали ли об этой работе раньше (в прошлые прогоны)."""
    return any(same_work(item, w) for w in works or [])


def dedupe(items):
    """Убрать повторы внутри одной подборки. Их два вида: художник пишет об
    одной работе дважды (эскиз, потом готовое) и одну новость публикуют разные
    каналы своими словами."""
    out = []
    for it in items:
        if any(same_work(it, kept) for kept in out):
            continue
        out.append(it)
    if len(out) < len(items):
        print(f"  повторов про одну работу убрано: {len(items) - len(out)}")
    return out


def _as_text(value):
    """Привести ответ модели к строке: место или описание иногда приходит
    списком («["Фонтанка", "90"]») или числом, а дальше ждут строку."""
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (list, tuple, set)):
        return ", ".join(_as_text(v) for v in value if v not in (None, "", True, False))
    if isinstance(value, dict):
        return ", ".join(_as_text(v) for v in value.values())
    return str(value).strip()


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
        place = _as_text(it.get("place"))
        summary = _as_text(it.get("summary")) or first_sentence(it["text"])
        line = f"- {summary}"
        if place and place.lower() not in summary.lower():
            line += f" ({place})"
        line += f" - [{it['_channel']}]({it['url']})"
        lines.append(line)
    lines.append("")
    return "\n".join(lines)


# ---------- запуск ----------

def collect_items(days=7, use_llm=True, limit=None, save=False, sources=None):
    """Находки радара: собрать кандидатов, отобрать моделью, отсеять уже
    показанные работы, склеить повторы и (по флагу) записать память.

    Одна точка для всех, кто показывает радар: и агент 3 сам по себе, и
    публикатор в утреннем дайджесте. Раньше публикатор делал это своим кодом,
    поэтому память работ на него не действовала и повторы проходили."""
    sources = sources or load_sources()
    candidates = gather(sources, days)
    print(f"Всего кандидатов по словам: {len(candidates)}")

    seen = load_seen()
    works = load_works()
    today_iso = dt.date.today().isoformat()
    fresh = [c for c in candidates if c["url"] not in seen]
    print(f"Новых (не показанных раньше): {len(fresh)}")
    if limit:
        fresh = fresh[:limit]

    items, old_work = [], 0
    if use_llm and llm.available():
        print(f"Отбор моделью ({llm.provider()})...")
        for c in fresh:
            verdict = judge(c)
            seen[c["url"]] = today_iso  # помечаем как разобранный в любом случае
            if not (verdict and verdict.get("relevant")):
                continue
            item = {**c, "summary": verdict.get("summary"), "place": verdict.get("place")}
            # об этой работе уже сообщали: другой канал, другой день, тот же мурал
            if is_known_work(item, works):
                old_work += 1
                continue
            items.append(item)
    else:
        if use_llm:
            print("Ключ модели не найден - отбираю только по словам (--no-llm).")
        for c in fresh:
            seen[c["url"]] = today_iso
            # без модели отбираем только по словам-признакам, иначе в подборку
            # попадет вся лента художника целиком
            if not c.get("_by_keyword"):
                continue
            if is_known_work(c, works):
                old_work += 1
                continue
            items.append(c)
    if old_work:
        print(f"  про эти работы уже сообщали раньше: {old_work}")

    items = dedupe(items)
    for it in items:
        works.append({"summary": _as_text(it.get("summary")) or first_sentence(it["text"]),
                      "place": _as_text(it.get("place")),
                      "date": today_iso,
                      "url": it.get("url"),
                      "channel": it.get("_channel")})
    if save:
        save_seen(seen, works)
    return items


def run(days, use_llm, do_send, save):
    sources = load_sources()
    print(f"Каналов в списке: {len(sources.get('channels', []))}. Окно: {days} дн.")
    items = collect_items(days=days, use_llm=use_llm, save=save, sources=sources)
    today_iso = dt.date.today().isoformat()
    md = build_markdown(items, days)
    os.makedirs(DIGEST_DIR, exist_ok=True)
    out = os.path.join(DIGEST_DIR, f"{today_iso}-streetart.md")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(md + "\n")
    print(f"Готово: {out}  (находок: {len(items)})")

    if do_send and items:
        _send(md)
    elif do_send:
        print("Отправка пропущена: новых находок нет.")
    return items


def _send(md):
    try:
        import broadcast
        broadcast.send_markdown(md, kind="streetart")
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
         "url": "https://t.me/streetartkeeper/2", "_channel": "Стрит-арт хранитель (САХ)",
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
