# -*- coding: utf-8 -*-
"""
Поиск картинок персоны через Википедию и Викисклад (без ключей).

Возвращает портрет и несколько изображений со страницы человека.
Это "лучшее усилие": если в Википедии о человеке мало - вернет мало или ничего.
Картинки потом показываются владельцу на одобрение, так что неточность не страшна.
"""

from urllib.parse import urljoin, urlparse, urlsplit, urlunsplit, quote

import requests
from bs4 import BeautifulSoup

WIKI_API = "https://ru.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "NKG-Digest/1.0 (culture digest)"}

# Что не берем в работы (служебное, иконки, флаги и т. п.)
SKIP = ("logo", "icon", "flag", "commons-logo", "wiki", "edit", "ar.svg",
        "map", "gnome", "symbol", "blank", "default", "placeholder")


def _get(params):
    p = {"format": "json", "formatversion": "2"}
    p.update(params)
    try:
        r = requests.get(WIKI_API, params=p, headers=HEADERS, timeout=20)
        return r.json()
    except Exception:
        return {}


def _find_title(name):
    """Найти страницу человека в русской Википедии."""
    d = _get({"action": "query", "list": "search", "srsearch": name, "srlimit": 1})
    hits = (d.get("query") or {}).get("search") or []
    return hits[0]["title"] if hits else None


def _portrait(title):
    d = _get({"action": "query", "prop": "pageimages", "titles": title,
              "piprop": "thumbnail", "pithumbsize": 800})
    pages = (d.get("query") or {}).get("pages") or []
    for p in pages:
        thumb = (p.get("thumbnail") or {}).get("source")
        if thumb:
            return thumb
    return None


def _page_image_urls(title, limit=12):
    """Картинки, использованные на странице (File:...), -> прямые ссылки."""
    d = _get({"action": "query", "prop": "images", "titles": title, "imlimit": 40})
    pages = (d.get("query") or {}).get("pages") or []
    files = []
    for p in pages:
        for im in p.get("images", []):
            t = im.get("title", "")
            low = t.lower()
            if not (low.endswith(".jpg") or low.endswith(".jpeg") or low.endswith(".png")):
                continue
            if any(s in low for s in SKIP):
                continue
            files.append(t)
    files = files[:limit]
    if not files:
        return []
    d2 = _get({"action": "query", "titles": "|".join(files),
               "prop": "imageinfo", "iiprop": "url", "iiurlwidth": 1000})
    pages2 = (d2.get("query") or {}).get("pages") or []
    urls = []
    for p in pages2:
        info = (p.get("imageinfo") or [])
        if info:
            url = info[0].get("thumburl") or info[0].get("url")
            if url:
                urls.append(url)
    return urls


def find_person_images(name, max_works=4):
    """Вернуть {'portrait': url|None, 'works': [url,...], 'title': заголовок|None}."""
    title = _find_title(name)
    if not title:
        return {"portrait": None, "works": [], "title": None}
    portrait = _portrait(title)
    all_urls = _page_image_urls(title)
    works = [u for u in all_urls if u != portrait][:max_works]
    return {"portrait": portrait, "works": works, "title": title}


def find_page_image(url):
    """Главная картинка со страницы события и название источника.

    Возвращает (image_url|None, source_name). Картинку берём из og:image
    (или twitter:image), источник - из og:site_name или домена сайта.
    """
    if not url:
        return None, None
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        soup = BeautifulSoup(r.text, "html.parser")
    except Exception:
        return None, _domain(url)

    candidates = []
    for sel in [("meta", {"property": "og:image"}),
                ("meta", {"name": "twitter:image"}),
                ("meta", {"property": "og:image:url"})]:
        tag = soup.find(*sel)
        if tag and tag.get("content"):
            candidates.append(urljoin(url, tag["content"].strip()))
    for im in soup.find_all("img", src=True):
        candidates.append(urljoin(url, im["src"].strip()))

    img = next((c for c in candidates if _usable_image(c)), None)
    if img:
        img = _encode(img)

    site = soup.find("meta", property="og:site_name")
    source = site["content"].strip() if (site and site.get("content")) else _domain(url)
    return img, source


def _encode(u):
    """Закодировать пробелы и кириллицу в пути ссылки (чтобы Telegram загрузил)."""
    try:
        p = urlsplit(u)
        return urlunsplit((p.scheme, p.netloc, quote(p.path), quote(p.query, safe="=&"), ""))
    except Exception:
        return u


def _usable_image(u):
    """Годится ли картинка для отправки (http, не пиксель/логотип/data)."""
    if not u or not u.lower().startswith("http"):
        return False
    low = u.lower()
    bad = ("mc.yandex", "/watch", "pixel", "spacer", "1x1", "blank",
           "logo", "icon", "sprite", "favicon", "counter", ".svg")
    if any(b in low for b in bad):
        return False
    return any(ext in low for ext in (".jpg", ".jpeg", ".png", ".webp", ".gif"))


def _domain(url):
    try:
        return urlparse(url).netloc.replace("www.", "")
    except Exception:
        return url


if __name__ == "__main__":
    import sys
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    arg = " ".join(sys.argv[1:]) or "Виталий Пушницкий"
    if arg.startswith("http"):
        print(find_page_image(arg))
    else:
        print(find_person_images(arg))
