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

import requests
from bs4 import BeautifulSoup
from ruamel.yaml import YAML

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
        out.append("Найдены строки во входящих. Для автоприменения нужен ключ ANTHROPIC_API_KEY (этап 2):\n")
        for s in inbox:
            out.append(f"- {s}")
    else:
        out.append("Пусто.")

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


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="внести безопасные правки в базу")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args()

    base = load_base(BASE_PATH)
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

    applied = None
    if args.apply:
        applied = apply_fixes(base, findings)
        dump_base(base, BASE_PATH)
        print(f"Внесено правок в базу: {applied}")

    inbox = read_inbox()
    write_report(findings, inbox, applied)
    print(f"Отчет: {REPORT_PATH}")
    if inbox:
        print(f"Во входящих {len(inbox)} строк(и) - обработка текстовых правок будет на этапе 2 (с ключом модели).")


if __name__ == "__main__":
    main()
