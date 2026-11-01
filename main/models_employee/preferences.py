# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Личные настройки пользователя (окно «Настройки» — шестерёнка): у каждого свои, хранятся на сервере
в Employee.preferences и действуют на любом компьютере и в любом браузере, где он вошёл.
"""

from .models import Employee

VIEWS = ("month", "week", "day")
CHOICES = {
    "theme": ("system", "light", "dark"),  # оформление
    "start_view": ("last",) + VIEWS,  # вид при открытии календаря
    "last_view": VIEWS,  # последний выбранный вид (для «Последний»)
}
DEFAULTS = {"theme": "system", "start_view": "last", "last_view": "month"}
# Фильтр календарей (левая панель): id владельцев, чьи календари выключены. По умолчанию включены все.
HIDDEN_CALENDARS = "hidden_calendars"
MAX_HIDDEN_CALENDARS = 1000


def _clean_ids(value):
    """Список id пользователей или None, если значение недопустимо."""
    if not isinstance(value, list) or len(value) > MAX_HIDDEN_CALENDARS:
        return None
    if not all(isinstance(v, int) and not isinstance(v, bool) and v > 0 for v in value):
        return None
    return sorted(set(value))


def _employee(user):
    emp = getattr(user, "employee", None)
    if emp is None:
        emp, _ = Employee.objects.get_or_create(user=user)
    return emp


def get_preferences(user) -> dict:
    stored = _employee(user).preferences if user.is_authenticated else {}
    stored = stored if isinstance(stored, dict) else {}
    prefs = {k: stored.get(k) if stored.get(k) in CHOICES[k] else v for k, v in DEFAULTS.items()}
    prefs[HIDDEN_CALENDARS] = _clean_ids(stored.get(HIDDEN_CALENDARS)) or []
    return prefs


def hidden_calendars(user) -> list[int]:
    """Владельцы, чьи календари пользователь выключил в фильтре: их события не показываются и не идут в отчет."""
    return get_preferences(user)[HIDDEN_CALENDARS]


def update_preferences(user, data: dict) -> tuple[dict, dict]:
    """Сохраняет допустимые значения. Возвращает (настройки, ошибки)."""
    errors = {}
    changes = {}
    for key, value in data.items():
        if key == HIDDEN_CALENDARS:
            ids = _clean_ids(value)
            if ids is None:
                errors[key] = ["Ожидается список id пользователей."]
            else:
                changes[key] = ids
        elif key not in CHOICES:
            errors[key] = ["Неизвестная настройка."]
        elif value not in CHOICES[key]:
            errors[key] = [f"Допустимые значения: {', '.join(CHOICES[key])}."]
        else:
            changes[key] = value
    if changes and not errors:
        emp = _employee(user)
        prefs = emp.preferences if isinstance(emp.preferences, dict) else {}
        prefs.update(changes)
        emp.preferences = prefs
        emp.save(update_fields=["preferences"])
    return get_preferences(user), errors
