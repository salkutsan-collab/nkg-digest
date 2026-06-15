# -*- coding: utf-8 -*-
"""
Агент 5 - "Разбор дня".

Содержательный дневной пост про ОДНО событие: что это, контекст, немного
истории площадки или автора, на что обратить внимание. В отличие от утренней
афиши (сухой список ссылок), здесь связный текст - но строго с опорой на
источники, без выдуманных фактов.

Два шага (важно для честности):
  1. Сбор фактов - низкая температура, только то, что есть в источниках
     (страница события + справка из русской Википедии о площадке/авторе).
  2. Редактура - живее, но по правилам стиля проекта (простой язык, без
     рекламных слов, пафоса, жаргона и англицизмов).

Запуск (Windows - py):
  py agents/agent5_feature.py --sample      # собрать и НАПЕЧАТАТЬ пост, никуда не отправляя
  py agents/agent5_feature.py --send        # собрать и опубликовать в каналы
"""

import os
import re
import sys
import argparse
import datetime as dt

import agent2_digest as a2
import publisher as pub
import images
import llm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGEST_DIR = os.path.join(ROOT, "digests")


# ---------- выбор события и сбор источников ----------

def todays_events():
    """События сегодняшнего дня: из предпросмотра (если он про сегодня) или сбор заново."""
    today = dt.date.today()
    day_key = pub.WEEKDAY_KEYS[today.weekday()]
    data = pub._load_json(pub.PREVIEW_PATH)
    if data and data.get("target") == today.isoformat():
        return data.get("events", [])
    themes = pub.load_themes()
    theme = pub.theme_for(day_key, themes)
    if not theme or theme.get("mode") == "person":
        return []
    events, _start, _end = pub.collect_for_theme(theme, today, use_llm=True)
    return events


def pick_subject(events):
    """Самое релевантное событие, у которого есть за что зацепиться (площадка/персона)."""
    if not events:
        return None
    ranked = sorted(events, key=lambda e: -pub.relevance(e))
    return ranked[0]


def _venue_website(name):
    """Адрес сайта площадки из базы участников (для страницы 'о галерее/музее')."""
    try:
        base = a2.load_base()
        for p in base.get("participants", []):
            if p.get("name") == name:
                for s in p.get("sources", []):
                    if s.get("type") == "website":
                        return s.get("url")
    except Exception:
        pass
    return None


def gather_sources(event):
    """Собрать материалы для опоры: страница события + сайт площадки + справки из Википедии."""
    page = ""
    url = event.get("_url")
    if url:
        page = a2.fetch_text(url)
    venue = event.get("_participant", "")
    # сайт площадки - часто там история и описание (особенно если нет статьи в Википедии)
    about = ""
    site = _venue_website(venue)
    if site:
        about = a2.fetch_text(site)
    names = []
    if venue:
        names.append(venue)
    names += [p for p in (event.get("persons") or []) if p][:2]
    wiki = []
    seen = set()
    for name in names:
        if name in seen:
            continue
        seen.add(name)
        title, text, wurl = images.wiki_summary(name, sentences=6)
        if text:
            wiki.append({"name": name, "title": title, "text": text, "url": wurl})
    return page, about, wiki


# ---------- шаг 1: проверенные факты ----------

FACTS_SYSTEM = (
    "Ты помощник-фактчекер культурной редакции. Из приложенных источников "
    "выписываешь ТОЛЬКО факты, которые там прямо есть. Ничего не придумываешь и "
    "не додумываешь: если чего-то в источниках нет - просто не пишешь об этом. "
    "Без оценок и рекламных слов. Каждый факт - короткой строкой, по-русски, "
    "без буквы е с точками и без длинного тире."
)


