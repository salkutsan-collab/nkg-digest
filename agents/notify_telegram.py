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


def send_text(text, parse_mode="HTML"):
    token, chat = _cfg()
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
