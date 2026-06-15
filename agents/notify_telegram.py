# -*- coding: utf-8 -*-
"""
Отправка дайджеста в Telegram.

Нужны два значения в .env:
  TELEGRAM_BOT_TOKEN=...   (от @BotFather)
  TELEGRAM_CHAT_ID=...     (@имя_канала для публичного, или числовой -100... для закрытого)

Запуск:
  py agents/notify_telegram.py --test                  # отправить проверочное сообщение
  py agents/notify_telegram.py --updates               # показать чаты, которые видел бот (найти chat_id)
  py agents/notify_telegram.py --file digests/2026-06-14-daily.md   # отправить дайджест из файла
"""

import os
import re
import sys
import html
import argparse

import requests

from llm import _load_dotenv
_load_dotenv()

API = "https://api.telegram.org/bot{token}/{method}"
LIMIT = 3800  # с запасом до телеграмного лимита 4096


def _cfg():
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat = os.environ.get("TELEGRAM_CHAT_ID")
    if not token:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN (см. .env.example).")
    return token, chat


def md_to_tg(md):
    """Перевести наш markdown в HTML, понятный Telegram (parse_mode=HTML)."""
    out_lines = []
    for line in md.splitlines():
        s = html.escape(line, quote=False)  # экранируем &, <, >
        # ссылки [текст](url) -> <a href="url">текст</a>
        s = re.sub(r"\[([^\]]+)\]\((https?://[^)]+)\)",
                   lambda m: f'<a href="{m.group(2)}">{m.group(1)}</a>', s)
        # заголовки # / ## -> жирный
        s = re.sub(r"^\s*#{1,6}\s*(.+)$", r"<b>\1</b>", s)
        # **жирный**
        s = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", s)
        # _курсив_ (только когда подчеркивание окружено границами слова)
        s = re.sub(r"(?<![\w])_(.+?)_(?![\w])", r"<i>\1</i>", s)
        # маркер списка "- " -> "• "
        s = re.sub(r"^\s*-\s+", "• ", s)
        out_lines.append(s)
    return "\n".join(out_lines)


def split_chunks(text, limit=LIMIT):
    """Разбить длинный текст по строкам, не превышая лимит сообщения."""
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit and cur:
            chunks.append(cur)
            cur = ""
        cur += (line + "\n")
    if cur.strip():
        chunks.append(cur)
    return chunks


def send_text(text, parse_mode="HTML", chat=None):
    token, default_chat = _cfg()
    chat = chat or default_chat
    if not chat:
        raise SystemExit("Не задан TELEGRAM_CHAT_ID.")
    r = requests.post(
        API.format(token=token, method="sendMessage"),
        json={"chat_id": chat, "text": text, "parse_mode": parse_mode,
              "disable_web_page_preview": True},
        timeout=30,
    )
    data = r.json()
    if not data.get("ok"):
        raise SystemExit(f"Ошибка Telegram: {data}")
    return data


def owner_chat():
    """Личный чат владельца - для согласований (топ-3 персоны и т. п.)."""
    return os.environ.get("TELEGRAM_OWNER_CHAT_ID")


def send_to_owner(text):
    """Написать владельцу в личку. Возвращает True/False (есть ли адрес)."""
    chat = owner_chat()
    if not chat:
        print("Не задан TELEGRAM_OWNER_CHAT_ID - личное сообщение не отправлено.")
        return False
    send_text(text, chat=chat)
    return True


def _get_updates():
    token, _ = _cfg()
    try:
        r = requests.get(API.format(token=token, method="getUpdates"), timeout=30)
        return r.json().get("result", [])
    except Exception:
        return []


def current_update_id():
    """Наибольший номер апдейта сейчас - запоминаем его при отправке вопроса,
    чтобы потом читать только ответы, пришедшие ПОСЛЕ него."""
    ids = [u.get("update_id", 0) for u in _get_updates()]
    return max(ids) if ids else 0