def verified_facts(event, page, venue_about, wiki):
    parts = [f"СОБЫТИЕ: {event.get('title','')} - {event.get('_participant','')}."]
    descr = (event.get("about") or "").strip()
    if descr:
        parts.append("Описание с сайта: " + descr)
    if page:
        parts.append("ТЕКСТ СТРАНИЦЫ СОБЫТИЯ:\n" + page[:4000])
    if venue_about:
        parts.append(f"САЙТ ПЛОЩАДКИ ({event.get('_participant','')}):\n"
                     + venue_about[:3000])
    for w in wiki:
        parts.append(f"СПРАВКА (Википедия, {w['title']}):\n{w['text']}")
    user = ("Из источников ниже выпиши проверенные факты, полезные для рассказа об "
            "этом событии: про площадку (когда открылась, чем известна, на чем "
            "специализируется), автора (кто он, чем занимается, направление), про "
            "само событие и его контекст. Только то, что прямо есть в источниках. "
            "Список короткими строками. Если фактов мало - это нормально, не "
            "добавляй ничего от себя.\n\n" + "\n\n".join(parts))
    try:
        return llm.chat(FACTS_SYSTEM, user, temperature=0.1, max_tokens=900).strip()
    except Exception as e:
        print(f"  (факты не собраны: {str(e)[:100]})")
        return ""


# ---------- шаг 2: текст поста ----------

WRITE_SYSTEM = (
    "Ты редактор петербургского культурного канала про дизайн и искусство. "
    "Пишешь короткий живой пост про одно событие, опираясь ТОЛЬКО на переданные "
    "проверенные факты - ничего не выдумываешь сверх них. "
    "Правила стиля: простой человеческий язык, без жаргона и англицизмов (если "
    "термин нужен - тут же поясни простыми словами); без рекламных слов и пафоса "
    "(нельзя 'не пропустите', 'настоящий', 'уникальный', 'праздник'); без буквы е "
    "с точками и без длинного тире (обычная е и дефис). "
    "Запрещены и любые их синонимы: 'не пропустите', 'погружение', 'оживает', "
    "'возможность увидеть', 'выходит за рамки', 'по-новому раскрывает', "
    "'уникальный', 'неповторимый'. Не "
    "обращайся к читателю ('вы увидите') - пиши про само событие и факты. "
    "Структура: первая фраза по сути, без рекламы; что это за событие; немного "
    "контекста или истории из фактов; на что обратить внимание. 4-6 абзацев, "
    "коротких. Без заголовка и без ссылок - их добавят отдельно."
)


def style_clean(text):
    """Убрать то, что запрещено правилами проекта: длинное тире и букву е с точками."""
    if not text:
        return text
    text = text.replace("—", "-").replace("–", "-")  # длинное/среднее тире -> дефис
    text = text.replace("ё", "е").replace("Ё", "Е")  # ё -> е, Ё -> Е
    return text


def write_post(event, facts, wiki):
    when = pub._when_str(event)
    base = (f"Событие: {event.get('title','')}\n"
            f"Площадка: {event.get('_participant','')}{when}\n"
            f"Проверенные факты (опирайся только на них):\n{facts or '(фактов мало)'}")
    try:
        text = llm.chat(WRITE_SYSTEM, base, temperature=0.6, max_tokens=1200).strip()
    except Exception as e:
        print(f"  (текст не написан: {str(e)[:100]})")
        return ""
    return text


# ---------- сборка и отправка ----------

# ---------- глубокий путь: Anthropic + веб-поиск ----------

def feature_provider():
    """Каким движком делать 'Разбор дня': openai | anthropic | yandex.
    Берём из FEATURE_PROVIDER, если ключ есть; иначе - что доступно; иначе Yandex."""
    prov = os.environ.get("FEATURE_PROVIDER", "").lower()
    if prov == "openai" and os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if prov == "anthropic" and os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    if os.environ.get("OPENAI_API_KEY"):
        return "openai"
    if os.environ.get("ANTHROPIC_API_KEY"):
        return "anthropic"
    return "yandex"


