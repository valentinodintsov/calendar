import json
import os
from datetime import datetime, time, timedelta

from django.apps import apps
from django.contrib.auth import get_user_model
from django.contrib.auth.decorators import login_required
from django.contrib.staticfiles import finders
from django.db import transaction
from django.db.models import Q
from django.http import FileResponse, Http404, HttpResponse, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.utils import timezone
from django.utils.dateparse import parse_date
from django.utils.http import content_disposition_header
from django.views.decorators.csrf import ensure_csrf_cookie
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from models_employee.permissions import Access, api_login_required, forbidden

from . import report
from .forms import EventForm
from .models import Attachment, Event, user_display_name

MAX_UPLOAD_SIZE = 20 * 1024 * 1024  # 20 МБ на файл
MAX_FILES_PER_EVENT = 20


def _users_for_select():
    users = get_user_model().objects.filter(is_active=True).order_by("first_name", "last_name", "username")
    return [{"id": u.pk, "name": user_display_name(u), "email": u.email} for u in users]


def _static_version():
    """
    Метка для адресов скрипта и стилей (?v=…): время последнего изменения файлов. Без неё браузер после
    обновления календаря ещё несколько часов берёт старый скрипт из кэша, и новые кнопки страницы не работают.
    """
    paths = [finders.find(name) for name in ("events/calendar.js", "events/calendar.css")]
    return max((int(os.path.getmtime(p)) for p in paths if p), default=0)


@login_required
@ensure_csrf_cookie
def calendar_view(request):
    """Главная страница — одностраничный календарь."""
    return render(
        request,
        "events/calendar.html",
        {
            "colors": Event.Color.choices,
            "reminder_choices": Event.REMINDER_CHOICES,
            "users": _users_for_select(),
            "me": Access(request.user).me(),
            "max_upload_mb": MAX_UPLOAD_SIZE // (1024 * 1024),
            "static_version": _static_version(),
        },
    )


def _events_qs():
    return Event.objects.select_related("owner").prefetch_related("invitees", "attachments")


def _form_errors(form):
    return {field: [str(e) for e in errs] for field, errs in form.errors.items()}


def _read_json(request):
    try:
        data = json.loads(request.body or b"{}")
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None
    return data if isinstance(data, dict) else None


def _bad_json():
    return JsonResponse({"errors": {"__all__": ["Некорректный JSON."]}}, status=400)


def _day_start(d):
    return timezone.make_aware(datetime.combine(d, time.min))


def _parse_date(value):
    try:
        return parse_date(value or "")
    except ValueError:  # похоже на дату, но такой даты нет (2026-02-30)
        return None


def _in_range(qs, start=None, end=None):
    """События, пересекающиеся с диапазоном дат [start, end)."""
    if start:
        range_start = _day_start(start)
        # событие попадает в диапазон, если заканчивается не раньше его начала
        qs = qs.filter(Q(end__gte=range_start) | Q(end__isnull=True, start__gte=range_start))
    if end:
        qs = qs.filter(start__lt=_day_start(end))
    return qs


def _event_for(access, pk, action):
    """
    Событие с проверкой прав (action: "read" или "change").
    Без права чтения — 404, чтобы не раскрывать сам факт существования события;
    без права изменения — 403.
    """
    event = get_object_or_404(_events_qs(), pk=pk)
    perms = access.perms(event)
    if not perms["read"]:
        raise Http404("Событие не найдено")
    if not perms[action]:
        return event, perms, forbidden("Нет права на изменение этого события.")
    return event, perms, None


def _saved(access, event, status=200):
    # перечитываем с prefetch, чтобы в ответе были актуальные приглашённые и вложения
    event = _events_qs().get(pk=event.pk)
    data = event.to_dict(access.perms(event))
    report = _delivery_report(event.pk)
    if report is not None:
        data["delivery"] = report  # кому ушло событие из участников адресной книги
    return JsonResponse(data, status=status)


def _delivery_report(pk):
    if not apps.is_installed("integrations"):
        return None
    from integrations.push import pop_delivery_report

    return pop_delivery_report(pk)


def _files_changed(request, pk, data):
    """
    После загрузки или удаления файлов: приглашения участникам (отложенные до файлов) и обновления с новым набором
    файлов. ?deliver=0 — не отправлять: следом будут ещё операции с файлами этого события.
    """
    if request.GET.get("deliver") == "0" or not apps.is_installed("integrations"):
        return
    from integrations.push import files_changed

    report = files_changed(pk)
    if report is not None:
        data["delivery"] = report


def _mark_directory_guests(event, previous_guests=()):
    """
    Гости, вписанные вручную, но найденные в адресной книге AD/LDAP, тоже получают событие в свой календарь.
    Проверяем только новых гостей (запрос к серверу каталога), уже отмеченные не трогаем.
    """
    if not apps.is_installed("directory"):
        return
    marked = set(event.directory_guest_list())
    before = {e.lower() for e in previous_guests}
    fresh = [e for e in event.guest_list() if e.lower() not in marked and e.lower() not in before]
    if not fresh:
        return
    from directory.search import emails_in_directory

    found = emails_in_directory(fresh)
    if found:
        event.directory_guests = Event.pack_directory_guests(marked | found)


