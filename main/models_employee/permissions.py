# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Проверка прав доступа к календарю. Роли пользователя складываются:
итоговые права — объединение прав всех его ролей.

Права на событие: {"read": bool, "change": bool}.
Право «Запись» (создание) относится к календарю владельца, а не к событию:
см. Access.creatable_owners() / Access.can_create_for().
"""

from functools import wraps

from django.contrib.auth import get_user_model
from django.db.models import Q
from django.http import Http404, JsonResponse

from .models import Delegation, Role
from .preferences import get_preferences

NO_ACCESS = {"read": False, "change": False}
READ_ONLY = {"read": True, "change": False}
FULL = {"read": True, "change": True}


def _employee(user):
    return getattr(user, "employee", None)  # профиля нет — только права владельца по умолчанию


def reads_all_calendars(user):
    """Менеджер всех календарей с правом чтения (настройка CALENDAR_MANAGERS)."""
    from .ad_auth import calendar_managers

    return user is not None and user.is_authenticated and user.get_username().lower() in calendar_managers()


def get_roles(user, with_manager=True):
    """Множество ролей пользователя (Role). «Менеджер» — если ему делегированы права или он в CALENDAR_MANAGERS."""
    if user is None or not user.is_authenticated:
        return set()
    emp = _employee(user)
    roles = set()
    if emp is None or emp.is_owner:
        roles.add(Role.OWNER)
    if emp is not None and emp.is_observer:
        roles.add(Role.OBSERVER)
    if user.is_superuser or (emp is not None and emp.is_admin):
        roles.add(Role.ADMIN)
    if with_manager and (reads_all_calendars(user) or user.received_delegations.exists()):
        roles.add(Role.MANAGER)
    return roles


def is_admin(user):
    return Role.ADMIN in get_roles(user, with_manager=False)


def display_name(user):
    return user.get_full_name() or user.get_username()


def _merge(a, b):
    return {k: bool(a.get(k)) or bool(b.get(k)) for k in ("read", "change")}


class Access:
    """
    Права одного пользователя. Делегирования загружаются один раз,
    поэтому объект удобно создавать на запрос и проверять им много событий.
    """

    def __init__(self, user):
        self.user = user
        self.roles = get_roles(user, with_manager=False)
        self._by_owner = {}  # owner_id -> права на все события владельца
        self._by_event = {}  # event_id -> права на одно событие
        self._create_for = set()  # владельцы, в чьих календарях можно создавать
        self._read_all = bool(self.roles) and reads_all_calendars(user)  # чтение всех календарей
        if self.roles:
            for d in Delegation.objects.filter(manager=user):
                flags = {"read": d.can_read, "change": d.can_change}
                if d.event_id:
                    self._by_event[d.event_id] = _merge(self._by_event.get(d.event_id, NO_ACCESS), flags)
                else:
                    self._by_owner[d.owner_id] = _merge(self._by_owner.get(d.owner_id, NO_ACCESS), flags)
                    if d.can_create:
                        self._create_for.add(d.owner_id)
            if self._by_owner or self._by_event or self._read_all:
                self.roles.add(Role.MANAGER)

    # ---- роли ----
    @property
    def is_authenticated(self):
        return self.user is not None and self.user.is_authenticated

    @property
    def is_admin(self):
        return Role.ADMIN in self.roles

    @property
    def is_owner(self):
        return Role.OWNER in self.roles

    @property
    def is_observer(self):
        return Role.OBSERVER in self.roles

    @property
    def can_delegate(self):
        return self.is_owner or self.is_admin

    @property
    def can_manage_roles(self):
        return self.is_admin

    # ---- события ----
    def perms(self, event):
        """Права на событие: {"read", "change"} — объединение прав всех ролей."""
        if not self.is_authenticated:
            return dict(NO_ACCESS)
        # администратор и наблюдатель — все события; владелец — свои
        if self.is_admin or self.is_observer or (event.owner_id and event.owner_id == self.user.pk):
            return dict(FULL)
        # менеджер — по делегированным правам
        result = _merge(
            self._by_owner.get(event.owner_id, NO_ACCESS),
            self._by_event.get(event.pk, NO_ACCESS),
        )
        if self._read_all:
            result = _merge(result, READ_ONLY)
        # приглашённые всегда могут прочитать событие, на которое их позвали
        if not result["read"] and event.pk and any(u.pk == self.user.pk for u in event.invitees.all()):
            result = _merge(result, READ_ONLY)
        # гость, выбранный из адресной книги AD/LDAP, — тоже приглашённый (сопоставляем по e-mail)
        if not result["read"] and self._email_key and self._email_key in (getattr(event, "directory_guests", "") or ""):
            result = _merge(result, READ_ONLY)
        return result

    @property
    def _email_key(self):
        email = (getattr(self.user, "email", "") or "").strip().lower()
        return f",{email}," if email else ""

    def filter_events(self, qs):
        """Оставляет в queryset только события, которые пользователь может читать."""
        if not self.is_authenticated:
            return qs.none()
        if self.is_admin or self.is_observer or self._read_all:
            return qs
        cond = Q(owner=self.user) | Q(invitees=self.user)
        if self._email_key:
            cond |= Q(directory_guests__contains=self._email_key)
        owners = [oid for oid, f in self._by_owner.items() if f["read"]]
        events = [eid for eid, f in self._by_event.items() if f["read"]]
        if owners:
            cond |= Q(owner_id__in=owners)
        if events:
            cond |= Q(pk__in=events)
        return qs.filter(cond).distinct()

    # ---- запись (создание) ----
    def creatable_owners(self):
        """Пользователи, в чьих календарях можно создавать события."""
        User = get_user_model()
        if not self.is_authenticated:
            return User.objects.none()
        if self.is_admin:
            # администратор — в любом календаре (у кого есть свой календарь) и в своём
            qs = User.objects.filter(is_active=True).filter(
                Q(employee__is_owner=True) | Q(employee__isnull=True) | Q(pk=self.user.pk)
            )
        else:
            ids = set(self._create_for)
            if self.is_owner:
                ids.add(self.user.pk)
            qs = User.objects.filter(pk__in=ids, is_active=True)
        return qs.distinct().order_by("first_name", "last_name", "username")

    def can_create_for(self, owner):
        return owner is not None and self.creatable_owners().filter(pk=owner.pk).exists()

    def me(self):
        """Данные о текущем пользователе для интерфейса."""
        owners = list(self.creatable_owners())
        order = [Role.OWNER, Role.MANAGER, Role.OBSERVER, Role.ADMIN]
        return {
            "id": self.user.pk,
            "name": display_name(self.user),
            "roles": [{"code": r.value, "label": r.label} for r in order if r in self.roles],
            "can_create": bool(owners),
            "can_delegate": self.can_delegate,
            "can_manage_roles": self.can_manage_roles,
            "creatable_owners": [{"id": u.pk, "name": display_name(u)} for u in owners],
            "settings": get_preferences(self.user),
        }


def api_login_required(view):
    """
    Как login_required, но для API: 401 в JSON вместо редиректа на страницу входа.
    Заодно «не найдено» (Http404) отдаётся как JSON, а не HTML-страница.
    """

    @wraps(view)
    def wrapper(request, *args, **kwargs):
        if not request.user.is_authenticated:
            return JsonResponse({"errors": {"__all__": ["Требуется вход в систему."]}}, status=401)
        try:
            return view(request, *args, **kwargs)
        except Http404:
            return JsonResponse({"errors": {"__all__": ["Не найдено."]}}, status=404)

    return wrapper


def forbidden(message="Недостаточно прав."):
    return JsonResponse({"errors": {"__all__": [message]}}, status=403)