RESEARCH_SYSTEM = (
    "Ты редактор петербургского канала про дизайн и искусство. Пишешь содержательный "
    "пост про одно событие, активно пользуясь веб-поиском. Найди КОНКРЕТИКУ: конкретные "
    "работы автора и где их можно увидеть (выставки, площадки, города); чем известна "
    "площадка; и короткую прогулку рядом с её адресом - что посмотреть, где поесть, что "
    "сфотографировать. Опирайся ТОЛЬКО на найденное в вебе - ничего не выдумывай; чего "
    "не нашел, о том не пиши. "
    "Стиль: простой человеческий язык, без жаргона и англицизмов (термин - поясняй "
    "простыми словами); без рекламных слов и пафоса ('не пропустите', 'уникальный', "
    "'погружение', 'оживает' и подобных); без буквы е с точками и без длинного тире. "
    "Про прогулку: сначала по найденному определи стиль и темы автора, и подбирай "
    "места ПОД этот стиль - то, что понравится людям, которым близка такая эстетика "
    "(к грубой бетонной скульптуре - конструктивистская архитектура или брутальные "
    "интерьеры рядом; к тихой графике - сады, тихие дворы, букинисты), а не просто "
    "ближайшее кафе. К каждому месту - короткая фраза, почему оно в тему. "
    "ВАЖНО: рекомендуй ТОЛЬКО места, которые работают СЕЙЧАС (2026) - проверяй это "
    "поиском. Не предлагай закрытые или давно не работающие пространства (например, "
    "лофт Ткачи и Музей стрит-арта закрыты - их и подобные не предлагать). Лучше "
    "меньше мест, но все действующие. "
    "Структура: первая фраза по сути; что за событие и чем интересна площадка; "
    "конкретные работы автора и где их видели; раздел 'Сделать из этого прогулку' "
    "(посмотреть/поесть/сфотографировать рядом, подобранные под стиль автора); "
    "5-7 коротких абзацев. Без заголовка - его добавят отдельно. В конце двумя "
    "отдельными строками: сначала 'Источники:' с 3-5 ссылками, которыми пользовался; "
    "затем ОБЯЗАТЕЛЬНО строкой 'Фото:' дай 1-3 ПРЯМЫЕ ссылки на изображения "
    "(оканчиваются на .jpg/.jpeg/.png/.webp) - афишу или промо выставки с сайта "
    "площадки, либо фото работ автора. Специально поищи такую картинку. Но если "
    "уверенной прямой ссылки на изображение нет - строку 'Фото:' не выдумывай и не пиши."
)


PHOTO_TOKEN = re.compile(r"https?://[^\s,;]+", re.I)


def _extract_photos(text):
    """Найти прямые ссылки на изображения во всём ответе модели (не только в 'Фото:')."""
    out = []
    for tok in PHOTO_TOKEN.findall(text):
        if images._usable_image(tok) and tok not in out:
            out.append(tok)
    return out[:3]


def _strip_photo_line(text):
    """Убрать из тела служебную строку 'Фото:' и 'голые' строки-ссылки на картинки
    (они уйдут отдельным альбомом, а не текстом)."""
    out = []
    for l in text.splitlines():
        s = l.strip()
        if s.lower().startswith("фото:"):
            continue
        if s and " " not in s and images._usable_image(s):  # строка - только ссылка на фото
            continue
        out.append(l)
    return "\n".join(out).strip()


def _anthropic_research(user, system=RESEARCH_SYSTEM):
    import anthropic
    client = anthropic.Anthropic()
    model = os.environ.get("FEATURE_MODEL", "claude-sonnet-4-6")
    tools = [
        {"type": "web_search_20260209", "name": "web_search"},
        {"type": "web_fetch_20260209", "name": "web_fetch"},
    ]
    messages = [{"role": "user", "content": user}]
    text = ""
    for _ in range(5):  # серверный цикл инструментов может вернуть pause_turn - продолжаем
        resp = client.messages.create(
            model=model, max_tokens=4000, system=system,
            tools=tools, messages=messages, thinking={"type": "disabled"},
        )
        if resp.stop_reason == "pause_turn":
            messages.append({"role": "assistant", "content": resp.content})
            continue
        text = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        break
    return text.strip()


