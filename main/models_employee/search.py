# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Поиск сотрудников по фамилии / имени / e-mail с ранжированием по степени совпадения."""

from .matching import EXACT, normalize, score_parts
from .permissions import display_name


def match_score(user, query):
    """Степень совпадения (меньше — лучше) или None, если пользователь не подходит."""
    return score_parts(query, user.first_name, user.last_name, user.email, user.get_username())


def search_users(users, query, limit=10):
    """Подходящие пользователи, отсортированные по степени совпадения, затем по фамилии и имени."""
    ranked = []
    for user in users:
        score = match_score(user, query)
        if score is not None:
            ranked.append((score, normalize(user.last_name), normalize(user.first_name), normalize(user.get_username()), user))
    ranked.sort(key=lambda r: r[:4])
    return [(r[0], r[4]) for r in ranked[:limit]]


def user_card(user, score=None):
    data = {
        "id": user.pk,
        "name": display_name(user),
        "first_name": user.first_name,
        "last_name": user.last_name,
        "email": user.email,
        "username": user.get_username(),
    }
    if score is not None:
        data["exact"] = score == EXACT
    return data
