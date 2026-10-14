import json
import secrets

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.db import IntegrityError, transaction
from django.http import HttpResponseBadRequest, JsonResponse
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from events.models import Event
from models_employee.permissions import Access, api_login_required, forbidden

from .invitations import build_messages, recipients, send_messages
from .models import ExternalCalendar, GuestDelivery, SyncedEvent
from .providers import ProviderError
from .providers import google as google_api
from .push import suppress_push
from .sync import CALDAV_SERVERS, build_provider, sync_calendar

P = ExternalCalendar.Provider
SESSION_GOOGLE = "integrations_google_tokens"
SESSION_STATE = "integrations_google_state"

HELP = {
    P.GOOGLE: "Нажмите «Войти через Google» и разрешите доступ к календарю.",
    P.EXCHANGE: "Корпоративная почта Microsoft Exchange (календарь Outlook). Адрес сервера подставлен; "
    "можно указать просто имя, например email.yanao.ru. Логин — DOMAIN\\пользователь или адрес почты, "
    "пароль — доменный (тот же, что для входа в Outlook). События отправляются в Exchange сразу при сохранении.",
    P.YANDEX: "Логин — адрес @yandex.ru. Пароль — пароль приложения для Календаря: "
    "Яндекс ID → Безопасность → Пароли приложений (id.yandex.ru/security/app-passwords). "
    "Новый пароль начинает работать в течение 2–3 часов.",
    P.MAILRU: "Логин — полный адрес ящика. Пароль — пароль для внешнего приложения: "
    "account.mail.ru/user/2-step-auth/passwords/. Если календари не находятся, вставьте в «Адрес сервера» "
    "ссылку «Синхронизация через CalDav» из меню нужного календаря на calendar.mail.ru.",
    P.RAMBLER: "Логин — адрес @rambler.ru и пароль (или пароль приложения). Если подключение не удаётся, "
    "укажите адрес CalDAV-сервера из настроек Рамблер Календаря. Пригласить человека с адресом "
    "на Рамблере можно и без подключения — через поле «Гости» в событии.",
    P.CALDAV: "Любой сервер с поддержкой CalDAV (Nextcloud, iCloud, Zimbra и др.): адрес сервера, логин и пароль.",
}


def _json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _bad(field, message, status=400):
    return JsonResponse({"errors": {field: [message]}}, status=status)


