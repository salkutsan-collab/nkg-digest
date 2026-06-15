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
import json
import argparse
import datetime as dt

import agent2_digest as a2
import publisher as pub
import images
import llm

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DIGEST_DIR = os.path.join(ROOT, "digests")
STATE_PATH = os.path.join(ROOT, "data", "feature_preview.json")  # черновик на согласование


# ---------- выбор события и сбор источников ----------

def events_for(target):
    """События на дату target: из предпросмотра (если он про эту дату) или сбор заново."""
    day_key = pub.WEEKDAY_KEYS[target.weekday()]
    data = pub._load_json(pub.PREVIEW_PATH)
    if data and data.get("target") == target.isoformat():
        return data.get("events", [])
    themes = pub.load_themes()
    theme = pub.theme_for(day_key, themes)
    if not theme or theme.get("mode") == "person":
        return []
    events, _start, _end = pub.collect_for_theme(theme, target, use_llm=True)
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
                json=body, timeout=180)
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


def feature_images(event, limit=4):
    """3-4 картинки для зацепа: фото со страницы галереи/выставки + портреты авторов
    (и куратора, если он указан) из Википедии."""
    out = list(images.page_content_images(event.get("_url"), limit=limit))
    for name in [p for p in (event.get("persons") or []) if p][:3]:
        if len(out) >= limit:
            break
        info = images.find_person_images(name)
        for u in [info.get("portrait")] + (info.get("works") or []):
            if u and u not in out:
                out.append(u)
                if len(out) >= limit:
                    break
    return out[:limit]


def _gather_photos(event, raw=""):
    """Собрать до 4 картинок: найденные моделью + со страницы события + портреты авторов.
    Отправка сама загрузит их файлом и пропустит нерабочие, поэтому здесь только собираем."""
    out = []
    for u in _extract_photos(raw) + feature_images(event):
        if u and u not in out:
            out.append(u)
    return out[:4]


def build(event):
    # глубокий путь: веб-поиск через OpenAI или Anthropic (конкретика, прогулка, источники, фото)
    prov = feature_provider()
    if prov in ("openai", "anthropic"):
        raw = research_feature(event, prov)
        if raw:
            checked = verify_places(raw, prov) or raw   # проверка мест маршрута перед публикацией
            body = style_clean(_strip_photo_line(checked))
            return body, [], _gather_photos(event, raw)
        print("  (откат на сбор без веб-поиска)")
    # запасной путь: YandexGPT по странице события + Википедии
    page, venue_about, wiki = gather_sources(event)
    facts = verified_facts(event, page, venue_about, wiki)
    body = write_post(event, facts, wiki)
    if not body:
        return None, None, None
    return style_clean(body), wiki, _gather_photos(event)


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


def _send_to_channels(cap, bhtml, imgs):
    """Опубликовать в каналы: фото-зацеп (без подписи) + полный текст с заголовком."""
    import broadcast
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


def _dm_owner(text_html):
    """Прислать владельцу в личку (черновик на согласование)."""
    try:
        import notify_telegram as nt
        if not nt.owner_chat():
            print("TELEGRAM_OWNER_CHAT_ID не задан - черновик не отправлен.")
            return False
        for part in nt.split_chunks(text_html):
            nt.send_text(part, chat=nt.owner_chat())
        return True
    except Exception as e:
        print(f"  (черновик владельцу не ушёл: {str(e)[:100]})")
        return False


_APPROVE = {"ок", "ok", "да", "+", "ага", "норм", "хорошо", "ок.", "ok.", "+1"}


def _owner_correction(since):
    """Текстовые правки владельца после черновика (короткие 'ок' - это не правка)."""
    try:
        import notify_telegram as nt
        texts = [t.strip() for t in nt.owner_messages_since(since) if t and t.strip()]
        corr = [t for t in texts if t.lower() not in _APPROVE]
        return "\n".join(corr).strip()
    except Exception:
        return ""


