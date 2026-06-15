# -*- coding: utf-8 -*-
"""
Отправка дайджеста в мессенджер Max (max.ru).

Max Bot API - близкий родственник Telegram/TamTam, поэтому разметку берем ту же,
что и для Telegram (HTML-теги <b>, <i>, <a href>). Конвертер разметки и разбивку
на части переиспользуем из notify_telegram - один источник правды.

Нужны два значения в .env (или в секретах GitHub):
  MAX_BOT_TOKEN=...   (токен от @MasterBot в Max)
  MAX_CHAT_ID=...     (числовой id канала; узнать: py agents/notify_max.py --chats)

Запуск (Windows - py):
  py agents/notify_max.py --me                                   # кто я (проверка токена)
  py agents/notify_max.py --chats                                # показать чаты бота -> найти chat_id
  py agents/notify_max.py --test                                 # отправить проверочное сообщение
  py agents/notify_max.py --file digests/2026-06-15-monday.md    # отправить дайджест из файла
"""

import os
import sys
import time
import argparse

import requests

from llm import _load_dotenv
_load_dotenv()

# разметку и разбивку на части не дублируем - берем из Telegram-модуля
from notify_telegram import md_to_tg, split_chunks, LIMIT

API = "https://botapi.max.ru/{method}"


def _cfg():
    token = os.environ.get("MAX_BOT_TOKEN")
    chat = os.environ.get("MAX_CHAT_ID")
    if not token:
        raise SystemExit("Не задан MAX_BOT_TOKEN (см. .env.example).")
    return token, chat


def configured():
    """Есть ли токен Max - чтобы broadcast молча пропускал Max, если он не настроен."""
    return bool(os.environ.get("MAX_BOT_TOKEN"))


def _headers():
    token, _ = _cfg()
    return {"Authorization": token}


def _post(method, params=None, json=None, timeout=60):
    r = requests.post(API.format(method=method), headers=_headers(),
                      params=params or {}, json=json, timeout=timeout)
    data = r.json() if r.content else {}
    if r.status_code != 200 or data.get("code"):
        raise RuntimeError(f"Max {method}: {r.status_code} {data}")
    return data


def _get(method, params=None, timeout=30):
    r = requests.get(API.format(method=method), headers=_headers(),
                     params=params or {}, timeout=timeout)
    data = r.json() if r.content else {}
    if r.status_code != 200 or data.get("code"):
        raise RuntimeError(f"Max {method}: {r.status_code} {data}")
    return data


def send_text(text, chat=None):
    """Отправить одно сообщение в канал (или в указанный chat_id)."""
    token, default_chat = _cfg()
    chat = chat or default_chat
    if not chat:
        raise SystemExit("Не задан MAX_CHAT_ID.")
    return _post("messages", params={"chat_id": chat},
                 json={"text": text, "format": "html",
                       "notify": True, "disable_link_preview": True})


def dm_recipients():
    """Список user_id для личных сообщений в Max (секрет MAX_DM_RECIPIENTS через запятую)."""
    raw = os.environ.get("MAX_DM_RECIPIENTS", "")
    return [x.strip() for x in raw.split(",") if x.strip()]


def send_dm(text, user_id):
    """Личное сообщение пользователю Max по его user_id (адресуется не chat_id, а user_id)."""
    return _post("messages", params={"user_id": user_id},
                 json={"text": text, "format": "html",
                       "notify": True, "disable_link_preview": True})


def send_markdown(md):
    """Отправить наш markdown-дайджест в канал (с разбивкой на части)."""
    text = md_to_tg(md)  # тот же конвертер, что и для Telegram (HTML)
    parts = split_chunks(text, LIMIT)
    for i, part in enumerate(parts, 1):
        send_text(part)
        print(f"Max: отправлено сообщение {i}/{len(parts)}")
    return len(parts)


