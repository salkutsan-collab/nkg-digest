# -*- coding: utf-8 -*-
"""
Чтение пабликов ВКонтакте - второй источник для стрит-арт радара.

Зачем ВК: уличное искусство Петербурга живет там активнее, чем в телеграме.
Телеграм-каналы читаются без всякого доступа (веб-витрина t.me/s), а ВК так не
умеет: страница сообщества без входа отдает только каркас, без текстов постов.
Поэтому нужен ключ доступа к API.

Как получить ключ (делает владелец, один раз):
  1. dev.vk.com -> «Мои приложения» -> создать приложение типа «Standalone».
  2. В настройках приложения скопировать «Сервисный ключ доступа».
  3. Положить его в .env как VK_TOKEN=... и в секреты GitHub тем же именем.
  4. Проверить: py agents/vk_source.py --check

Если сервисный ключ не пустят к стене сообщества (ВК отвечает ошибкой 15 или 5),
нужен ключ пользователя с правом wall - об этом скажет та же проверка.

Запуск (Windows - py):
  py agents/vk_source.py --check                    # ключ на месте и работает?
  py agents/vk_source.py --groups a,b,c             # что это за группы, годятся ли
  py agents/vk_source.py --posts lenstreet          # последние посты группы
"""

import os
import re
import sys
import time
import argparse
import datetime as dt

import requests

API = "https://api.vk.com/method/{method}"
VERSION = "5.199"
PAUSE = 0.34          # у сервисного ключа предел 3 запроса в секунду

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# понятные пояснения к кодам ошибок ВК
ERRORS = {
    5: "ключ не подошел (истек или не того типа) - нужен новый",
    15: "доступ закрыт: сервисный ключ к этому методу не пускают, нужен ключ "
        "пользователя с правом wall",
    6: "слишком часто - подождать и повторить",
    18: "страница удалена или заблокирована",
    30: "профиль или сообщество закрыты",
    100: "неверные параметры запроса",
    203: "нет доступа к сообществу",
}


def token():
    return os.environ.get("VK_TOKEN") or os.environ.get("VK_SERVICE_TOKEN") or ""


def configured():
    """Есть ли ключ - чтобы радар молча пропускал ВК, если он не настроен."""
    return bool(token())


class VkError(RuntimeError):
    def __init__(self, code, message):
        self.code = code
        hint = ERRORS.get(code, "")
        super().__init__(f"ВК ошибка {code}: {message}" + (f" ({hint})" if hint else ""))


def api(method, tries=3, **params):
    """Вызов метода API. Ошибку ВК поднимаем понятным сообщением."""
    if not token():
        raise VkError(0, "не задан VK_TOKEN (см. начало файла)")
    params.update({"access_token": token(), "v": VERSION})
    last = None
    for attempt in range(tries):
        try:
            r = requests.get(API.format(method=method), params=params, timeout=30)
            data = r.json()
        except Exception as e:
            last = VkError(0, f"сеть: {str(e)[:80]}")
            time.sleep(1.5)
            continue
        err = data.get("error")
        if not err:
            time.sleep(PAUSE)
            return data.get("response")
        code = err.get("error_code")
        last = VkError(code, err.get("error_msg", ""))
        if code == 6 and attempt < tries - 1:     # слишком часто
            time.sleep(1.5)
            continue
        raise last
    raise last


# ---------- сообщества ----------

def group_info(domains):
    """Название, число подписчиков и описание сообществ. Список словарей."""
    if not domains:
        return []
    out = []
    # groups.getById берет до 500 адресов за раз, но ответ режем по 100 для надежности
    for i in range(0, len(domains), 100):
        chunk = domains[i:i + 100]
        try:
            resp = api("groups.getById", group_ids=",".join(chunk),
                       fields="members_count,description,activity")
        except VkError as e:
            print(f"  ({e})")
            continue
        groups = resp.get("groups") if isinstance(resp, dict) else resp
        for g in groups or []:
            out.append({
                "domain": g.get("screen_name"),
                "id": g.get("id"),
                "name": g.get("name"),
                "members": g.get("members_count"),
                "activity": g.get("activity") or "",
                "about": re.sub(r"\s+", " ", g.get("description") or "")[:200],
                "closed": bool(g.get("is_closed")),
            })
    return out