def owner_messages_since(since_id=0):
    """Тексты сообщений владельца, пришедшие после since_id (по возрастанию)."""
    owner = owner_chat()
    if not owner:
        return []
    out = []
    for upd in _get_updates():
        if upd.get("update_id", 0) <= (since_id or 0):
            continue
        msg = upd.get("message") or {}
        chat = msg.get("chat") or {}
        if str(chat.get("id")) != str(owner):
            continue
        text = (msg.get("text") or "").strip()
        if text:
            out.append(text)
    return out


def latest_reply_from_owner(allowed=("1", "2", "3"), since_id=0):
    """Последний короткий ответ владельца из разрешенных (например 1/2/3)."""
    found = None
    for text in owner_messages_since(since_id):
        if text in allowed:
            found = text
    return found


def send_photos(image_urls, caption=None, chat=None):
    """Отправить альбом фото (до 10), ЗАГРУЖАЯ их файлом (Telegram плохо тянет
    защищённые ссылки сам). Подпись - на первом фото. True при успехе."""
    import json
    import images
    token, default_chat = _cfg()
    chat = chat or default_chat
    urls = [u for u in (image_urls or []) if u][:10]
    if not chat or not urls:
        return False
    blobs = []
    for u in urls:
        data, ct = images.download_image(u)
        if data:
            ext = "png" if "png" in (ct or "") else "jpg"
            blobs.append((f"photo{len(blobs)}.{ext}", data))
    if not blobs:
        return False
    try:
        if len(blobs) == 1:
            data = {"chat_id": chat}
            if caption:
                data.update({"caption": caption[:1024], "parse_mode": "HTML"})
            r = requests.post(API.format(token=token, method="sendPhoto"),
                              data=data, files={"photo": blobs[0]}, timeout=120)
            return bool(r.json().get("ok"))
        media, files = [], {}
        for i, blob in enumerate(blobs):
            key = f"photo{i}"
            files[key] = blob
            item = {"type": "photo", "media": f"attach://{key}"}
            if i == 0 and caption:
                item.update({"caption": caption[:1024], "parse_mode": "HTML"})
            media.append(item)
        r = requests.post(API.format(token=token, method="sendMediaGroup"),
                          data={"chat_id": chat, "media": json.dumps(media)},
                          files=files, timeout=120)
        return bool(r.json().get("ok"))
    except Exception:
        return False


def send_markdown(md):
    """Отправить наш markdown-дайджест (с разбивкой на части)."""
    tg = md_to_tg(md)
    parts = split_chunks(tg)
    for i, part in enumerate(parts, 1):
        send_text(part)
        print(f"Отправлено сообщение {i}/{len(parts)}")
    return len(parts)


def show_updates():
    """Показать чаты, которые видел бот - чтобы найти числовой chat_id."""
    token, _ = _cfg()
    r = requests.get(API.format(token=token, method="getUpdates"), timeout=30)
    data = r.json()
    seen = {}
    for upd in data.get("result", []):
        msg = upd.get("message") or upd.get("channel_post") or {}
        chat = msg.get("chat")
        if chat:
            seen[chat["id"]] = chat.get("title") or chat.get("username") or chat.get("type")
    if not seen:
        print("Бот пока не видел ни одного чата. Напишите что-нибудь боту или в канал,")
        print("где он администратор, и запустите снова.")
    for cid, name in seen.items():
        print(f"chat_id = {cid}   ({name})")


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", action="store_true", help="отправить проверочное сообщение")
    ap.add_argument("--updates", action="store_true", help="показать chat_id виденных чатов")
    ap.add_argument("--file", help="отправить дайджест из файла .md")
    args = ap.parse_args()

    if args.updates:
        show_updates()
        return
    if args.test:
        send_text("<b>Проверка связи</b>\nДайджест НКГ подключен к каналу.")
        print("Готово: проверочное сообщение отправлено.")
        return
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            md = fh.read()
        n = send_markdown(md)
        print(f"Готово: дайджест отправлен ({n} сообщ.).")
        return
    ap.print_help()


if __name__ == "__main__":
    main()