def _upload_image(data, ct):
    """Загрузить байты картинки в Max: вернуть payload для attachments или None.
    Поток Max: POST /uploads?type=image -> url -> залить байты -> объект photos."""
    try:
        up = requests.post(API.format(method="uploads"), headers=_headers(),
                           params={"type": "image"}, timeout=30)
        url = up.json().get("url") if up.status_code == 200 else None
        if not url:
            return None
        fr = requests.post(url, files={"data": ("img", data, ct or "image/jpeg")}, timeout=120)
        photos = fr.json().get("photos") if fr.status_code == 200 else None
        return {"type": "image", "payload": {"photos": photos}} if photos else None
    except Exception:
        return None


def send_photos(image_urls, caption=None, chat=None):
    """Отправить фото с подписью, ЗАГРУЖАЯ их файлом (Max не принимает фото по URL).
    Возвращает True при успехе, False если не вышло (тогда зовущий шлёт текст)."""
    import images
    token, default_chat = _cfg()
    chat = chat or default_chat
    urls = [u for u in (image_urls or []) if u][:10]
    if not chat or not urls:
        return False
    attachments = []
    for u in urls:
        data, ct = images.download_image(u)
        if not data:
            continue
        att = _upload_image(data, ct)
        if att:
            attachments.append(att)
    if not attachments:
        return False
    body = {"attachments": attachments}
    if caption:
        body.update({"text": caption, "format": "html"})
    # Max обрабатывает картинку асинхронно - возможна ошибка "не готово", повторяем
    for _ in range(5):
        try:
            _post("messages", params={"chat_id": chat}, json=body)
            return True
        except RuntimeError as e:
            if "not.ready" in str(e) or "attachment" in str(e):
                time.sleep(2)
                continue
            print(f"  (Max: фото не ушло: {str(e)[:120]})")
            return False
    return False


def me():
    """Информация о боте - быстрая проверка токена."""
    return _get("me")


def show_chats():
    """Показать чаты, где состоит бот - чтобы найти числовой chat_id канала."""
    data = _get("chats")
    chats = data.get("chats", [])
    if not chats:
        print("Бот пока не состоит ни в одном чате.")
        print("Добавьте бота администратором в канал и запустите снова.")
        return
    for c in chats:
        print(f"chat_id = {c.get('chat_id')}   "
              f"({c.get('type')}: {c.get('title') or c.get('link') or '-'})")


def show_updates():
    """Показать, кто писал боту в личку - чтобы взять user_id для личной рассылки."""
    data = _get("updates", params={"timeout": 0, "limit": 30})
    seen = {}
    for u in data.get("updates", []):
        msg = u.get("message") or {}
        snd = msg.get("sender") or {}
        if snd.get("user_id"):
            seen[snd["user_id"]] = snd.get("name") or snd.get("username") or "-"
    if not seen:
        print("Никто пока не писал боту в личку.")
        print("Пусть человек откроет max.ru/id7801670627_bot, нажмёт «Старт» и напишет,")
        print("затем запустите снова.")
        return
    for uid, name in seen.items():
        print(f"user_id = {uid}   ({name})")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--me", action="store_true", help="проверить токен (кто я)")
    ap.add_argument("--chats", action="store_true", help="показать chat_id чатов бота")
    ap.add_argument("--updates", action="store_true", help="показать user_id написавших в личку")
    ap.add_argument("--test", action="store_true", help="отправить проверочное сообщение")
    ap.add_argument("--file", help="отправить дайджест из файла .md")
    args = ap.parse_args()

    if args.me:
        info = me()
        print(f"Бот: {info.get('name')} (@{info.get('username')}), id {info.get('user_id')}")
        return
    if args.chats:
        show_chats()
        return
    if args.updates:
        show_updates()
        return
    if args.test:
        send_text("<b>Проверка связи</b>\nДайджест НКГ подключен к каналу в Max.")
        print("Готово: проверочное сообщение отправлено в Max.")
        return
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            md = fh.read()
        n = send_markdown(md)
        print(f"Готово: дайджест отправлен в Max ({n} сообщ.).")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
