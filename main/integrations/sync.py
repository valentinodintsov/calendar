"""
Двусторонняя синхронизация собственного календаря пользователя с внешним календарём.

Для каждой пары «наше событие ↔ внешнее» хранится SyncedEvent с отпечатками содержимого
на момент последней синхронизации. Так видно, что и где изменилось:

  * событие новое во внешнем календаре   → создаём у нас (если можно импортировать);
  * событие новое у нас                  → создаём во внешнем (если можно экспортировать);
  * изменилось только с одной стороны    → переносим изменение на другую;
  * изменилось с обеих сторон (конфликт) → побеждает более позднее изменение;
  * удалено с одной стороны              → удаляем и с другой.

Синхронизируются события в окне [сейчас − INTEGRATIONS_SYNC_PAST_DAYS; сейчас + INTEGRATIONS_SYNC_FUTURE_DAYS].
Экземпляры повторяющихся событий импортируются как отдельные события «только для чтения»:
их изменения у нас при следующей синхронизации заменяются версией из внешнего календаря.
"""

from __future__ import annotations

import hashlib
import logging
from dataclasses import dataclass, field
from datetime import datetime, time, timedelta

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from events.models import Event

from .ics import EventData
from .guests import own_event_from_uid
from .models import ExternalCalendar, GuestDelivery, SyncedEvent
from .providers import AuthError, ConflictError, ProviderError
from .providers.caldav import CalDAVProvider
from .providers.exchange import ExchangeProvider
from .providers.google import GoogleProvider
from .push import suppress_push

log = logging.getLogger(__name__)

# Адреса серверов по умолчанию (можно изменить при подключении)
CALDAV_SERVERS = {
    ExternalCalendar.Provider.EXCHANGE: getattr(settings, "INTEGRATIONS_EXCHANGE_URL", ""),
    ExternalCalendar.Provider.YANDEX: "https://caldav.yandex.ru",
    ExternalCalendar.Provider.MAILRU: "https://calendar.mail.ru",
    ExternalCalendar.Provider.RAMBLER: "https://calendar.rambler.ru",
    ExternalCalendar.Provider.CALDAV: "",
}


def _tz():
    return timezone.get_default_timezone()


def sync_window(now=None):
    now = now or timezone.now()
    past = getattr(settings, "INTEGRATIONS_SYNC_PAST_DAYS", 30)
    future = getattr(settings, "INTEGRATIONS_SYNC_FUTURE_DAYS", 365)
    return now - timedelta(days=past), now + timedelta(days=future)


def build_provider(cal: ExternalCalendar, **overrides):
    """Адаптер для подключённого календаря (или для проверки параметров при подключении)."""
    provider = overrides.get("provider", cal.provider)
    creds = overrides.get("credentials", cal.credentials if cal.pk else {})
    calendar_id = overrides.get("calendar_id", cal.calendar_id)
    if provider == ExternalCalendar.Provider.GOOGLE:

        def save_tokens(new_creds):
            if cal.pk:
                cal.credentials = new_creds
                ExternalCalendar.objects.filter(pk=cal.pk).update(secret=cal.secret)

        return GoogleProvider(
            getattr(settings, "GOOGLE_OAUTH_CLIENT_ID", ""),
            getattr(settings, "GOOGLE_OAUTH_CLIENT_SECRET", ""),
            creds,
            calendar_id,
            _tz(),
            on_tokens=save_tokens,
        )
    if provider == ExternalCalendar.Provider.EXCHANGE:
        return ExchangeProvider(
            overrides.get("server_url", cal.server_url),
            overrides.get("username", cal.username),
            creds.get("password", ""),
            calendar_id,
            _tz(),
            auth=creds.get("auth", ""),
        )
    return CalDAVProvider(
        overrides.get("server_url", cal.server_url),
        overrides.get("username", cal.username),
        creds.get("password", ""),
        calendar_id,
        _tz(),
    )


# ---------------------------------------------------------------- преобразование событий


def local_uid(event: Event) -> str:
    domain = getattr(settings, "INTEGRATIONS_UID_DOMAIN", "") or "calendar.local"
    return f"calendar-event-{event.pk}@{domain}"


