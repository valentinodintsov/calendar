# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Ранжирование совпадений при поиске людей по фамилии / имени / e-mail.
Без зависимостей от Django — используется для сотрудников и для контактов адресной книги.
"""

import re

# Чем меньше, тем выше в подсказках
EXACT = 0  # полное совпадение: фамилия, имя, «имя фамилия», «фамилия имя», ФИО, e-mail или логин
PREFIX = 1  # начало фамилии, имени, e-mail или логина
FULL_PREFIX = 2  # начало «фамилия имя» / «имя фамилия» / ФИО
WORDS = 3  # каждое слово запроса — начало какого-то слова (например, «пет ив»)
SUBSTRING = 4  # запрос встречается где угодно


def normalize(text):
    """Без учёта регистра, «ё» = «е», лишние пробелы схлопнуты."""
    return re.sub(r"\s+", " ", (text or "").casefold().replace("ё", "е")).strip()


def score_parts(query, first="", last="", email="", login="", full_names=(), extra=()):
    """
    Степень совпадения (меньше — лучше) или None.
    full_names — готовые полные имена (например, displayName «Петров Иван Сергеевич»);
    extra — прочие поля (должность, отдел, телефон): участвуют только в поиске по словам и подстроке.
    """
    q = normalize(query)
    if not q:
        return None
    first, last, email, login = normalize(first), normalize(last), normalize(email), normalize(login)
    full = [s for s in (f"{first} {last}".strip(), f"{last} {first}".strip(), *map(normalize, full_names)) if s]
    singles = [s for s in (first, last, email, login, email.split("@")[0]) if s]
    others = [s for s in map(normalize, extra) if s]

    if q in singles or q in full:
        return EXACT
    if any(s.startswith(q) for s in singles):
        return PREFIX
    if any(s.startswith(q) for s in full):
        return FULL_PREFIX
    words = [w for w in re.split(r"[\s@._,()-]+", " ".join([first, last, email, login, *full, *others])) if w]
    tokens = q.split(" ")
    if all(any(w.startswith(t) for w in words) for t in tokens):
        return WORDS
    haystack = " ".join(singles + full + others)
    if all(t in haystack for t in tokens):
        return SUBSTRING
    return None