def _openai_research(user, system=RESEARCH_SYSTEM):
    import requests as rq
    key = os.environ["OPENAI_API_KEY"]
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-search-preview")
    body = {"model": model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "max_tokens": 1500}
    r = rq.post("https://api.openai.com/v1/chat/completions",
                headers={"Authorization": "Bearer " + key,
                         "Content-Type": "application/json"},
                json=body, timeout=120)
    data = r.json()
    if r.status_code != 200:
        raise RuntimeError(f"OpenAI: {r.status_code} {str(data)[:200]}")
    return (data["choices"][0]["message"].get("content") or "").strip()


def research_feature(event, provider):
    when = pub._when_str(event)
    addr = event.get("_address") or ""
    persons = ", ".join(p for p in (event.get("persons") or []) if p)
    user = (f"Событие: {event.get('title','')}.\n"
            f"Площадка: {event.get('_participant','')}{when}.\n"
            + (f"Адрес: {addr}.\n" if addr else "")
            + (f"Авторы/участники: {persons}.\n" if persons else "")
            + (f"Ссылка: {event.get('_url')}.\n" if event.get('_url') else "")
            + "Напиши пост по правилам из системной инструкции, найдя конкретику в вебе.")
    try:
        if provider == "openai":
            return _openai_research(user)
        return _anthropic_research(user)
    except Exception as e:
        print(f"  (исследование {provider} не удалось: {str(e)[:140]})")
        return ""


VERIFY_SYSTEM = (
    "Ты редактор-фактчекер культурного канала. Тебе дают готовый черновик поста. "
    "Твоя задача - проверить раздел про прогулку: КАЖДОЕ упомянутое место (музей, "
    "галерея, кафе, пространство, парк) перепроверь веб-поиском - работает ли оно "
    "СЕЙЧАС (2026), не закрыто ли, не переехало. Закрытые, несуществующие или "
    "сомнительные места убери; если осталось мало - замени на соседние, которые "
    "поиском подтверждены как действующие. Остальной текст (про событие и автора) "
    "оставь без изменений. Тот же стиль: простой язык, без рекламы, без буквы е с "
    "точками и без длинного тире. Верни ВЕСЬ пост целиком, в том же формате "
    "(включая строки 'Источники:' и 'Фото:', если они были)."
)


def verify_places(draft, provider):
    """Шаг проверки перед публикацией: перепроверить, что места маршрута работают."""
    user = ("Проверь и при необходимости исправь этот пост (раздел про прогулку - "
            "убрать закрытые/несуществующие места, оставить только действующие):\n\n" + draft)
    try:
        if provider == "openai":
            return _openai_research(user, system=VERIFY_SYSTEM)
        return _anthropic_research(user, system=VERIFY_SYSTEM)
    except Exception as e:
        print(f"  (проверка маршрута не удалась: {str(e)[:120]})")
        return ""


def feature_images(event, limit=3):
    """2-3 картинки для зацепления. Сначала - со страницы самого события (промо-кадры
    выставки), затем добираем фото автора из Википедии, если не хватило."""
    out = images.page_content_images(event.get("_url"), limit=limit)
    if len(out) < limit:
        persons = [p for p in (event.get("persons") or []) if p]
        if persons:
            info = images.find_person_images(persons[0])
            for u in [info.get("portrait")] + info.get("works", []):
                if u and u not in out:
                    out.append(u)
                    if len(out) >= limit:
                        break
    return out[:limit]


def _valid(urls):
    """Оставить только реально загружающиеся картинки (битые/защищённые отсеять)."""
    return [u for u in (urls or []) if images.image_loads(u)][:3]


