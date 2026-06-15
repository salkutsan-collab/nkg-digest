# -*- coding: utf-8 -*-
"""
Агент 1 - "Реестр участников НКГ".

Что делает:
  1. Проверяет ссылки в базе (data/participants.yaml): живы ли сайты,
     куда ведут редиректы.
  2. На сайтах участников ищет раздел с афишей/событиями и соцсети
     (ВКонтакте, Telegram) - и предлагает добавить их в базу.
  3. Обрабатывает "входящие" (data/inbox.md) - правки от человека
     обычным текстом (если задан ключ ANTHROPIC_API_KEY).

Запуск (на Windows используйте py):
  py agents/agent1_registry.py            # проверить и записать отчет, базу не менять
  py agents/agent1_registry.py --apply    # ещё и внести безопасные правки в базу

Отчет всегда пишется в data/agent1_report.md
"""

import sys
import os
import re
import argparse
import concurrent.futures as cf
from urllib.parse import urljoin, urlparse

import json

import requests
from bs4 import BeautifulSoup
from ruamel.yaml import YAML

import llm

# Шапка файла базы (комментарии сверху)
HEADER = """\
# Реестр участников Новой культурной географии (НКГ), Санкт-Петербург
#
# Этот файл - "база участников". Его ведет агент 1 (поиск и обновление),
# но можно править и руками: добавлять, убирать, исправлять ссылки.
#
# Поля каждого участника:
#   id        - короткий код (латиницей, без пробелов)
#   name      - название
#   category  - тип: креативное пространство | музей | галерея | стрит-арт |
#               дизайн-центр | образование | фестиваль | театр | кино | гастрономия
#   address   - адрес
#   tags      - темы (для отбора в дайджест)
#   events_url- страница афиши/событий (подсказка для агента 2)
#   sources   - откуда брать новости. type: website | vk | telegram | timepad | instagram
#   status    - active | needs_verification | dead
#
# Файл переписывается агентом 1 в едином формате; группировка по категориям - автоматическая.
"""

CAT_ORDER = ["креативное пространство", "музей", "галерея", "стрит-арт",
             "дизайн-центр", "образование", "фестиваль", "театр", "кино",
             "гастрономия"]

# --- пути ---
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BASE_PATH = os.path.join(ROOT, "data", "participants.yaml")
INBOX_PATH = os.path.join(ROOT, "data", "inbox.md")
REPORT_PATH = os.path.join(ROOT, "data", "agent1_report.md")

# Слова, по которым узнаем раздел с афишей/событиями на сайте
AFISHA_KEYS = [
    "афиш", "событи", "расписан", "выставк", "программ", "календ",
    "анонс", "мероприят", "exhibition", "event", "schedule",
    "calendar", "agenda", "whatson", "what-s-on", "afisha",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; NKG-Registry/1.0; +digest)"
}