REVISE_SYSTEM = (
    "Ты редактор. Тебе дают пост и правку от главного редактора. Внеси правку в пост и "
    "верни ВЕСЬ пост целиком, в том же стиле (простой язык, без рекламы, без буквы е с "
    "точками и без длинного тире). Ничего лишнего сверх правки не добавляй."
)


def apply_correction(body, correction, provider):
    user = (f"ПОСТ:\n{body}\n\nПРАВКА РЕДАКТОРА:\n{correction}\n\n"
            "Верни исправленный пост целиком.")
    try:
        if provider == "openai":
            return _openai_research(user, system=REVISE_SYSTEM)
        if provider == "anthropic":
            return _anthropic_research(user, system=REVISE_SYSTEM)
    except Exception as e:
        print(f"  (правка не применена: {str(e)[:100]})")
    return ""


def run_preview(send=False):
    """За сутки: собрать «Разбор дня» на ЗАВТРА и прислать владельцу на согласование."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    target = dt.date.today() + dt.timedelta(days=1)
    subject = pick_subject(events_for(target))
    if not subject:
        print("Разбор дня (черновик): подходящего события на завтра не нашлось.")
        return
    print(f"Черновик «Разбора» на {target}: {subject.get('title')} - {subject.get('_participant')}")
    body, wiki, imgs = build(subject)
    if not body:
        print("Разбор дня (черновик): текст не получился.")
        return
    cap = header_caption(subject)
    bhtml = body_html(subject, body, wiki)
    since = 0
    if send:
        msg = ("<b>Черновик «Разбора дня» на завтра</b>\n\n" + cap + "\n\n" + bhtml
               + "\n\n<i>Ответьте боту, что поправить, или промолчите - опубликую завтра "
               "в 14:00.</i>")
        if _dm_owner(msg):
            try:
                import notify_telegram as nt
                since = nt.current_update_id() or 0
            except Exception:
                pass
            print("Черновик отправлен владельцу в личку.")
    state = {"target": target.isoformat(), "event": subject, "body": body,
             "photos": imgs or [], "since": since}
    with open(STATE_PATH, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False, indent=1)
    print(f"Черновик сохранён: {STATE_PATH}")


def run_publish(send=False):
    """В 14:00: опубликовать утверждённый черновик (с учётом правок из лички),
    а если черновика нет - собрать свежий."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    today = dt.date.today()
    state = pub._load_json(STATE_PATH)
    if state and state.get("target") == today.isoformat():
        subject = state["event"]
        body = state["body"]
        imgs = state.get("photos") or []
        wiki = []
        corr = _owner_correction(state.get("since", 0))
        if corr:
            print(f"Правка редактора: {corr[:120]}")
            revised = apply_correction(body, corr, feature_provider())
            if revised:
                body = style_clean(_strip_photo_line(revised))
        print(f"Публикую утверждённый «Разбор дня»: {subject.get('title')}")
    else:
        subject = pick_subject(events_for(today))
        if not subject:
            print("Разбор дня: подходящего события не нашлось - пропускаем.")
            return
        print(f"Черновика нет, собираю свежий: {subject.get('title')}")
        body, wiki, imgs = build(subject)
        if not body:
            print("Разбор дня: текст не получился - пропускаем.")
            return
        imgs = imgs or []
    cap = header_caption(subject)
    bhtml = body_html(subject, body, wiki)
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
    _send_to_channels(cap, bhtml, imgs)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--preview", action="store_true",
                    help="собрать «Разбор» на ЗАВТРА и прислать владельцу на согласование")
    ap.add_argument("--sample", action="store_true", help="напечатать, не отправляя")
    ap.add_argument("--send", action="store_true", help="опубликовать / отправить владельцу")
    args = ap.parse_args()
    do_send = args.send and not args.sample
    if args.preview:
        run_preview(send=do_send)
    else:
        run_publish(send=do_send)


if __name__ == "__main__":
    main()