# ---------- посты ----------

def _post_text(post):
    """Текст поста вместе с текстом репоста: паблики часто репостят художников."""
    parts = [post.get("text") or ""]
    for rep in post.get("copy_history") or []:
        parts.append(rep.get("text") or "")
    return "\n".join(p for p in parts if p).strip()


def fetch_posts(domain, limit=30):
    """Последние посты сообщества в том же виде, что дает телеграм-витрина:
    {text, url, date}. При ошибке возвращаем пустой список - радар не должен падать."""
    try:
        resp = api("wall.get", domain=domain, count=min(limit, 100), filter="owner")
    except VkError as e:
        print(f"  (ВК {domain}: {e})")
        return []
    out = []
    for p in (resp or {}).get("items", []):
        text = _post_text(p)
        if not text:
            continue
        owner = p.get("owner_id")
        pid = p.get("id")
        day = None
        if p.get("date"):
            try:
                day = dt.datetime.fromtimestamp(p["date"]).date()
            except Exception:
                day = None
        out.append({"text": text,
                    "url": f"https://vk.com/wall{owner}_{pid}",
                    "date": day})
    return out


# ---------- поиск по всему ВК ----------

_city_cache = {}


def _owner_city(owner_ids):
    """Город сообществ и людей по их id. Нужно, чтобы отсечь чужие города:
    в поиске ВК полно постов из Перми и Красноярска, где Петербург просто
    упомянут в перечислении."""
    todo = [o for o in set(owner_ids) if o not in _city_cache]
    groups = [str(-o) for o in todo if o < 0]
    users = [str(o) for o in todo if o > 0]
    for i in range(0, len(groups), 100):
        try:
            resp = api("groups.getById", group_ids=",".join(groups[i:i + 100]),
                       fields="city,members_count")
        except VkError as e:
            print(f"  ({e})")
            continue
        items = resp.get("groups") if isinstance(resp, dict) else resp
        for g in items or []:
            _city_cache[-g["id"]] = ((g.get("city") or {}).get("title"), g.get("name"))
    for i in range(0, len(users), 100):
        try:
            resp = api("users.get", user_ids=",".join(users[i:i + 100]), fields="city")
        except VkError as e:
            print(f"  ({e})")
            continue
        for u in resp or []:
            name = " ".join(x for x in (u.get("first_name"), u.get("last_name")) if x)
            _city_cache[u["id"]] = ((u.get("city") or {}).get("title"), name)
    for o in todo:
        _city_cache.setdefault(o, (None, None))
    return _city_cache


def search_posts(queries, days=14, limit=60, keywords=(), deny=(), spb_hints=()):
    """Поиск свежих постов по всему ВК с отбором.

    Порядок отбора: слово нашей темы -> признак Петербурга -> стоп-слова
    (реклама, закупки, детские конкурсы) -> склейка повторов (один пост часто
    размножен по десяти группам) -> город автора должен быть Петербург.
    Возвращает посты в том же виде, что и остальные источники."""
    since = int(time.time()) - days * 86400
    kw = [w.lower() for w in keywords]
    dn = [w.lower() for w in deny]
    hints = [h.lower() for h in spb_hints]
    found = {}
    for q in queries:
        try:
            resp = api("newsfeed.search", q=q, count=100, start_time=since, extended=1)
        except VkError as e:
            print(f"  (ВК поиск «{q}»: {e})")
            continue
        for p in (resp or {}).get("items", []):
            text = _post_text(p)
            low = text.lower()
            if kw and not any(w in low for w in kw):
                continue
            if hints and not any(h in low for h in hints):
                continue
            if any(w in low for w in dn):
                continue
            key = re.sub(r"\W+", "", low)[:120]
            if key in found:
                continue
            found[key] = p
    if not found:
        return []
    cities = _owner_city([p.get("owner_id") for p in found.values()])
    out = []
    for p in found.values():
        owner = p.get("owner_id")
        town, who = cities.get(owner, (None, None))
        text = _post_text(p)
        if town and "петербург" not in town.lower():
            continue
        if not town and not any(h in text[:200].lower() for h in hints):
            continue        # город не указан - требуем Петербург в начале поста
        day = None
        if p.get("date"):
            try:
                day = dt.datetime.fromtimestamp(p["date"]).date()
            except Exception:
                day = None
        out.append({"text": text,
                    "url": f"https://vk.com/wall{owner}_{p.get('id')}",
                    "date": day,
                    "_author": who or f"id{owner}"})
    out.sort(key=lambda p: p["date"] or dt.date.min, reverse=True)
    return out[:limit]