def _get_user(user_id):
    try:
        return get_user_model().objects.get(pk=int(user_id), is_active=True)
    except (TypeError, ValueError, get_user_model().DoesNotExist):
        return None


@api_login_required
@require_http_methods(["GET", "POST"])
def events_collection(request):
    """
    GET  /api/events/?start=YYYY-MM-DD&end=YYYY-MM-DD  — события, которые можно читать, в диапазоне [start, end)
    GET  /api/events/?q=текст                          — поиск
    POST /api/events/                                  — создать событие, право «Запись» (owner — чей календарь)
    """
    access = Access(request.user)

    if request.method == "POST":
        data = _read_json(request)
        if data is None:
            return _bad_json()
        owner = _get_user(data.get("owner")) if data.get("owner") else request.user
        if owner is None:
            return JsonResponse({"errors": {"owner": ["Владелец не найден."]}}, status=400)
        if not access.can_create_for(owner):
            return forbidden("Нет права создавать события в этом календаре.")
        form = EventForm(data=data)
        if not form.is_valid():
            return JsonResponse({"errors": _form_errors(form)}, status=400)
        # одна транзакция: отправка во внешние календари (on_commit) начнётся, когда сохранены и участники
        event = form.save(commit=False)
        event.files_pending = bool(data.get("files_pending"))
        _mark_directory_guests(event)  # запрос к каталогу — до транзакции
        with transaction.atomic():
            event.owner = owner
            event.created_by = request.user
            event.save()
            form.save_m2m()
            event.invitees.remove(owner)  # владелец — не приглашённый в собственное событие
        return _saved(access, event, status=201)

    start = _parse_date(request.GET.get("start"))
    end = _parse_date(request.GET.get("end"))
    qs = _in_range(access.filter_events(_events_qs()), start, end)

    query = Event.normalize(request.GET.get("q", "").strip())
    if query:
        # регистронезависимо, в т.ч. для кириллицы (см. Event.search_text)
        qs = qs.filter(search_text__contains=query)

    return JsonResponse({"events": [e.to_dict(access.perms(e)) for e in qs[:2000]]})


@api_login_required
@require_GET
def events_report(request):
    """
    GET /api/events/report/?start=YYYY-MM-DD&end=YYYY-MM-DD — файл «Отчет по событиям.docx»:
    все события, которые пользователь может читать (то же, что он видит в календаре), за период
    с start по end включительно.
    """
    start = _parse_date(request.GET.get("start"))
    end = _parse_date(request.GET.get("end"))
    errors = {}
    if not start:
        errors["start"] = ["Укажите начало периода."]
    if not end:
        errors["end"] = ["Укажите окончание периода."]
    elif start and end < start:
        errors["end"] = ["Окончание периода не может быть раньше начала."]
    if errors:
        return JsonResponse({"errors": errors}, status=400)
    events = _in_range(Access(request.user).filter_events(_events_qs()), start, end + timedelta(days=1))
    response = HttpResponse(report.build_report(events, start, end, request.user), content_type=report.CONTENT_TYPE)
    response["Content-Disposition"] = content_disposition_header(True, report.FILENAME)
    response["Cache-Control"] = "no-store"
    return response


@api_login_required
@require_http_methods(["GET", "PUT", "PATCH", "DELETE"])
def event_detail(request, pk):
    """
    GET    /api/events/<id>/  — получить (чтение)
    PUT    /api/events/<id>/  — заменить (изменение)
    PATCH  /api/events/<id>/  — частично изменить, например перенести (изменение)
    DELETE /api/events/<id>/  — удалить вместе с вложениями (изменение)
    """
    access = Access(request.user)

    if request.method == "GET":
        event, perms, denied = _event_for(access, pk, "read")
        return denied or JsonResponse(event.to_dict(perms))

    event, perms, denied = _event_for(access, pk, "change")
    if denied:
        return denied

    if request.method == "DELETE":
        with transaction.atomic():
            event.delete()
        return JsonResponse({"deleted": pk})

    data = _read_json(request)
    if data is None:
        return _bad_json()
    if request.method == "PATCH":
        merged = event.to_dict()
        merged["invitees"] = [u["id"] for u in merged["invitees"]]
        merged["owner"] = event.owner_id
        merged.update(data)
        data = merged

    new_owner = None
    if data.get("owner") not in (None, "") and str(data["owner"]) != str(event.owner_id):
        # передать событие в другой календарь может только администратор
        if not access.is_admin:
            return forbidden("Сменить владельца события может только администратор.")
        new_owner = _get_user(data["owner"])
        if new_owner is None:
            return JsonResponse({"errors": {"owner": ["Владелец не найден."]}}, status=400)

    form = EventForm(data=data, instance=event)
    if not form.is_valid():
        return JsonResponse({"errors": _form_errors(form)}, status=400)
    previous_guests = Event.objects.filter(pk=event.pk).values_list("guest_emails", flat=True).first() or ""
    event = form.save(commit=False)
    event.files_pending = bool(data.get("files_pending"))
    _mark_directory_guests(event, Event(guest_emails=previous_guests).guest_list())
    with transaction.atomic():
        if new_owner:
            event.owner = new_owner
        event.save()
        form.save_m2m()
        if event.owner_id:
            event.invitees.remove(event.owner_id)
    return _saved(access, event)