def event_to_data(event: Event, uid: str) -> EventData:
    return EventData(
        uid=uid,
        title=event.title,
        description=event.description,
        location=event.location,
        url=event.url,
        start=event.start,
        end=event.end or event.start,
        all_day=event.all_day,
        last_modified=event.updated_at,
        reminder=event.reminder_minutes if event.reminder_minutes is not None else -1,
    )


def apply_data(event: Event, data: EventData):
    tz = _tz()
    event.title = (data.title or "Без названия")[:200]
    event.description = data.description or ""
    event.location = (data.location or "")[:200]
    event.url = (data.url or "")[:500]
    if data.all_day:
        # у нас событие на весь день — с 00:00 первого дня до 23:59 последнего (в часовом поясе сервера)
        start_d = data.start.astimezone(tz).date() if data.start.tzinfo else data.start.date()
        end_d = data.end.astimezone(tz).date() if data.end and data.end.tzinfo else start_d
        event.start = datetime.combine(start_d, time.min, tzinfo=tz)
        event.end = datetime.combine(max(end_d, start_d), time(23, 59), tzinfo=tz)
    else:
        event.start = data.start
        event.end = data.end if data.end and data.end >= data.start else data.start
    event.all_day = data.all_day
    if data.reminder is not None:  # сервис хранит напоминание (Exchange) — переносим; иначе не трогаем
        event.reminder_minutes = data.reminder if data.reminder >= 0 else None


def fingerprint(data: EventData, with_reminder=False) -> str:
    return hashlib.sha1(repr(data.content_key(with_reminder)).encode("utf-8")).hexdigest()


def keeps_reminder(cal) -> bool:
    """Сервис хранит напоминание события (сравниваем и переносим его) — пока только Exchange."""
    return cal is not None and cal.provider == ExternalCalendar.Provider.EXCHANGE


def _local_fingerprint(event: Event, cal=None) -> str:
    # нормализуем через apply_data → event_to_data, чтобы сравнивать одинаково округлённые значения
    return fingerprint(event_to_data(event, ""), keeps_reminder(cal))


def _remote_fingerprint(data: EventData, cal=None) -> str:
    probe = Event()
    apply_data(probe, data)
    return fingerprint(event_to_data(probe, ""), keeps_reminder(cal))


# ---------------------------------------------------------------- участники и вложения из внешнего календаря


def _extra_key(data: EventData) -> str:
    if data.attendees is None and data.attachments is None:
        return ""
    people = sorted(e.lower() for e in data.attendees or [])
    files = sorted((a["id"], a.get("size", 0)) for a in data.attachments or [])
    return hashlib.sha1(repr((people, files)).encode("utf-8")).hexdigest()


def _apply_participants(event: Event, emails: list):
    """Участники из Exchange: сотрудники с учётной записью — в «Приглашённые», остальные — в «Гости»."""
    from django.contrib.auth import get_user_model

    owner_email = (event.owner.email or "").lower() if event.owner_id else ""
    emails = [e for e in dict.fromkeys(e.strip() for e in emails) if e and e.lower() != owner_email]
    users = {}
    if emails:
        cond = Q()
        for e in emails:
            cond |= Q(email__iexact=e)
        for u in get_user_model().objects.filter(cond, is_active=True):
            users.setdefault(u.email.lower(), u)
    invitees = [users[e.lower()] for e in emails if e.lower() in users]
    guests = [e for e in emails if e.lower() not in users]
    if {u.pk for u in invitees} != set(event.invitees.values_list("pk", flat=True)):
        event.invitees.set(invitees)
    guest_emails = ", ".join(guests)
    if event.guest_emails != guest_emails:
        event.guest_emails = guest_emails
        event.save(update_fields=["guest_emails"])


