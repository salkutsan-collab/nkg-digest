# -*- coding: utf-8 -*-
"""
Единая обертка над моделью для всех агентов.

Провайдер выбирается переменной окружения LLM_PROVIDER:
  gigachat   (по умолчанию) - GigaChat от Сбера, оплата в рублях
  anthropic  - Claude (запасной вариант)

Ключи берутся из окружения или из файла .env в корне проекта.
Для GigaChat нужен один "ключ авторизации" (Authorization key, base64):
  GIGACHAT_CREDENTIALS=...
  GIGACHAT_SCOPE=GIGACHAT_API_PERS   (для физлиц; для компаний GIGACHAT_API_B2B)
"""

import os

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _load_dotenv():
    """Подтянуть переменные из .env (без сторонних библиотек)."""
    path = os.path.join(_ROOT, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))


_load_dotenv()


class LLMError(Exception):
    pass


def provider():
    return os.environ.get("LLM_PROVIDER", "gigachat").lower()


def available():
    """Есть ли ключ для выбранного провайдера."""
    p = provider()
    if p == "yandex":
        return bool(os.environ.get("YANDEX_API_KEY") and os.environ.get("YANDEX_FOLDER_ID"))
    if p == "gigachat":
        return bool(os.environ.get("GIGACHAT_CREDENTIALS"))
    if p == "anthropic":
        return bool(os.environ.get("ANTHROPIC_API_KEY"))
    return False


def chat(system, user, temperature=0.3, max_tokens=1800):
    """Один вопрос к модели. Возвращает текст ответа."""
    p = provider()
    if p == "yandex":
        return _yandex(system, user, temperature, max_tokens)
    if p == "gigachat":
        return _gigachat(system, user, temperature, max_tokens)
    if p == "anthropic":
        return _anthropic(system, user, temperature, max_tokens)
    raise LLMError(f"Неизвестный провайдер LLM_PROVIDER={p}")


def _yandex(system, user, temperature, max_tokens):
    key = os.environ.get("YANDEX_API_KEY")
    folder = os.environ.get("YANDEX_FOLDER_ID")
    if not key or not folder:
        raise LLMError("Нужны YANDEX_API_KEY и YANDEX_FOLDER_ID (см. .env.example).")
    model = os.environ.get("YANDEX_MODEL", "yandexgpt")  # yandexgpt | yandexgpt-lite
    import requests

    msgs = []
    if system:
        msgs.append({"role": "system", "text": system})
    msgs.append({"role": "user", "text": user})
    body = {
        "modelUri": f"gpt://{folder}/{model}/latest",
        "completionOptions": {"stream": False, "temperature": temperature,
                              "maxTokens": str(max_tokens)},
        "messages": msgs,
    }
    r = requests.post(
        "https://llm.api.cloud.yandex.net/foundationModels/v1/completion",
        headers={"Authorization": f"Api-Key {key}", "Content-Type": "application/json"},
        json=body, timeout=60,
    )
    data = r.json()
    if "result" not in data:
        raise LLMError(f"Ошибка YandexGPT: {str(data)[:200]}")
    return data["result"]["alternatives"][0]["message"]["text"]


def _gigachat(system, user, temperature, max_tokens):
    creds = os.environ.get("GIGACHAT_CREDENTIALS")
    if not creds:
        raise LLMError("Не задан GIGACHAT_CREDENTIALS (ключ авторизации GigaChat).")
    scope = os.environ.get("GIGACHAT_SCOPE", "GIGACHAT_API_PERS")
    model = os.environ.get("GIGACHAT_MODEL", "GigaChat")
    from gigachat import GigaChat
    from gigachat.models import Chat, Messages, MessagesRole

    msgs = []
    if system:
        msgs.append(Messages(role=MessagesRole.SYSTEM, content=system))
    msgs.append(Messages(role=MessagesRole.USER, content=user))
    with GigaChat(credentials=creds, scope=scope, model=model,
                  verify_ssl_certs=False) as g:
        resp = g.chat(Chat(messages=msgs, temperature=max(0.01, temperature),
                           max_tokens=max_tokens))
    return resp.choices[0].message.content


def _anthropic(system, user, temperature, max_tokens):
    if not os.environ.get("ANTHROPIC_API_KEY"):
        raise LLMError("Не задан ANTHROPIC_API_KEY.")
    import anthropic
    client = anthropic.Anthropic()
    model = os.environ.get("ANTHROPIC_MODEL", "claude-opus-4-8")
    msg = client.messages.create(
        model=model, max_tokens=max_tokens, temperature=temperature,
        system=system or "", messages=[{"role": "user", "content": user}],
    )
    return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
