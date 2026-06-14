# -*- coding: utf-8 -*-
"""
Предпочтения читателя - чему учится дайджест (data/preferences.md).

Правила простым текстом влияют на оценку релевантности событий: что просили
больше - выше балл, что просили убрать - ниже. Пополняется вручную или ответом
владельца боту на предпросмотр.

Запуск руками:
  py agents/prefs.py --show
  py agents/prefs.py "не нужны детские спектакли"
"""

import os
import re
import sys
import argparse

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PREFS_PATH = os.path.join(ROOT, "data", "preferences.md")
SEP = "---"


def load_preferences():
    """Список правил (строки после линии ---, без комментариев)."""
    if not os.path.exists(PREFS_PATH):
        return []
    with open(PREFS_PATH, encoding="utf-8") as fh:
        body = fh.read()
    if SEP in body:
        body = body.split(SEP, 1)[1]
    rules = []
    for ln in body.splitlines():
        s = ln.strip()
        if not s or s.startswith(("#", "<!--", ">")):
            continue
        if s.startswith("- "):
            s = s[2:].strip()
        if s:
            rules.append(s)
    return rules


def as_prompt():
    """Готовая вставка для запроса к модели или пустая строка."""
    rules = load_preferences()
    if not rules:
        return ""
    return "Предпочтения читателя (учитывай их при оценке relevance: что просят " \
           "больше - выше балл; что просят убрать - ставь 0): " + "; ".join(rules) + "."


def add_preference(text):
    """Дописать правило, если его ещё нет. Возвращает True при добавлении."""
    text = re.sub(r"\s+", " ", str(text)).strip().rstrip(".")
    if len(text) < 4:
        return False
    existing = {r.lower() for r in load_preferences()}
    if text.lower() in existing:
        return False
    if not os.path.exists(PREFS_PATH):
        with open(PREFS_PATH, "w", encoding="utf-8") as fh:
            fh.write("# Предпочтения читателя\n\n" + SEP + "\n")
    with open(PREFS_PATH, "a", encoding="utf-8") as fh:
        fh.write(f"- {text}\n")
    return True


def looks_like_preference(text):
    """Похоже ли сообщение на правило (а не на выбор номеров / 'ок')?"""
    s = (text or "").strip().lower()
    if not s:
        return False
    if s in ("ок", "ok", "да", "нет", "1", "2", "3"):
        return False
    # если в сообщении только цифры, запятые и пробелы - это выбор номеров
    if re.fullmatch(r"[\d\s,.;]+", s):
        return False
    # должно быть достаточно букв, чтобы считать это фразой
    letters = len(re.findall(r"[а-яёa-z]", s))
    return letters >= 4


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser()
    ap.add_argument("text", nargs="*", help="новое правило")
    ap.add_argument("--show", action="store_true")
    args = ap.parse_args()
    if args.show or not args.text:
        rules = load_preferences()
        print("Правила предпочтений:" if rules else "Правил пока нет.")
        for r in rules:
            print(f"  - {r}")
        return
    text = " ".join(args.text)
    print("Добавлено." if add_preference(text) else "Уже есть или слишком коротко.")


if __name__ == "__main__":
    main()