def _apply_attachments(provider, event: Event, files: list, result) -> bool:
    """Файлы из Exchange: новые скачиваем, удалённые там — удаляем у нас. False — что-то не скачалось."""
    from django.core.files.base import ContentFile

    from events.models import Attachment

    limit = getattr(settings, "INTEGRATIONS_MAX_ATTACHMENT_SIZE", 20 * 1024 * 1024)
    remote = {a["id"]: a for a in files}
    ok = True
    for att in event.attachments.exclude(external_id="").exclude(external_id__startswith="pending:"):
        if att.external_id not in remote:
            att.delete()  # удалён в Outlook
    have = set(event.attachments.exclude(external_id="").values_list("external_id", flat=True))
    for aid, meta in remote.items():
        if aid in have:
            continue
        if meta.get("size", 0) > limit:
            result.errors.append(f"«{event.title}»: файл {meta['name']} больше {limit // (1024 * 1024)} МБ — не загружен.")
            continue
        try:
            content = provider.get_attachment(aid)
        except ProviderError as exc:
            result.errors.append(f"«{event.title}»: файл {meta['name']} не загружен: {exc}")
            ok = False
            continue
        name = (meta["name"] or "файл")[:255]
        Attachment.objects.create(event=event, file=ContentFile(content, name=name), name=name, size=len(content), external_id=aid)
    return ok


def _import_extras(cal, provider, link, event: Event, data: EventData, result):
    """
    Участники и вложения события, созданного во внешнем календаре (Outlook), — к нам.
    Применяются, только когда изменились там: правки этих полей у нас до изменения в Outlook сохраняются.
    """
    key = _extra_key(data)
    if not key or key == link.extra_key or link.origin != SyncedEvent.Origin.REMOTE or not cal.can_import:
        return
    if data.attendees is not None:
        _apply_participants(event, data.attendees)
    ok = True
    if data.attachments is not None and hasattr(provider, "get_attachment"):
        ok = _apply_attachments(provider, event, data.attachments, result)
    if ok:
        link.extra_key = key
        SyncedEvent.objects.filter(pk=link.pk).update(extra_key=key)


def _invited_copy(cal, uid) -> bool:
    """Это копия встречи, на которую пользователь приглашён и которую уже импортировал организатор?"""
    return SyncedEvent.objects.filter(uid=uid, origin=SyncedEvent.Origin.REMOTE, event__invitees=cal.user).exclude(
        calendar=cal
    ).exists()


# ---------------------------------------------------------------- синхронизация


@dataclass
class SyncResult:
    created_local: int = 0
    updated_local: int = 0
    deleted_local: int = 0
    created_remote: int = 0
    updated_remote: int = 0
    deleted_remote: int = 0
    conflicts: int = 0
    errors: list = field(default_factory=list)

    def summary(self):
        parts = []
        if self.created_local or self.updated_local or self.deleted_local:
            parts.append(f"к нам: +{self.created_local} ~{self.updated_local} −{self.deleted_local}")
        if self.created_remote or self.updated_remote or self.deleted_remote:
            parts.append(f"туда: +{self.created_remote} ~{self.updated_remote} −{self.deleted_remote}")
        if self.conflicts:
            parts.append(f"конфликтов: {self.conflicts}")
        if self.errors:
            parts.append(f"ошибок: {len(self.errors)}")
        return "; ".join(parts) or "изменений нет"

    def to_dict(self):
        return {**{k: v for k, v in self.__dict__.items() if k != "errors"}, "errors": self.errors, "summary": self.summary()}


def _in_window(event: Event, start, end):
    return event.start < end and (event.end or event.start) >= start


