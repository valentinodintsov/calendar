# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Вход в календарь по учётной записи Active Directory.

Пароль проверяет контроллер домена: календарь подключается к нему по LDAP от имени входящего
пользователя (служебная учётная запись не нужна) и читает его же запись. Пароли в базе календаря
не хранятся. При первом входе создаётся пользователь календаря, при следующих — обновляются
имя, e-mail и должность. Пользователь привязывается к записи домена по objectGUID, поэтому
переименование логина в домене не теряет календарь, а повторно выданный логин не получает чужой.

Настройки — AD_AUTH_* в settings.py.
"""

from __future__ import annotations

import logging
import re
import uuid
from dataclasses import dataclass

from django.conf import settings
from django.contrib.auth import get_user_model
from django.contrib.auth.backends import ModelBackend
from django.db import transaction

from directory.ldap_client import AD, ConnectionSettings, DirectoryClient, DirectoryError, _text, escape_filter

from .models import Employee

log = logging.getLogger(__name__)

# Вложенное членство в группах (LDAP_MATCHING_RULE_IN_CHAIN)
IN_CHAIN = "1.2.840.113556.1.4.1941"
USER_ATTRIBUTES = ["objectGUID", "sAMAccountName", "givenName", "sn", "displayName", "mail", "title"]
# Символы, недопустимые в sAMAccountName
LOGIN_RE = re.compile(r'[^\s"/\\\[\]:;|=,+*?<>@]{1,64}')

# Подкоды ошибки входа AD («AcceptSecurityContext error, data 775») → что сказать пользователю.
# 525 (нет такого пользователя) и 52e (неверный пароль) намеренно не различаются.
BIND_MESSAGES = {
    "530": "Вход в домен сейчас запрещён для этой учётной записи (ограничение по времени).",
    "531": "Вход в домен с этого сервера запрещён для этой учётной записи.",
    "532": "Срок действия пароля истёк. Смените пароль в домене и войдите снова.",
    "533": "Учётная запись отключена в домене.",
    "701": "Срок действия учётной записи в домене истёк.",
    "773": "Требуется сменить пароль в домене. Смените пароль и войдите снова.",
    "775": "Учётная запись заблокирована в домене. Обратитесь к администратору.",
}
UNAVAILABLE = "Сервер авторизации (Active Directory) недоступен. Попробуйте позже."
FAILED = "Не удалось проверить учётную запись в домене. Обратитесь к администратору."


class ADAuthError(Exception):
    """Вход невозможен. message — текст для пользователя; пусто — «неверное имя пользователя или пароль»."""

    def __init__(self, message=""):
        super().__init__(message)
        self.message = message


@dataclass
class ADAccount:
    guid: str
    login: str
    dn: str = ""
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    title: str = ""
    is_admin: bool | None = None  # None — ни группа, ни список администраторов не назначают роль, её не трогаем


# ---------------------------------------------------------------- настройки


def domain() -> str:
    return (getattr(settings, "AD_AUTH_DOMAIN", "") or "").strip().lower()


def base_dn() -> str:
    explicit = (getattr(settings, "AD_AUTH_BASE_DN", "") or "").strip()
    return explicit or ",".join(f"DC={part}" for part in domain().split(".") if part)


def _netbios() -> str:
    return (getattr(settings, "AD_AUTH_NETBIOS_DOMAIN", "") or domain().split(".")[0]).strip().lower()


def connection_settings(bind_user: str = "", password: str = "") -> ConnectionSettings:
    return ConnectionSettings(
        kind=AD,
        host=(getattr(settings, "AD_AUTH_SERVERS", "") or "").strip() or domain(),
        port=getattr(settings, "AD_AUTH_PORT", None) or None,
        security=getattr(settings, "AD_AUTH_SECURITY", "ssl"),
        verify_certificate=getattr(settings, "AD_AUTH_VERIFY_CERT", True),
        ca_cert_file=getattr(settings, "AD_AUTH_CA_CERT_FILE", "") or "",
        bind_dn=bind_user,
        password=password,
        base_dn=base_dn(),
        timeout=getattr(settings, "AD_AUTH_TIMEOUT", 10),
    )


def parse_login(raw: str) -> str | None:
    """
    «ivanov», «YANAO\\ivanov», «ivanov@yanao.ru» → «ivanov». Чужой домен и недопустимые символы → None.
    """
    value = (raw or "").strip()
    if "\\" in value:
        prefix, _, value = value.partition("\\")
        if prefix.strip().lower() not in (_netbios(), domain()):
            return None
    elif "@" in value:
        value, _, suffix = value.rpartition("@")
        if suffix.strip().lower() != domain():
            return None
    return value if LOGIN_RE.fullmatch(value) else None


def setting_logins(name: str) -> set[str]:
    """Логины из настройки-списка через запятую («ivanov», «ivanov@yanao.ru», «YANAO\\ivanov») в нижнем регистре."""
    raw = getattr(settings, name, "") or ""
    items = raw.split(",") if isinstance(raw, str) else raw
    logins = set()
    for item in items:
        if not item.strip():
            continue
        login = parse_login(item)
        if login is None:
            log.error("%s: «%s» — не логин домена %s, пропущен", name, item.strip(), domain())
        else:
            logins.add(login.lower())
    return logins


def admin_users() -> set[str]:
    return setting_logins("AD_AUTH_ADMIN_USERS")


def calendar_managers() -> set[str]:
    return setting_logins("CALENDAR_MANAGERS")


# ---------------------------------------------------------------- запрос к контроллеру домена


def _bind_error(exc: DirectoryError) -> ADAuthError:
    if exc.network:
        return ADAuthError(UNAVAILABLE)
    text = f"{exc} {exc.__cause__ or ''}".lower()
    code = re.search(r"data\s+([0-9a-f]{3,4})\b", text)
    if code:
        return ADAuthError(BIND_MESSAGES.get(code.group(1), ""))
    if "invalidcredentials" in text.replace(" ", "") or "неверное имя пользователя" in text:
        return ADAuthError("")
    return ADAuthError(FAILED)


def _entries(conn):
    return [r for r in (conn.response or []) if r.get("type") == "searchResEntry"]


def _group_dn(conn, group: str) -> str | None:
    """Группа задаётся полным DN или именем (sAMAccountName / cn)."""
    if "=" in group:
        return group
    name = escape_filter(group)
    conn.search(base_dn(), f"(&(objectClass=group)(|(sAMAccountName={name})(cn={name})))", attributes=["cn"], size_limit=2)
    found = _entries(conn)
    return found[0]["dn"] if len(found) == 1 else None


def _in_group(conn, user_dn: str, group: str) -> bool:
    """Состоит ли пользователь в группе, в том числе через вложенные группы."""
    from ldap3 import BASE
    from ldap3.core.exceptions import LDAPException, LDAPNoSuchObjectResult

    group_dn = _group_dn(conn, group)
    if not group_dn:
        log.error("Группа Active Directory «%s» не найдена (или имя неоднозначно)", group)
        return False
    group_filter = escape_filter(group_dn)
    try:
        conn.search(user_dn, f"(memberOf:{IN_CHAIN}:={group_filter})", search_scope=BASE, attributes=["cn"])
    except LDAPException:
        # LDAP-шлюз перед доменом (например, почтовый сервер) не понимает правило вложенных групп — только прямое членство
        try:
            conn.search(user_dn, f"(memberOf={group_filter})", search_scope=BASE, attributes=["cn"])
        except LDAPNoSuchObjectResult:
            log.error("Группа Active Directory «%s» не найдена", group)
            return False
    return bool(_entries(conn))


def fetch_account(login: str, password: str) -> ADAccount:
    """
    Проверяет пароль входом на контроллер домена и возвращает данные учётной записи.
    ADAuthError — если войти нельзя.
    """
    from ldap3.core.exceptions import LDAPException

    if not password:
        # простой вход LDAP с пустым паролем сервер считает анонимным и «успешным»
        raise ADAuthError("")
    cfg = connection_settings(f"{login}@{domain()}", password)
    try:
        conn = DirectoryClient(cfg).connect()
    except DirectoryError as exc:
        error = _bind_error(exc)
        if error.message in (UNAVAILABLE, FAILED):
            log.error("Вход через Active Directory (%s): %s", cfg.host, exc)
        raise error from exc
    try:
        conn.search(
            cfg.base_dn,
            f"(&(objectCategory=person)(objectClass=user)(sAMAccountName={escape_filter(login)}))",
            attributes=USER_ATTRIBUTES,
            size_limit=2,
        )
        found = _entries(conn)
        if len(found) != 1:
            log.error("Вход через Active Directory: запись «%s» не найдена в %s (найдено: %d)", login, cfg.base_dn, len(found))
            raise ADAuthError(FAILED)
        dn = found[0]["dn"]
        attrs = {k.lower(): v for k, v in (found[0].get("raw_attributes") or {}).items()}

        def first(name):
            return next((_text(v) for v in attrs.get(name.lower(), []) if _text(v)), "")

        raw_guid = (attrs.get("objectguid") or [b""])[0]
        if len(raw_guid) != 16:
            raise ADAuthError(FAILED)

        required = (getattr(settings, "AD_AUTH_REQUIRE_GROUP", "") or "").strip()
        if required and not _in_group(conn, dn, required):
            raise ADAuthError("У вашей учётной записи нет доступа к календарю. Обратитесь к администратору.")
        admin_group = (getattr(settings, "AD_AUTH_ADMIN_GROUP", "") or "").strip()
        account_login = first("sAMAccountName") or login
        is_admin = _in_group(conn, dn, admin_group) if admin_group else None
        if account_login.lower() in admin_users():
            is_admin = True

        return ADAccount(
            guid=str(uuid.UUID(bytes_le=raw_guid)),
            login=account_login,
            dn=dn,
            first_name=first("givenName"),
            last_name=first("sn"),
            email=first("mail") if "@" in first("mail") else "",
            title=first("title"),
            is_admin=is_admin,
        )
    except LDAPException as exc:
        log.exception("Вход через Active Directory: ошибка чтения записи «%s»", login)
        raise ADAuthError(FAILED) from exc
    finally:
        try:
            conn.unbind()
        except Exception:  # noqa: BLE001
            pass


# ---------------------------------------------------------------- пользователь календаря


@transaction.atomic
def sync_user(account: ADAccount):
    """Создаёт или обновляет пользователя календаря по учётной записи домена."""
    User = get_user_model()
    username = account.login.lower()
    linked = Employee.objects.select_related("user").filter(ad_guid=account.guid).first()
    if linked is not None:
        user = linked.user
        if user.username != username:  # логин переименован в домене
            if User.objects.filter(username__iexact=username).exclude(pk=user.pk).exists():
                log.error("Логин «%s» в календаре занят: пользователь «%s» не переименован", username, user.username)
            else:
                user.username = username
    else:
        user = User.objects.filter(username__iexact=username).first()
        if user is None:
            user = User(username=username)
            user.set_unusable_password()
        elif user.is_superuser or Employee.objects.filter(user=user).exclude(ad_guid=None).exists():
            # локальный суперпользователь или логин, раньше принадлежавший другому человеку в домене
            log.error("Вход через Active Directory: логин «%s» занят другой учётной записью календаря", username)
            raise ADAuthError("Этот логин уже занят в календаре другой учётной записью. Обратитесь к администратору.")

    # пустые значения из домена не затирают то, что заполнено в календаре
    for field_name, value in (("first_name", account.first_name[:150]), ("last_name", account.last_name[:150]),
                              ("email", account.email[:254])):
        if value:
            setattr(user, field_name, value)
    user.save()

    employee, _ = Employee.objects.get_or_create(user=user)
    employee.user = user  # Employee.save() меняет user.is_staff — пусть это будет тот же объект
    employee.ad_guid = account.guid
    if account.title:
        employee.position = account.title[:150]
    if account.is_admin is not None:
        employee.is_admin = account.is_admin
    employee.save()
    return user


class ActiveDirectoryBackend(ModelBackend):
    """Вход по логину и паролю домена. Права и роли — как у обычного пользователя Django."""

    def authenticate(self, request, username=None, password=None, **kwargs):
        if not domain() or not username or not password:
            return None
        login = parse_login(username)
        if login is None:
            return None
        try:
            user = sync_user(fetch_account(login, password))
        except ADAuthError as exc:
            if request is not None and exc.message:
                request.ad_auth_error = exc.message  # покажет форма входа
            return None
        return user if self.user_can_authenticate(user) else None


class LocalSuperuserBackend(ModelBackend):
    """
    Резервный вход по паролю из базы календаря — только для локальных суперпользователей
    (python manage.py createsuperuser), чтобы администрировать календарь, когда домен недоступен.
    """

    def authenticate(self, request, username=None, password=None, **kwargs):
        user = super().authenticate(request, username=username, password=password, **kwargs)
        return user if user is not None and user.is_superuser else None
