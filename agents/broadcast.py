# -*- coding: utf-8 -*-
"""
Веерная рассылка постов КАНАЛА сразу во все мессенджеры (Telegram + Max).

Зачем отдельный слой: личные сообщения владельцу и интерактив (предпросмотр,
выбор номеров, запоминание правил) - это телеграм-специфика и остаются в
notify_telegram. А вот публичные посты канала должны уходить в оба мессенджера.
Max подключается сам, если задан MAX_BOT_TOKEN; иначе молча пропускается.

Любая ошибка одного мессенджера не должна ронять отправку в другой.
"""

import notify_telegram


def _max():
    """Вернуть модуль Max, только если он настроен (есть токен)."""
    try:
        import notify_max
        if notify_max.configured():
            return notify_max
    except Exception:
        pass
    return None


# notify_telegram при отсутствии токена кидает SystemExit (не подкласс Exception),
# поэтому ловим оба - падение одного мессенджера не должно ронять другой и весь прогон.
_ERRORS = (SystemExit, Exception)


def send_markdown(md):
    """Опубликовать markdown-дайджест в канал во всех мессенджерах."""
    n = 0
    try:
        n = notify_telegram.send_markdown(md)
    except _ERRORS as e:
        print(f"  (Telegram: пост не ушёл: {str(e)[:120]})")
    m = _max()
    if m:
        try:
            m.send_markdown(md)
        except _ERRORS as e:
            print(f"  (Max: пост не ушёл: {str(e)[:120]})")
    return n


def send_text(text):
    """Отправить готовый HTML-текст в канал во всех мессенджерах."""
    try:
        notify_telegram.send_text(text)
    except _ERRORS as e:
        print(f"  (Telegram: сообщение не ушло: {str(e)[:120]})")
    m = _max()
    if m:
        try:
            m.send_text(text)
        except _ERRORS as e:
            print(f"  (Max: сообщение не ушло: {str(e)[:120]})")


def send_photos(image_urls, caption=None):
    """Отправить фото с подписью в канал во всех мессенджерах.
    Возвращает True, если фото ушло ХОТЯ БЫ в один мессенджер."""
    ok = False
    try:
        ok = bool(notify_telegram.send_photos(image_urls, caption=caption))
    except _ERRORS as e:
        print(f"  (Telegram: фото не ушло: {str(e)[:120]})")
    m = _max()
    if m:
        try:
            ok = bool(m.send_photos(image_urls, caption=caption)) or ok
        except _ERRORS as e:
            print(f"  (Max: фото не ушло: {str(e)[:120]})")
    return ok


def _photo_or_text(mod, label, image_urls, caption, text):
    """Один мессенджер: сначала фото с подписью, а если не вышло - текст.
    Решение про запасной текст принимается ОТДЕЛЬНО для каждого мессенджера,
    иначе сбой в одном приводит к дублю в другом."""
    if image_urls:
        try:
            if mod.send_photos(image_urls, caption=caption):
                return
        except _ERRORS as e:
            print(f"  ({label}: фото не ушло: {str(e)[:120]})")
    try:
        mod.send_text(text)
    except _ERRORS as e:
        print(f"  ({label}: сообщение не ушло: {str(e)[:120]})")


def send_photo_or_text(image_urls, caption, text):
    """Пост с картинкой (например рекомендация дня): в каждый мессенджер уходит
    фото с подписью, а если фото не доставить - текстовый вариант."""
    _photo_or_text(notify_telegram, "Telegram", image_urls, caption, text)
    m = _max()
    if m:
        _photo_or_text(m, "Max", image_urls, caption, text)