def sync_calendar(cal: ExternalCalendar, now=None) -> SyncResult:
    """Синхронизирует один календарь. Не бросает ProviderError — ошибка записывается в cal.last_error."""
    result = SyncResult()
    try:
        # изменения, пришедшие из внешнего календаря, обратно мгновенно не отправляем (иначе — эхо)
        with suppress_push():
            _sync(cal, result, now)
        if cal.provider == ExternalCalendar.Provider.EXCHANGE and cal.can_export:
            # файлы и участникам из адресной книги — то, что не удалось отправить сразу
            from events.models import Attachment

            from .guests import deliver_pending
            from .push import push_attachments

            stuck = Attachment.objects.filter(event__external_links__calendar=cal).filter(
                Q(external_id="") | Q(external_id__startswith="pending:")
            )
            for event_id in stuck.values_list("event_id", flat=True).distinct():
                rep = push_attachments(event_id, include_stuck=True)
                result.errors.extend(rep["errors"])
            deliver_pending(cal.user)
        cal.last_error = "; ".join(result.errors[:3])
    except ProviderError as exc:
        cal.last_error = str(exc)
        result.errors.append(str(exc))
    except Exception as exc:  # noqa: BLE001 — не роняем пакетную синхронизацию из-за одного календаря
        log.exception("Ошибка синхронизации календаря %s", cal.pk)
        cal.last_error = f"Внутренняя ошибка: {exc.__class__.__name__}"
        result.errors.append(cal.last_error)
    cal.last_sync_at = timezone.now()
    cal.last_summary = result.summary()[:255]
    cal.save(update_fields=["last_sync_at", "last_error", "last_summary"])
    return result