def fetch(url, timeout=15):
    """Скачать страницу. Возвращает статус, конечный URL и html (для сайтов)."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        ctype = r.headers.get("Content-Type", "")
        html = r.text if "text/html" in ctype else ""
        return {
            "ok": r.status_code < 400,
            "status": r.status_code,
            "final_url": r.url,
            "html": html,
            "error": None,
        }
    except Exception as e:
        return {"ok": False, "status": None, "final_url": url, "html": "", "error": str(e)[:200]}


def _clean_social(url):
    """Отсеять служебные ссылки ВК/Telegram (share, widget, статьи и т. п.)."""
    url = url.replace("http://", "https://")
    low = url.lower()
    junk = ["share", "widget", "away.php", "joinchat", "video_ext",
            "method=", "?w=", "/wall", "/photo", "/topic"]
    if any(j in low for j in junk):
        return None
    if urlparse(url).path.startswith("/@"):   # vk.com/@... - это статья, не сообщество
        return None
    return url.rstrip("/")


def handle_of(url):
    """Имя сообщества/канала из ссылки (только буквы и цифры, в нижнем регистре)."""
    seg = urlparse(url).path.strip("/").split("/")[0]
    return re.sub(r"[^a-z0-9]", "", seg.lower())


def social_matches(pid, url):
    """Похожа ли соцсеть на участника (его код есть в имени канала)?"""
    h = handle_of(url)
    tokens = [t for t in pid.split("-") if len(t) >= 4]
    return any(t in h for t in tokens) if tokens else False


def discover(html, base_url):
    """Найти на странице раздел афиши и ссылки на соцсети."""
    afisha, vk, tg, ig = [], [], [], []
    if not html:
        return {"afisha": [], "vk": [], "telegram": [], "instagram": []}
    soup = BeautifulSoup(html, "html.parser")
    base_host = urlparse(base_url).netloc.lower().replace("www.", "")
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if not href or href.startswith(("#", "mailto:", "tel:", "javascript:")):
            continue
        full = urljoin(base_url, href)
        host = urlparse(full).netloc.lower()
        text = a.get_text(" ", strip=True).lower()
        probe = (href + " " + text).lower()
        if "vk.com" in host:
            c = _clean_social(full)
            if c:
                vk.append(c)
        elif "t.me" in host or "telegram.me" in host:
            c = _clean_social(full)
            if c:
                tg.append(c)
        elif "instagram.com" in host:
            c = _clean_social(full)
            if c:
                ig.append(c)
        elif urlparse(full).netloc.lower().replace("www.", "") == base_host:
            if any(k in probe for k in AFISHA_KEYS):
                afisha.append(full.split("#")[0].rstrip("/"))

    def uniq(seq, limit):
        seen, out = set(), []
        for x in seq:
            if x not in seen:
                seen.add(x)
                out.append(x)
            if len(out) >= limit:
                break
        return out

    return {
        "afisha": uniq(afisha, 3),
        "vk": uniq(vk, 2),
        "telegram": uniq(tg, 2),
        "instagram": uniq(ig, 1),
    }


def existing_urls(participant):
    return {s.get("url", "").rstrip("/").lower() for s in participant.get("sources", [])}


def process_participant(p):
    """Проверить все источники участника и собрать находки."""
    finding = {
        "id": p.get("id"),
        "name": p.get("name"),
        "checks": [],          # (type, url, ok, status, final_url, error)
        "redirects": [],       # (url, final_url)
        "dead": [],            # url
        "found": {"vk": [], "telegram": [], "afisha": [], "instagram": []},
    }
    have = existing_urls(p)
    for s in p.get("sources", []):
        url = s.get("url")
        if not url:
            continue
        res = fetch(url)
        finding["checks"].append((s.get("type"), url, res["ok"], res["status"],
                                  res["final_url"], res["error"]))
        if not res["ok"]:
            finding["dead"].append(url)
            continue
        if res["final_url"].rstrip("/").lower() != url.rstrip("/").lower():
            finding["redirects"].append((url, res["final_url"]))
        if s.get("type") == "website" and res["html"]:
            d = discover(res["html"], res["final_url"])
            for vk in d["vk"]:
                if vk.lower() not in have:
                    finding["found"]["vk"].append(vk)
            for tg in d["telegram"]:
                if tg.lower() not in have:
                    finding["found"]["telegram"].append(tg)
            finding["found"]["afisha"].extend(d["afisha"])
            finding["found"]["instagram"].extend(d["instagram"])
    return finding


def apply_fixes(base, findings):
    """Внести безопасные правки в структуру базы (in place)."""
    by_id = {p.get("id"): p for p in base["participants"]}
    changed = 0
    for f in findings:
        p = by_id.get(f["id"])
        if p is None:
            continue
        # 1) починить редиректы - только у сайтов (у соцсетей не трогаем,
        #    иначе vk.com превращается в m.vk.com и числовые id)
        for old, new in f["redirects"]:
            for s in p["sources"]:
                if s.get("type") != "website":
                    continue
                if s.get("url", "").rstrip("/").lower() == old.rstrip("/").lower():
                    s["url"] = new.rstrip("/")
                    if "verify" in s:
                        del s["verify"]
                    changed += 1
        # 2) добавить найденные соцсети - только если они похожи на участника
        have = existing_urls(p)
        for kind, key in (("vk", "vk"), ("telegram", "telegram")):
            for url in f["found"][key]:
                if not social_matches(f["id"], url):
                    continue
                if url.rstrip("/").lower() in have:
                    continue
                p["sources"].append({"type": kind, "url": url})
                have.add(url.rstrip("/").lower())
                changed += 1
                break  # достаточно одного достоверного канала на тип
        # 3) записать раздел афиши (подсказка для агента 2)
        if f["found"]["afisha"] and not p.get("events_url"):
            p["events_url"] = f["found"]["afisha"][0]
            changed += 1
        # 4) обновить статус
        any_ok = any(c[2] for c in f["checks"])
        all_verified = all("verify" not in s for s in p["sources"])
        if any_ok and all_verified:
            p["status"] = "active"
        elif not any_ok:
            p["status"] = "dead"
    return changed


def read_inbox():
    if not os.path.exists(INBOX_PATH):
        return []
    lines = []
    with open(INBOX_PATH, encoding="utf-8") as fh:
        body = fh.read()
    # берем только то, что после линии-разделителя "---"
    if "---" in body:
        body = body.split("---", 1)[1]
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", "<!--", ">")):
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        lines.append(s)
    return lines


INBOX_TEMPLATE = """\
# Входящие для агента 1 (правки базы участников)

