# -*- coding: utf-8 -*-
"""
Веерная рассылка постов КАНАЛА сразу во все мессенджеры (Telegram + Max).

Зачем отдельный слой: личные сообщения владельцу и интерактив (предпросмотр,
выбор номеров, запоминание правил) - это телеграм-специфика и остаются в
notify_telegram. А вот публичные посты канала должны уходить в оба мессенджера.
Max подключается сам, если задан MAX_BOT_TOKEN; иначе молча пропускается.

Любая ошибка одного мессенджера не должна ронять отправку в другой.

Здесь же ведется журнал публикаций (postlog): на каждый пост в каждый мессенджер
пишется строка в data/posts.jsonl - что за рубрика, сколько сообщений и фото,
id сообщений, ушло или со сбоем. Раз в сутки заодно снимается число подписчиков.
Журнал - вспомогательный: его сбой публикацию не ломает.
"""

import notify_telegram
import postlog


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

# число подписчиков снимаем один раз за прогон (а не на каждое сообщение)
_subs_done = False


def _reset(mod):
    """Обнулить id прошлой отправки в мессенджере - перед новым постом."""
    try:
        mod.reset_ids()
    except Exception:
        pass


def _ids(mod):
    try:
        return mod.last_message_ids()
    except Exception:
        return []


def _log(channel, kind, ok, mod=None, parts=0, photos=0, chars=0, error="", extra=None):
    """Записать строку в журнал публикаций. Журнал не должен ронять отправку."""
    try:
        postlog.log_post(kind=kind, channel=channel, ok=ok, parts=parts,
                         photos=photos, chars=chars,
                         message_ids=_ids(mod) if mod else [],
                         error=error, extra=extra)
    except Exception as e:
        print(f"  (журнал публикаций: {str(e)[:120]})")


def _count_subscribers():
    """Снять число подписчиков каналов - один раз за прогон, один раз в сутки."""
    global _subs_done
    if _subs_done:
        return
    _subs_done = True
    try:
        got = postlog.snapshot_subscribers()
        for ch, n in (got or {}).items():
            print(f"  (подписчики {ch}: {n})")
    except Exception as e:
        print(f"  (подписчики: не посчитать: {str(e)[:120]})")


def send_markdown(md, kind="?", extra=None):
    """Опубликовать markdown-дайджест в канал во всех мессенджерах."""
    chars = len(md or "")
    n = 0
    _reset(notify_telegram)
    try:
        n = notify_telegram.send_markdown(md)
        _log("telegram", kind, True, notify_telegram, parts=n, chars=chars, extra=extra)
    except _ERRORS as e:
        print(f"  (Telegram: пост не ушёл: {str(e)[:120]})")
        _log("telegram", kind, False, chars=chars, error=e, extra=extra)
    m = _max()
    if m:
        _reset(m)
        try:
            parts = m.send_markdown(md)
            _log("max", kind, True, m, parts=parts, chars=chars, extra=extra)
        except _ERRORS as e:
            print(f"  (Max: пост не ушёл: {str(e)[:120]})")
            _log("max", kind, False, chars=chars, error=e, extra=extra)
    _count_subscribers()
    return n


def send_text(text, kind="?", extra=None):
    """Отправить готовый HTML-текст в канал во всех мессенджерах."""
    chars = len(text or "")
    _reset(notify_telegram)
    try:
        notify_telegram.send_text(text)
        _log("telegram", kind, True, notify_telegram, parts=1, chars=chars, extra=extra)
    except _ERRORS as e:
        print(f"  (Telegram: сообщение не ушло: {str(e)[:120]})")
        _log("telegram", kind, False, chars=chars, error=e, extra=extra)
    m = _max()
    if m:
        _reset(m)
        try:
            m.send_text(text)
            _log("max", kind, True, m, parts=1, chars=chars, extra=extra)
        except _ERRORS as e:
            print(f"  (Max: сообщение не ушло: {str(e)[:120]})")
            _log("max", kind, False, chars=chars, error=e, extra=extra)
    _count_subscribers()


def send_photos(image_urls, caption=None, kind="?", extra=None):
    """Отправить фото с подписью в канал во всех мессенджерах.
    Возвращает True, если фото ушло ХОТЯ БЫ в один мессенджер."""
    n_photos = len([u for u in (image_urls or []) if u][:10])
    chars = len(caption or "")
    ok = False
    _reset(notify_telegram)
    try:
        ok = bool(notify_telegram.send_photos(image_urls, caption=caption))
        _log("telegram", kind, ok, notify_telegram, parts=1 if ok else 0,
             photos=n_photos if ok else 0, chars=chars,
             error="" if ok else "фото не приняты", extra=extra)
    except _ERRORS as e:
        print(f"  (Telegram: фото не ушло: {str(e)[:120]})")
        _log("telegram", kind, False, photos=n_photos, chars=chars, error=e, extra=extra)
    m = _max()
    if m:
        _reset(m)
        try:
            got = bool(m.send_photos(image_urls, caption=caption))
            _log("max", kind, got, m, parts=1 if got else 0,
                 photos=n_photos if got else 0, chars=chars,
                 error="" if got else "фото не приняты", extra=extra)
            ok = got or ok
        except _ERRORS as e:
            print(f"  (Max: фото не ушло: {str(e)[:120]})")
            _log("max", kind, False, photos=n_photos, chars=chars, error=e, extra=extra)
    _count_subscribers()
    return ok


def _photo_or_text(mod, label, channel, image_urls, caption, text, kind, extra):
    """Один мессенджер: сначала фото с подписью, а если не вышло - текст.
    Решение про запасной текст принимается ОТДЕЛЬНО для каждого мессенджера,
    иначе сбой в одном приводит к дублю в другом."""
    n_photos = len([u for u in (image_urls or []) if u][:10])
    if image_urls:
        _reset(mod)
        try:
            if mod.send_photos(image_urls, caption=caption):
                _log(channel, kind, True, mod, parts=1, photos=n_photos,
                     chars=len(caption or ""), extra=extra)
                return
        except _ERRORS as e:
            print(f"  ({label}: фото не ушло: {str(e)[:120]})")
    _reset(mod)
    try:
        mod.send_text(text)
        _log(channel, kind, True, mod, parts=1, chars=len(text or ""),
             extra=dict(extra or {}, fallback="текст вместо фото" if image_urls else ""))
    except _ERRORS as e:
        print(f"  ({label}: сообщение не ушло: {str(e)[:120]})")
        _log(channel, kind, False, chars=len(text or ""), error=e, extra=extra)


def send_photo_or_text(image_urls, caption, text, kind="?", extra=None):
    """Пост с картинкой (например рекомендация дня): в каждый мессенджер уходит
    фото с подписью, а если фото не доставить - текстовый вариант."""
    _photo_or_text(notify_telegram, "Telegram", "telegram",
                   image_urls, caption, text, kind, extra)
    m = _max()
    if m:
        _photo_or_text(m, "Max", "max", image_urls, caption, text, kind, extra)
    _count_subscribers()