def _sync(cal: ExternalCalendar, result: SyncResult, now=None):
    provider = build_provider(cal)
    w_start, w_end = sync_window(now)
    remote_items = provider.list_events(w_start, w_end)
    remote = {}
    for item in remote_items:
        remote.setdefault(item.data.uid, item)
    all_remote_uids = set(remote)

    links = list(cal.links.select_related("event"))
    linked_in_window = [l for l in links if l.event_id and _in_window(l.event, w_start, w_end)]
    # Защита: если сервер вдруг вернул пустой список, а у нас много связанных событий,
    # не удаляем их — скорее всего, это сбой или выбран не тот календарь.
    suspicious_empty = not remote_items and len(linked_in_window) >= 3

    def protect(fn, *args):
        try:
            return fn(*args)
        except AuthError:
            raise
        except ProviderError as exc:
            result.errors.append(str(exc))
            return None

    # 1) уже связанные события
    for link in links:
        item = remote.pop(link.uid, None)
        event = link.event

        if event is None:  # удалено у нас
            if item and cal.can_export and not link.read_only:
                try:
                    provider.delete_event(item.remote_id, item.etag)
                    result.deleted_remote += 1
                except ConflictError:
                    remote[link.uid] = item  # там успели изменить — вернём событие к нам как новое
                except ProviderError as exc:
                    result.errors.append(str(exc))
                    continue
            link.delete()
            continue

        if item is None:  # во внешнем календаре события нет
            if not _in_window(event, w_start, w_end) or suspicious_empty:
                continue
            if cal.can_import:
                with transaction.atomic():
                    link.delete()
                    event.delete()
                result.deleted_local += 1
            elif cal.can_export:  # только экспорт: восстанавливаем удалённое там
                created = protect(provider.create_event, event_to_data(event, link.uid))
                if created:
                    link.remote_id, link.etag = created.remote_id, created.etag
                    link.local_key = link.remote_key = _local_fingerprint(event, cal)
                    link.save()
                    result.created_remote += 1
            continue

        local_key = _local_fingerprint(event, cal)
        remote_key = _remote_fingerprint(item.data, cal)
        local_changed = local_key != link.local_key
        remote_changed = remote_key != link.remote_key

        winner = None
        if local_changed and remote_changed and local_key != remote_key:
            result.conflicts += 1
            remote_time = item.data.last_modified
            winner = "local" if remote_time and event.updated_at > remote_time else "remote"
        elif remote_changed:
            winner = "remote"
        elif local_changed:
            winner = "local"

        if winner == "local" and (link.read_only or not cal.can_export):
            # изменять это событие во внешнем календаре нельзя — возвращаем внешнюю версию
            winner = "remote" if cal.can_import and (link.read_only or remote_changed) else None

        if winner == "remote" and cal.can_import:
            apply_data(event, item.data)
            event.save()
            result.updated_local += 1
            link.local_key = link.remote_key = _local_fingerprint(event, cal)
            if cal.provider == ExternalCalendar.Provider.EXCHANGE:
                # изменено организатором в Outlook — участникам собрания обновление уже разослал сам Exchange
                GuestDelivery.objects.filter(event=event, channel=GuestDelivery.Channel.EXCHANGE).update(
                    content_key=_local_fingerprint(event)
                )
        elif winner == "local" and cal.can_export:
            try:
                link.etag = provider.update_event(item.remote_id, item.etag, event_to_data(event, link.uid))
                result.updated_remote += 1
                link.local_key = link.remote_key = local_key
            except ConflictError:
                result.errors.append(f"«{event.title}»: изменено одновременно в обоих календарях, повторите синхронизацию.")
                continue
            except AuthError:
                raise
            except ProviderError as exc:
                result.errors.append(str(exc))
                continue
        else:
            link.remote_key = remote_key
            if not local_changed:
                link.local_key = local_key
        if winner != "local":
            link.etag = item.etag
        link.remote_id = item.remote_id
        link.save()
        if getattr(item, "needs_rewrite", False) and winner != "local" and cal.can_export and not link.read_only:
            # Exchange: ссылки нет в тексте события (записано старой версией) — перезаписываем, никому не рассылая
            try:
                link.etag = provider.update_event(item.remote_id, link.etag, event_to_data(event, link.uid))
                SyncedEvent.objects.filter(pk=link.pk).update(etag=link.etag)
            except ProviderError as exc:
                result.errors.append(str(exc))
        try:
            _import_extras(cal, provider, link, event, item.data, result)
        except ProviderError as exc:
            result.errors.append(str(exc))

    # 2) новые события во внешнем календаре
    if cal.can_import:
        for uid, item in remote.items():
            own = own_event_from_uid(uid)
            if own is not None:
                if own.owner_id != cal.user_id:
                    # приглашение на событие нашего календаря: участник уже видит его у нас — не дублируем
                    continue
                if not own.external_links.filter(calendar=cal).exists():
                    # наше событие, связь с которым потерялась, — восстанавливаем связь вместо копии
                    SyncedEvent.objects.create(
                        calendar=cal, event=own, uid=uid, remote_id=item.remote_id, etag=item.etag,
                        local_key=_local_fingerprint(own, cal), remote_key=_remote_fingerprint(item.data, cal),
                        origin=SyncedEvent.Origin.LOCAL, read_only=item.data.read_only or cal.remote_read_only,
                    )
                continue
            if _invited_copy(cal, uid):
                continue  # пользователь уже видит эту встречу у нас как приглашённый — не дублируем
            with transaction.atomic():
                event = Event(owner=cal.user, created_by=cal.user, color=cal.color)
                apply_data(event, item.data)
                event.save()
                link = SyncedEvent.objects.create(
                    calendar=cal, event=event, uid=uid, remote_id=item.remote_id, etag=item.etag,
                    local_key=_local_fingerprint(event, cal), remote_key=_local_fingerprint(event, cal),
                    origin=SyncedEvent.Origin.REMOTE, read_only=item.data.read_only or cal.remote_read_only,
                )
            result.created_local += 1
            try:
                _import_extras(cal, provider, link, event, item.data, result)
            except ProviderError as exc:
                result.errors.append(str(exc))

    # 3) новые события у нас — только созданные в нашем календаре, а не импортированные из других
    if cal.can_export:
        candidates = (
            Event.objects.filter(owner=cal.user, start__lt=w_end)
            .filter(Q(end__gte=w_start) | Q(end__isnull=True, start__gte=w_start))
            .exclude(external_links__calendar=cal)
            .exclude(external_links__origin=SyncedEvent.Origin.REMOTE)
        )
        for event in candidates:
            uid = local_uid(event)
            if uid in all_remote_uids:
                continue  # уже есть там (связь потерялась) — не дублируем
            created = protect(provider.create_event, event_to_data(event, uid))
            if created:
                SyncedEvent.objects.create(
                    calendar=cal, event=event, uid=uid, remote_id=created.remote_id, etag=created.etag,
                    local_key=_local_fingerprint(event, cal), remote_key=_local_fingerprint(event, cal),
                    origin=SyncedEvent.Origin.LOCAL,
                )
                result.created_remote += 1


def sync_user(user) -> dict:
    return {cal.pk: sync_calendar(cal) for cal in user.external_calendars.filter(enabled=True)}