Пишите сюда обычным текстом, что изменить в базе. Агент 1 прочитает,
внесет правки в `participants.yaml` и очистит этот файл.

Примеры (просто текстом, в свободной форме):

- добавь галерею Name Gallery, сайт name-gallery.ru
- добавь мастерскую такую-то на Васильевском
- у Севкабеля поменялся адрес страницы афиши
- убери из базы DiDi Gallery

---
<!-- Пишите ваши правки ниже этой линии -->
"""

_TRANSLIT = {
    "а": "a", "б": "b", "в": "v", "г": "g", "д": "d", "е": "e", "ж": "zh",
    "з": "z", "и": "i", "й": "y", "к": "k", "л": "l", "м": "m", "н": "n",
    "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u", "ф": "f",
    "х": "h", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "sch", "ы": "y", "э": "e",
    "ю": "yu", "я": "ya", "ъ": "", "ь": "",
}


def slug(name, taken):
    """Короткий латинский id из названия, уникальный относительно taken."""
    s = "".join(_TRANSLIT.get(ch, ch) for ch in str(name).lower())
    s = re.sub(r"[^a-z0-9]+", "-", s).strip("-")[:30] or "place"
    base = s
    i = 2
    while s in taken:
        s = f"{base}-{i}"
        i += 1
    return s


INBOX_SYSTEM = (
    "Ты помощник, который превращает заметки человека о культурных площадках "
    "Петербурга в структурированные действия над базой. Отвечай ТОЛЬКО валидным "
    "JSON-массивом без пояснений."
)


def _parse_actions(raw):
    m = re.search(r"\[.*\]", raw, re.S)
    if not m:
        return []
    try:
        data = json.loads(m.group(0))
    except Exception:
        return []
    return data if isinstance(data, list) else []


def _apply_fields(p, a):
    """Записать в участника поля из действия (только заданные)."""
    if a.get("category"):
        p["category"] = a["category"]
    if a.get("address"):
        p["address"] = a["address"]
    if a.get("events_url"):
        p["events_url"] = a["events_url"]
    if a.get("tags"):
        p["tags"] = a["tags"]
    if a.get("website"):
        have = {s.get("url", "").rstrip("/").lower() for s in p.get("sources", [])}
        if a["website"].rstrip("/").lower() not in have:
            p.setdefault("sources", []).append(
                {"type": "website", "url": a["website"], "verify": True})


def apply_inbox(base, lines):
    """Применить текстовые правки из входящих моделью. Возвращает (счетчик, лог)."""
    if not lines:
        return 0, []
    cats = ("креативное пространство | музей | галерея | стрит-арт | дизайн-центр | "
            "образование | фестиваль | театр | кино | гастрономия")
    prompt = (
        "Вот заметки человека о площадках (по одной в строке). Для каждой определи "
        "действие над базой и верни JSON-массив объектов с полями:\n"
        '  "action"     - "add" | "update" | "remove",\n'
        '  "name"       - название площадки,\n'
        f'  "category"   - одна из: {cats} (или null),\n'
        '  "address"    - адрес или null,\n'
        '  "events_url" - страница афиши/событий или null,\n'
        '  "website"    - сайт или null,\n'
        '  "tags"       - массив тем или [].\n'
        "Заметки:\n" + "\n".join(f"- {x}" for x in lines)
    )
    try:
        raw = llm.chat(INBOX_SYSTEM, prompt, temperature=0.1, max_tokens=1500)
    except Exception as e:
        print(f"  входящие: ошибка модели: {str(e)[:120]}")
        return 0, []
    actions = _parse_actions(raw)
    by_name = {p["name"].lower(): p for p in base["participants"]}
    taken = {p["id"] for p in base["participants"]}
    n, log = 0, []
    for a in actions:
        if not isinstance(a, dict):
            continue
        act = (a.get("action") or "").lower()
        name = (a.get("name") or "").strip()
        if not name:
            continue
        target = by_name.get(name.lower())
        if act == "remove":
            if target:
                base["participants"].remove(target)
                n += 1
                log.append(f"убрана: {name}")
            continue
        if act == "update" and target:
            _apply_fields(target, a)
            n += 1
            log.append(f"обновлена: {name}")
            continue
        # add (или update несуществующей -> добавляем)
        pid = slug(name, taken)
        taken.add(pid)
        p = {"id": pid, "name": name, "category": a.get("category") or "галерея",
             "tags": [], "sources": [], "status": "needs_verification"}
        _apply_fields(p, a)
        base["participants"].append(p)
        by_name[name.lower()] = p
        n += 1
        log.append(f"добавлена: {name} (id {pid})")
    return n, log


def clear_inbox():
    with open(INBOX_PATH, "w", encoding="utf-8") as fh:
        fh.write(INBOX_TEMPLATE)


def write_report(findings, inbox, applied):
    out = []
    out.append("# Отчет агента 1 (реестр НКГ)\n")
    total = len(findings)
    dead = [f for f in findings if not any(c[2] for c in f["checks"])]
    redir = [f for f in findings if f["redirects"]]
    enrich = [f for f in findings if f["found"]["vk"] or f["found"]["telegram"] or f["found"]["afisha"]]
    out.append(f"Проверено участников: {total}\n")
    out.append(f"Недоступны (надо разобраться): {len(dead)}\n")
    out.append(f"С редиректами (поправлен адрес): {len(redir)}\n")
    out.append(f"Можно дополнить (соцсети/афиша): {len(enrich)}\n")
    if applied is not None:
        out.append(f"Автоправок внесено: {applied}\n")
    else:
        out.append("Режим проверки: база НЕ менялась (запустите с --apply, чтобы внести правки)\n")

    if dead:
        out.append("\n## Недоступные ссылки\n")
        for f in dead:
            out.append(f"- **{f['name']}** ({f['id']})")
            for t, url, ok, st, fin, err in f["checks"]:
                if not ok:
                    out.append(f"  - {t}: {url} -> {err or st}")

    out.append("\n## Находки по участникам\n")
    for f in findings:
        bits = []
        if f["redirects"]:
            bits.append(f"редиректы: {len(f['redirects'])}")
        if f["found"]["vk"]:
            bits.append("ВК: " + ", ".join(f["found"]["vk"]))
        if f["found"]["telegram"]:
            bits.append("TG: " + ", ".join(f["found"]["telegram"]))
        if f["found"]["afisha"]:
            bits.append("афиша: " + f["found"]["afisha"][0])
        if bits:
            out.append(f"- **{f['name']}**: " + "; ".join(bits))

    out.append("\n## Входящие (правки от человека)\n")
    if inbox:
        out.append("Найдены строки во входящих. Для автоприменения нужен ключ модели "
                   "(запустите с --apply при заданном ключе):\n")
        for s in inbox:
            out.append(f"- {s}")
    else:
        out.append("Пусто или уже применено.")

    with open(REPORT_PATH, "w", encoding="utf-8") as fh:
        fh.write("\n".join(out) + "\n")


def load_base(path):
    """Загрузить базу в обычные dict/list (без привязки к формату файла)."""
    y = YAML(typ="safe")
    with open(path, encoding="utf-8") as fh:
        return y.load(fh)


def _scalar(v):
    s = str(v)
    if s == "":
        return '""'
    if any(c in s for c in (":", "#", '"', "'", ", ")) or s[0] in "[]{}>|*&!%@`-?":
        return '"' + s.replace('"', '\\"') + '"'
    return s


def dump_base(base, path):
    """Записать базу в едином аккуратном виде, сгруппировав по категориям."""
    L = [HEADER, "meta:"]
    for k, v in (base.get("meta") or {}).items():
        if isinstance(v, list):
            L.append(f"  {k}: [{', '.join(_scalar(x) for x in v)}]")
        else:
            L.append(f"  {k}: {_scalar(v)}")
    L.append("")
    L.append("participants:")

    def cat_key(p):
        c = p.get("category", "")
        return CAT_ORDER.index(c) if c in CAT_ORDER else len(CAT_ORDER)

    ordered = sorted(base["participants"], key=cat_key)
    last_cat = None
    for p in ordered:
        c = p.get("category", "")
        if c != last_cat:
            L.append("")
            L.append(f"  # ---------- {c} ----------")
            last_cat = c
        L.append("")
        L.append(f"  - id: {_scalar(p.get('id'))}")
        L.append(f"    name: {_scalar(p.get('name'))}")
        L.append(f"    category: {_scalar(c)}")
        if p.get("address"):
            L.append(f"    address: {_scalar(p['address'])}")
        if p.get("tags"):
            L.append(f"    tags: [{', '.join(_scalar(t) for t in p['tags'])}]")
        if p.get("events_url"):
            L.append(f"    events_url: {_scalar(p['events_url'])}")
        L.append("    sources:")
        for s in p.get("sources", []):
            inner = f"type: {s.get('type')}, url: \"{s.get('url')}\""
            if s.get("verify"):
                inner += ", verify: true"
            L.append(f"      - {{{inner}}}")
        L.append(f"    status: {_scalar(p.get('status', 'active'))}")

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


def _dm_owner_changes(log):
    """Написать владельцу в личку, что изменилось в базе (кого добавили и т. п.)."""
    def names(prefix):
        out = []
        for l in log:
            if l.startswith(prefix):
                out.append(l.split(":", 1)[1].split("(id")[0].strip())
        return out
    added, updated, removed = names("добавлена"), names("обновлена"), names("убрана")
    if not (added or updated or removed):
        return
    parts = []
    if added:
        parts.append("добавлены: " + ", ".join(added))
    if updated:
        parts.append("обновлены: " + ", ".join(updated))
    if removed:
        parts.append("убраны: " + ", ".join(removed))
    msg = "<b>Обновление базы участников</b>\n" + "; ".join(parts) + "."
    try:
        import notify_telegram as nt
        if nt.owner_chat():
            nt.send_text(msg, chat=nt.owner_chat())
            print("Владельцу отправлено сообщение об изменениях базы.")
        else:
            print("TELEGRAM_OWNER_CHAT_ID не задан - сообщение об изменениях не ушло.")
    except Exception as e:
        print(f"  (сообщение владельцу не ушло: {str(e)[:100]})")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="внести безопасные правки в базу")
    ap.add_argument("--inbox-only", action="store_true",
                    help="только применить входящие моделью, без проверки ссылок")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    base = load_base(BASE_PATH)

    # быстрый режим: только применить текстовые правки из входящих
    if args.inbox_only:
        inbox = read_inbox()
        if not inbox:
            print("Входящие пусты.")
            return
        if not llm.available():
            print("Нет ключа модели - не могу разобрать входящие.")
            return
        n, log = apply_inbox(base, inbox)
        for line in log:
            print("  -", line)
        if n:
            dump_base(base, BASE_PATH)
            clear_inbox()
        print(f"Входящие применены: {n}. Площадок в базе: {len(base['participants'])}")
        return

    participants = list(base["participants"])
    print(f"Участников в базе: {len(participants)}")
    print("Проверяю ссылки...")

    findings = []
    with cf.ThreadPoolExecutor(max_workers=args.workers) as ex:
        for f in ex.map(process_participant, participants):
            findings.append(f)
            ok = sum(1 for c in f["checks"] if c[2])
            print(f"  {f['name']}: источников ок {ok}/{len(f['checks'])}"
                  + ("  [редирект]" if f["redirects"] else "")
                  + ("  [+соцсети]" if (f['found']['vk'] or f['found']['telegram']) else ""))

    inbox = read_inbox()
    applied = None
    if args.apply:
        applied = apply_fixes(base, findings)
        inbox_log = []
        if inbox and llm.available():
            n, log = apply_inbox(base, inbox)
            inbox_log = log
            for line in log:
                print("  входящие:", line)
            if n:
                clear_inbox()
                inbox = []  # уже применили
            print(f"Из входящих применено правок: {n}")
        elif inbox:
            print("Во входящих есть строки, но нет ключа модели - пропускаю.")
        dump_base(base, BASE_PATH)
        print(f"Внесено правок в базу (ссылки): {applied}")
        _dm_owner_changes(inbox_log)  # написать владельцу, кого добавили/обновили/убрали

    write_report(findings, inbox, applied)
    print(f"Отчет: {REPORT_PATH}")


if __name__ == "__main__":
    main()