def build(event):
    # глубокий путь: веб-поиск через OpenAI или Anthropic (конкретика, прогулка, источники, фото)
    prov = feature_provider()
    if prov in ("openai", "anthropic"):
        raw = research_feature(event, prov)
        if raw:
            checked = verify_places(raw, prov) or raw   # проверка мест маршрута перед публикацией
            photos = _valid(_extract_photos(raw))       # фото берём из исходного ответа
            body = style_clean(_strip_photo_line(checked))
            if not photos:                              # модель фото не дала - пробуем со страницы
                photos = _valid(feature_images(event))
            return body, [], photos
        print("  (откат на сбор без веб-поиска)")
    # запасной путь: YandexGPT по странице события + Википедии
    page, venue_about, wiki = gather_sources(event)
    facts = verified_facts(event, page, venue_about, wiki)
    body = write_post(event, facts, wiki)
    if not body:
        return None, None, None
    return style_clean(body), wiki, _valid(feature_images(event))


def header_caption(event):
    """Короткая подпись-заголовок (идёт с фотоальбомом, влезает в лимит подписи)."""
    import html
    when = pub._when_str(event)
    return (f"<b>Разбор дня</b>\n{html.escape(event.get('title',''))} - "
            f"{html.escape(event.get('_participant',''))}{html.escape(when)}")


def body_html(event, body, wiki):
    """Тело поста: текст + ссылка на событие + источники (без верхнего заголовка).
    Тело прогоняем через md_to_tg: OpenAI отдаёт markdown (ссылки, жирный) - делаем HTML."""
    import html
    from notify_telegram import md_to_tg
    lines = [md_to_tg(body)]
    url = event.get("_url")
    if url:
        lines += ["", f'<a href="{url}">Страница события</a>']
    srcs = [w for w in wiki if w.get("url")]
    if srcs:
        links = "; ".join(f'<a href="{w["url"]}">{html.escape(w["title"])}</a>' for w in srcs)
        lines.append("Источники: " + links)
    return "\n".join(lines)


def run(send=False):
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    events = todays_events()
    subject = pick_subject(events)
    if not subject:
        print("Разбор дня: подходящего события не нашлось - пропускаем.")
        return
    print(f"Разбор дня про: {subject.get('title')} - {subject.get('_participant')}")
    body, wiki, imgs = build(subject)
    if not body:
        print("Разбор дня: текст не получился - пропускаем.")
        return
    imgs = imgs or []
    cap = header_caption(subject)
    bhtml = body_html(subject, body, wiki)

    today = dt.date.today()
    os.makedirs(DIGEST_DIR, exist_ok=True)
    with open(os.path.join(DIGEST_DIR, f"{today.isoformat()}-feature.md"),
              "w", encoding="utf-8") as fh:
        fh.write(body + "\n")

    if not send:
        print("\n----- ПРЕДПРОСМОТР (не отправлено) -----\n")
        print(cap + "\n\n" + bhtml)
        print(f"\n[фото для зацепа: {len(imgs)}]")
        for u in imgs:
            print("  " + u)
        print("----- конец -----")
        return

    import broadcast
    # фото - необязательный зацеп без подписи; полный текст (с заголовком) уходит всегда,
    # поэтому сбой картинки на любой площадке не урезает сам пост
    if imgs:
        try:
            broadcast.send_photos(imgs)
        except Exception as e:
            print(f"  (фото не ушли: {str(e)[:100]})")
    try:
        broadcast.send_text(cap + "\n\n" + bhtml)
        print("Разбор дня опубликован.")
    except Exception as e:
        print(f"Отправка не удалась: {str(e)[:160]}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sample", action="store_true", help="напечатать пост, не отправляя")
    ap.add_argument("--send", action="store_true", help="опубликовать в каналы")
    args = ap.parse_args()
    run(send=args.send and not args.sample)


if __name__ == "__main__":
    main()