def _google_configured():
    return bool(getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", "") and getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""))


def _can_connect(user):
    """Подключать внешние календари к своему календарю может его владелец (или администратор)."""
    access = Access(user)
    return access.is_owner or access.is_admin


def _redirect_uri(request):
    return request.build_absolute_uri(reverse("integrations:google-callback"))


# ---------------------------------------------------------------- список и подключение


@api_login_required
@require_http_methods(["GET", "POST"])
def calendars(request):
    """
    GET  /api/integrations/ — подключённые календари и доступные сервисы
    POST /api/integrations/ — подключить календарь (после /discover/) и сразу синхронизировать
    """
    if not _can_connect(request.user):
        return forbidden("Подключать внешние календари может владелец календаря.")

    if request.method == "GET":
        providers = [
            {
                "code": code,
                "label": label,
                "auth": "oauth" if code == P.GOOGLE else "password",
                "server_url": CALDAV_SERVERS.get(code, ""),
                "available": _google_configured() if code == P.GOOGLE else True,
                "help": HELP[code],
            }
            for code, label in P.choices
        ]
        return JsonResponse(
            {
                "calendars": [c.to_dict() for c in request.user.external_calendars.all()],
                "providers": providers,
                "google_pending": bool(request.session.get(SESSION_GOOGLE)),
                "colors": [{"code": c, "label": l} for c, l in Event.Color.choices],
                "directions": [{"code": c, "label": l} for c, l in ExternalCalendar.Direction.choices],
            }
        )

    data = _json(request)
    if data is None:
        return _bad("__all__", "Некорректный JSON.")
    provider = data.get("provider")
    if provider not in P.values:
        return _bad("provider", "Выберите сервис.")
    if not data.get("calendar_id"):
        return _bad("calendar_id", "Выберите календарь.")
    direction = data.get("direction") or ExternalCalendar.Direction.BOTH
    if direction not in ExternalCalendar.Direction.values:
        return _bad("direction", "Неизвестное направление синхронизации.")
    color = data.get("color") or Event.Color.GRAY
    if color not in Event.Color.values:
        return _bad("color", "Неизвестный цвет.")

    cal = ExternalCalendar(
        user=request.user,
        provider=provider,
        calendar_id=str(data["calendar_id"])[:1000],
        calendar_name=str(data.get("calendar_name") or "")[:255],
        direction=direction,
        color=color,
        remote_read_only=bool(data.get("read_only")),
    )
    if provider == P.GOOGLE:
        tokens = request.session.get(SESSION_GOOGLE)
        if not tokens:
            return _bad("__all__", "Сначала войдите через Google.")
        cal.credentials = tokens
    else:
        cal.server_url = (data.get("server_url") or CALDAV_SERVERS.get(provider, "")).strip()[:500]
        cal.username = (data.get("username") or "").strip()[:255]
        if not cal.server_url or not cal.username or not data.get("password"):
            return _bad("__all__", "Укажите адрес сервера, логин и пароль.")
        cal.credentials = {"password": data["password"]}
    if cal.remote_read_only and direction != ExternalCalendar.Direction.IMPORT:
        cal.direction = ExternalCalendar.Direction.IMPORT  # в календарь только для чтения записывать нельзя
    duplicate = ExternalCalendar.objects.filter(user=request.user, provider=provider, calendar_id=cal.calendar_id)
    if duplicate.exists():
        return _bad("calendar_id", "Этот календарь уже подключён.")
    try:
        with transaction.atomic():  # точка сохранения: ошибка не ломает внешнюю транзакцию
            cal.save()
    except IntegrityError:
        return _bad("calendar_id", "Этот календарь уже подключён.")
    if provider == P.GOOGLE:
        request.session.pop(SESSION_GOOGLE, None)
    result = sync_calendar(cal)
    cal.refresh_from_db()
    return JsonResponse({"calendar": cal.to_dict(), "sync": result.to_dict()}, status=201)


@api_login_required
@require_POST
def discover(request):
    """
    POST /api/integrations/discover/ — проверить вход и получить список календарей.
    CalDAV: {"provider", "server_url", "username", "password"}; Google: {"provider": "google"} после входа через Google.
    """
    if not _can_connect(request.user):
        return forbidden("Подключать внешние календари может владелец календаря.")
    data = _json(request)
    if data is None:
        return _bad("__all__", "Некорректный JSON.")
    provider = data.get("provider")
    if provider not in P.values:
        return _bad("provider", "Выберите сервис.")
    probe = ExternalCalendar(user=request.user, provider=provider)
    if provider == P.GOOGLE:
        tokens = request.session.get(SESSION_GOOGLE)
        if not tokens:
            return _bad("__all__", "Сначала войдите через Google.")
        adapter = build_provider(probe, credentials=tokens, calendar_id="")
    else:
        server = (data.get("server_url") or CALDAV_SERVERS.get(provider, "")).strip()
        if not server or not data.get("username") or not data.get("password"):
            return _bad("__all__", "Укажите адрес сервера, логин и пароль.")
        adapter = build_provider(
            probe, server_url=server, username=data["username"].strip(),
            credentials={"password": data["password"]}, calendar_id="",
        )
    try:
        found = adapter.list_calendars()
    except ProviderError as exc:
        return _bad("__all__", str(exc))
    if provider == P.GOOGLE:
        request.session[SESSION_GOOGLE] = adapter.credentials  # access-токен мог обновиться
    connected = set(request.user.external_calendars.filter(provider=provider).values_list("calendar_id", flat=True))
    for c in found:
        c["connected"] = c["id"] in connected
    return JsonResponse({"calendars": found})


@api_login_required
@require_http_methods(["PATCH", "DELETE"])
def calendar_detail(request, pk):
    """
    PATCH  /api/integrations/<id>/ — изменить направление, цвет, вкл/выкл, пароль
    DELETE /api/integrations/<id>/?remove_events=1 — отключить (по умолчанию импортированные события остаются)
    """
    cal = get_object_or_404(ExternalCalendar, pk=pk, user=request.user)
    if request.method == "DELETE":
        if request.GET.get("remove_events") in ("1", "true"):
            ids = list(cal.links.filter(origin=SyncedEvent.Origin.REMOTE).values_list("event_id", flat=True))
            with suppress_push():  # календарь отключается — удалять в нём события не нужно
                Event.objects.filter(pk__in=[i for i in ids if i], owner=request.user).delete()
        cal.delete()
        return JsonResponse({"deleted": pk})

    data = _json(request)
    if data is None:
        return _bad("__all__", "Некорректный JSON.")
    if "direction" in data:
        if data["direction"] not in ExternalCalendar.Direction.values:
            return _bad("direction", "Неизвестное направление синхронизации.")
        if cal.remote_read_only and data["direction"] != ExternalCalendar.Direction.IMPORT:
            return _bad("direction", "Календарь доступен только для чтения — возможен только импорт.")
        cal.direction = data["direction"]
    if "color" in data:
        if data["color"] not in Event.Color.values:
            return _bad("color", "Неизвестный цвет.")
        cal.color = data["color"]
    if "enabled" in data:
        cal.enabled = bool(data["enabled"])
    if data.get("password") and cal.provider != P.GOOGLE:
        cal.credentials = {"password": data["password"]}
        cal.last_error = ""
    cal.save()
    return JsonResponse({"calendar": cal.to_dict()})


@api_login_required
@require_POST
def sync_one(request, pk):
    """POST /api/integrations/<id>/sync/ — синхронизировать сейчас."""
    cal = get_object_or_404(ExternalCalendar, pk=pk, user=request.user)
    result = sync_calendar(cal)
    cal.refresh_from_db()
    return JsonResponse({"calendar": cal.to_dict(), "sync": result.to_dict()})


@api_login_required
@require_POST
def sync_all(request):
    """POST /api/integrations/sync/ — синхронизировать все включённые календари пользователя."""
    out = []
    for cal in request.user.external_calendars.filter(enabled=True):
        result = sync_calendar(cal)
        cal.refresh_from_db()
        out.append({"calendar": cal.to_dict(), "sync": result.to_dict()})
    return JsonResponse({"results": out})


# ---------------------------------------------------------------- Google OAuth


@login_required
@require_GET
def google_connect(request):
    """Перенаправляет на страницу согласия Google."""
    if not _google_configured():
        return HttpResponseBadRequest("Интеграция с Google не настроена: задайте GOOGLE_OAUTH_CLIENT_ID и GOOGLE_OAUTH_CLIENT_SECRET.")
    state = secrets.token_urlsafe(24)
    request.session[SESSION_STATE] = state
    return redirect(google_api.authorization_url(settings.GOOGLE_OAUTH_CLIENT_ID, _redirect_uri(request), state))


@login_required
@require_GET
def google_callback(request):
    """Google возвращает сюда code; меняем его на токены и возвращаемся в календарь выбирать календарь Google."""
    target = reverse("events:calendar")
    state = request.session.pop(SESSION_STATE, None)
    if request.GET.get("error"):
        return redirect(f"{target}?integrations=google-denied")
    if not state or not secrets.compare_digest(state, request.GET.get("state", "")) or not request.GET.get("code"):
        return redirect(f"{target}?integrations=google-error")
    try:
        tokens = google_api.exchange_code(
            settings.GOOGLE_OAUTH_CLIENT_ID, settings.GOOGLE_OAUTH_CLIENT_SECRET, _redirect_uri(request), request.GET["code"]
        )
    except ProviderError:
        return redirect(f"{target}?integrations=google-error")
    request.session[SESSION_GOOGLE] = {
        "refresh_token": tokens.get("refresh_token", ""),
        "access_token": tokens.get("access_token", ""),
        "expires_at": tokens.get("expires_at", 0),
    }
    return redirect(f"{target}?integrations=google")


# ---------------------------------------------------------------- приглашения по e-mail


@api_login_required
@require_POST
def invite(request, pk):
    """
    POST /api/events/<id>/invite/ — отправить приглашения (.ics) приглашённым и гостям.
    {"cancel": true} — отправить отмену (перед удалением события). Нужно право «Изменение».
    """
    access = Access(request.user)
    event = get_object_or_404(Event.objects.select_related("owner").prefetch_related("invitees"), pk=pk)
    perms = access.perms(event)
    if not perms["read"]:
        return JsonResponse({"errors": {"__all__": ["Не найдено."]}}, status=404)
    if not perms["change"]:
        return forbidden("Нет права на изменение этого события.")
    data = _json(request) or {}
    if not recipients(event):
        return JsonResponse({"sent": [], "message": "Некому отправлять: нет приглашённых с e-mail и гостей."})
    # участникам из адресной книги событие доставляется автоматически — второй раз не пишем
    auto = set(GuestDelivery.objects.filter(event=event).values_list("email", flat=True))
    to = [email for _, email in recipients(event) if email.lower() not in auto]
    if not to:
        return JsonResponse({"sent": [], "failed": {}, "message": "Все участники уже получили событие автоматически."})
    sent, failed = send_messages(build_messages(event, to, "CANCEL" if data.get("cancel") else "REQUEST"))
    if failed and not sent:
        reason = next(iter(failed.values()))
        return JsonResponse({"errors": {"__all__": [f"Не удалось отправить письма: {reason}."]}, "failed": failed}, status=502)
    return JsonResponse({"sent": sent, "failed": failed})