@api_login_required
@require_POST
def attachment_upload(request, pk):
    """
    POST /api/events/<id>/attachments/ — multipart, поле files (можно несколько).
    После загрузки участникам уходит приглашение с файлами (или обновление), итог — в поле delivery.
    Нужно право изменения события. Автор нового события может прикрепить к нему файлы
    и без права изменения — если у него есть право записи в этот календарь.
    """
    access = Access(request.user)
    event = get_object_or_404(_events_qs(), pk=pk)
    perms = access.perms(event)
    is_author = event.created_by_id == request.user.pk and access.can_create_for(event.owner)
    if not (perms["change"] or is_author):
        if not perms["read"]:
            raise Http404("Событие не найдено")
        return forbidden("Нет права на изменение этого события.")
    files = request.FILES.getlist("files")
    error = ""
    if not files:
        error = "Выберите файлы."
    elif event.attachments.count() + len(files) > MAX_FILES_PER_EVENT:
        error = f"К событию можно прикрепить не больше {MAX_FILES_PER_EVENT} файлов."
    else:
        too_big = [f.name for f in files if f.size > MAX_UPLOAD_SIZE]
        if too_big:
            error = f"Файл больше {MAX_UPLOAD_SIZE // (1024 * 1024)} МБ: {', '.join(too_big)}"
    if error:
        data = {"errors": {"files": [error]}}
        _files_changed(request, event.pk, data)  # файлы не приняты — приглашения, ждавшие их, уходят без файлов
        return JsonResponse(data, status=400)

    created = [
        Attachment.objects.create(event=event, file=f, name=f.name[:255], size=f.size)
        for f in files
    ]
    data = {"attachments": [a.to_dict() for a in created]}
    _files_changed(request, event.pk, data)
    return JsonResponse(data, status=201)


@api_login_required
@require_http_methods(["DELETE"])
def attachment_delete(request, pk):
    """DELETE /api/attachments/<id>/ — нужно право изменения события."""
    attachment = get_object_or_404(Attachment, pk=pk)
    _, _, denied = _event_for(Access(request.user), attachment.event_id, "change")
    if denied:
        return denied
    attachment.delete()
    data = {"deleted": pk}
    _files_changed(request, attachment.event_id, data)
    return JsonResponse(data)


@api_login_required
@require_GET
def attachment_download(request, pk):
    """GET /api/attachments/<id>/download/ — нужно чтение события; всегда отдаём как скачивание."""
    attachment = get_object_or_404(Attachment, pk=pk)
    _, _, denied = _event_for(Access(request.user), attachment.event_id, "read")
    if denied:
        return denied
    try:
        fh = attachment.file.open("rb")
    except FileNotFoundError:
        raise Http404("Файл не найден")
    return FileResponse(fh, as_attachment=True, filename=attachment.name)


@api_login_required
@require_GET
def reminders(request):
    """
    GET /api/reminders/ — напоминания текущего пользователя, которые пора показать (или покажутся в ближайшие 15 минут).
    Только события, где он владелец или участник. Ответ: {"reminders": [{"id", "title", "start", "location", "remind_at", "minutes"}]}
    """
    now = timezone.now()
    user = request.user
    email = (user.email or "").strip().lower()
    mine = Q(owner=user) | Q(invitees=user)
    if email:
        mine |= Q(directory_guests__contains=f",{email},")
    longest = max(m for m, _ in Event.REMINDER_CHOICES)
    qs = (
        Event.objects.filter(mine, reminder_minutes__isnull=False)
        .filter(start__gte=now - timedelta(minutes=30), start__lte=now + timedelta(minutes=longest + 15))
        .distinct()
    )
    items = []
    for ev in qs:
        remind_at = ev.start - timedelta(minutes=ev.reminder_minutes)
        if remind_at <= now + timedelta(minutes=15):
            items.append({
                "id": ev.pk,
                "title": ev.title,
                "start": timezone.localtime(ev.start).strftime("%Y-%m-%dT%H:%M"),
                "all_day": ev.all_day,
                "location": ev.location,
                "minutes": ev.reminder_minutes,
                "remind_at": remind_at.isoformat(),
            })
    items.sort(key=lambda r: r["remind_at"])
    return JsonResponse({"reminders": items, "now": now.isoformat()})