# ---------- проверка руками ----------

def _keywords():
    """Слова-признаки из настроек радара - ими оцениваем, о том ли группа."""
    try:
        from ruamel.yaml import YAML
        y = YAML(typ="safe")
        with open(os.path.join(ROOT, "data", "streetart_sources.yaml"),
                  encoding="utf-8") as fh:
            return (y.load(fh) or {}).get("keywords") or []
    except Exception:
        return ["мурал", "стрит-арт", "арт-объект", "инсталляц"]


def check():
    """Работает ли ключ и к чему он пускает."""
    if not configured():
        print("VK_TOKEN не задан. Как получить ключ - в начале файла "
              "agents/vk_source.py.")
        return False
    print("Ключ найден. Проверяю доступ...")
    try:
        info = group_info(["lenstreet"])
        print(f"  groups.getById: работает ({info[0]['name'] if info else 'пусто'})")
    except VkError as e:
        print(f"  groups.getById: {e}")
        return False
    posts = fetch_posts("lenstreet", limit=3)
    if posts:
        print(f"  wall.get: работает, последний пост {posts[0]['date']}")
        print(f"    {posts[0]['text'][:120]}")
        return True
    print("  wall.get: постов не отдал. Если выше ошибка 15 или 5 - нужен ключ "
          "пользователя с правом wall, сервисного мало.")
    return False


def rate_groups(domains):
    """Опознать группы и оценить, годятся ли они радару."""
    words = [w.lower() for w in _keywords()]
    infos = {g["domain"]: g for g in group_info(domains) if g.get("domain")}
    print(f"\nОпознано сообществ: {len(infos)} из {len(domains)}\n")
    for d in domains:
        g = infos.get(d)
        if not g:
            print(f"  {d}: не опознано")
            continue
        posts = fetch_posts(d, limit=20)
        hits = sum(1 for p in posts if any(w in p["text"].lower() for w in words))
        spb = sum(1 for p in posts
                  if any(h in p["text"].lower()
                         for h in ("петербург", "спб", "питер", "ленинград")))
        last = max((p["date"] for p in posts if p["date"]), default=None)
        print(f"  vk.com/{d}")
        print(f"    «{g['name']}», подписчиков {g['members']}, закрыта: {g['closed']}")
        print(f"    постов прочитано {len(posts)}, свежесть {last}, "
              f"по теме {hits}, про Петербург {spb}")
        if g["about"]:
            print(f"    {g['about'][:120]}")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    from llm import _load_dotenv
    _load_dotenv()

    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="проверить ключ доступа")
    ap.add_argument("--groups", help="адреса сообществ через запятую - опознать и оценить")
    ap.add_argument("--posts", help="показать последние посты сообщества")
    args = ap.parse_args()

    if args.check:
        check()
        return
    if args.groups:
        rate_groups([d.strip().strip("/").split("/")[-1]
                     for d in args.groups.split(",") if d.strip()])
        return
    if args.posts:
        for p in fetch_posts(args.posts, limit=10):
            print(f"\n{p['date']}  {p['url']}")
            print("  " + re.sub(r"\s+", " ", p["text"])[:200])
        return
    ap.print_help()


if __name__ == "__main__":
    main()
