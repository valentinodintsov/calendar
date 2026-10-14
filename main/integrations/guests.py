"""
Событие — в календарь участников, выбранных из адресной книги AD/LDAP.

Когда в событие добавляют участника из адресной книги (Event.directory_guests), событие автоматически
появляется в его календаре и дальше обновляется при каждом изменении:

  * у владельца подключён Microsoft Exchange — событие в Exchange становится собранием с этими участниками,
    и Exchange сам рассылает приглашения, обновления и отмены: встреча появляется в календарях участников
    (Outlook, OWA, телефон), как если бы её создали в Outlook;
  * Exchange не подключён — каждому участнику уходит письмо с приглашением iTIP (.ics, METHOD:REQUEST);
    при изменении — обновление с тем же UID, при удалении участника или события — отмена (METHOD:CANCEL).
    Exchange/Outlook и большинство почтовых сервисов ставят такое событие в календарь сами.

Участник, у которого есть учётная запись в нашем календаре, к тому же видит событие у нас (права на чтение
по совпадению e-mail, см. models_employee.permissions).

Что уже отправлено, хранится в GuestDelivery: отправляем только новым участникам, отмену — удалённым,
обновление — всем, если изменилось содержимое события (название, время, место, описание, ссылка).
"""

from __future__ import annotations

import hashlib
import logging
import re

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db.models import Q
from django.utils import timezone

from events.models import Event, user_display_name

from .models import ExternalCalendar, GuestDelivery, SyncedEvent

log = logging.getLogger(__name__)

EXCHANGE = ExternalCalendar.Provider.EXCHANGE
WRITABLE = (ExternalCalendar.Direction.BOTH, ExternalCalendar.Direction.EXPORT)


def _owner_exchange_calendars(owner_id):
    return ExternalCalendar.objects.filter(
        user_id=owner_id, provider=EXCHANGE, enabled=True, direction__in=WRITABLE, remote_read_only=False
    )


def exchange_link(event):
    """Связь события с календарём Exchange владельца, куда разрешена запись (если событие уже там)."""
    return (
        SyncedEvent.objects.filter(event=event, read_only=False, calendar__in=_owner_exchange_calendars(event.owner_id))
        .select_related("calendar")
        .order_by("pk")
        .first()
    )


def guest_emails(event) -> list[str]:
    owner_email = (event.owner.email or "").strip().lower() if event.owner_id else ""
    return [e for e in event.directory_guest_list() if e != owner_email]


def _names(emails):
    """Имена участников, у которых есть учётная запись в календаре."""
    if not emails:
        return {}
    q = Q()
    for e in emails:
        q |= Q(email__iexact=e)
    return {u.email.lower(): user_display_name(u) for u in get_user_model().objects.filter(q, is_active=True)}


def _plan(guests, rows, channel, key):
    mine = {e: r for e, r in rows.items() if r.channel == channel}
    added = [e for e in guests if e not in mine]
    removed = [e for e in mine if e not in guests]
    changed = [e for e in guests if e in mine and mine[e].content_key != key]
    return added, removed, changed


def _email_key(event, key: str) -> str:
    """
    Отпечаток события для писем: с файлами (они вложены в приглашение) и напоминанием (оно записано в .ics) —
    изменилось что-то из этого, нужно обновление.
    """
    files = sorted(event.attachments.values_list("pk", "name", "size"))
    reminder = event.reminder_minutes
    if not files and reminder is None:
        return key
    extra = f"{files!r}" if reminder is None else f"{files!r}:reminder={reminder}"
    return hashlib.sha1(f"{key}:{extra}".encode("utf-8")).hexdigest()


def _record(event, emails, key, channel):
    for email in emails:
        GuestDelivery.objects.update_or_create(event=event, email=email, defaults={"channel": channel, "content_key": key})


def _note_error(cal, title, exc):
    from .push import _note_error as note

    note(cal, title, exc)


# ---------------------------------------------------------------- доставка


def deliver_guests(event_id: int) -> dict:
    """
    Приводит календари участников из адресной книги в соответствие с событием. Можно вызывать повторно.
    Отчёт: channel, invited/updated/cancelled — кому что ушло, failed — {адрес: причина}, errors — общие ошибки.
    """
    from .sync import _local_fingerprint

    report = {"channel": "", "invited": [], "updated": [], "cancelled": [], "failed": {}, "errors": [], "notes": []}
    event = Event.objects.select_related("owner").filter(pk=event_id).first()
    if event is None or not event.owner_id:
        return report
    guests = guest_emails(event)
    rows = {r.email: r for r in GuestDelivery.objects.filter(event=event)}
    if not guests and not rows:
        return report
    key = _local_fingerprint(event)

    if _owner_exchange_calendars(event.owner_id).exists():
        report["channel"] = GuestDelivery.Channel.EXCHANGE
        link = exchange_link(event)
        ok = False
        if link is None:
            report["errors"].append("событие не удалось записать в Exchange")
        else:
            ok = _via_exchange(event, link, guests, rows, key, report)
        if not ok:
            # Exchange недоступен — новым участникам приглашение письмом, чтобы они узнали о встрече сразу;
            # в собрание Exchange они попадут при следующей синхронизации
            report["notes"].append("Exchange недоступен — приглашения отправлены письмом")
            exchange_rows = {e for e, r in rows.items() if r.channel == GuestDelivery.Channel.EXCHANGE}
            email_rows = {e: r for e, r in rows.items() if r.channel == GuestDelivery.Channel.EMAIL}
            _via_email(event, [g for g in guests if g not in exchange_rows], email_rows, key, report, cleanup=False)
    else:
        report["channel"] = GuestDelivery.Channel.EMAIL
        _via_email(event, guests, rows, key, report)
    return report


def _via_exchange(event, link, guests, rows, key, report) -> bool:
    """Собрание Exchange. False — Exchange недоступен (нужен запасной способ)."""
    from .providers import ConflictError, ProviderError
    from .sync import build_provider, event_to_data

    added, removed, changed = _plan(guests, rows, GuestDelivery.Channel.EXCHANGE, key)
    email_rows = {e: r for e, r in rows.items() if r.channel == GuestDelivery.Channel.EMAIL}
    # уже получившие приглашение письмом (когда Exchange был недоступен) — повторно не приглашаем
    really_added = [e for e in added if e not in email_rows]
    email_removed = [e for e in email_rows if e not in guests]
    if not (added or removed or changed or email_removed):
        return True
    if added or removed or changed:
        data = event_to_data(event, link.uid)
        data.attendees = guests
        # изменилось само событие — обновление всем; изменился состав — приглашения новым и отмены удалённым
        send = "all" if changed else ("changed" if really_added or removed else "none")
        try:
            etag = build_provider(link.calendar).update_event(link.remote_id, link.etag, data, send=send)
        except ConflictError:
            report["errors"].append("событие одновременно изменили в Exchange — повторим при синхронизации")
            return True
        except ProviderError as exc:
            report["errors"].append(str(exc))
            _note_error(link.calendar, event.title, exc)
            return False
        SyncedEvent.objects.filter(pk=link.pk).update(etag=etag)
        GuestDelivery.objects.filter(event=event, email__in=removed).delete()
        _record(event, guests, key, GuestDelivery.Channel.EXCHANGE)
        report["invited"] += really_added
        report["updated"] += changed
        report["cancelled"] += removed
    if email_removed:
        _via_email(event, [], {e: email_rows[e] for e in email_removed}, key, report, cleanup=False)
    return True


def _via_email(event, guests, rows, key, report, cleanup=True):
    from .invitations import build_messages
    from .mailer import send

    key = _email_key(event, key)
    added, removed, changed = _plan(guests, rows, GuestDelivery.Channel.EMAIL, key)
    if cleanup:
        # записи другого способа (Exchange отключили) — участник получит письмо как новый
        GuestDelivery.objects.filter(event=event).exclude(channel=GuestDelivery.Channel.EMAIL).exclude(
            email__in=guests
        ).delete()
    if not (added or removed or changed):
        return
    names = _names(guests + removed)
    attendees = [(names.get(e, ""), e) for e in guests]

    def deliver(emails, method, who):
        if not emails:
            return
        sent, failed = send(build_messages(event, emails, method, attendees=attendees))
        for email in sent:
            if method == "CANCEL":
                GuestDelivery.objects.filter(event=event, email=email).delete()
            else:
                _record(event, [email], key, GuestDelivery.Channel.EMAIL)
            report[who].append(email)
        report["failed"].update(failed)  # не отмечаем — повторим при следующем изменении или синхронизации

    deliver(added, "REQUEST", "invited")
    deliver(changed, "REQUEST", "updated")
    deliver(removed, "CANCEL", "cancelled")


def deliver_pending(user=None, now=None) -> int:
    """
    Досылает то, что не удалось отправить сразу (сервер был недоступен), для текущих и будущих событий.
    Вызывается из плановой синхронизации. Возвращает число событий, по которым что-то отправлено.
    """
    now = now or timezone.now()
    qs = Event.objects.filter(Q(end__gte=now) | Q(end__isnull=True, start__gte=now)).filter(
        Q(directory_guests__gt="") | Q(guest_deliveries__isnull=False)
    )
    if user is not None:
        qs = qs.filter(owner=user)
    done = 0
    for pk in qs.values_list("pk", flat=True).distinct():
        try:
            rep = deliver_guests(pk)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка доставки события %s участникам", pk)
            continue
        if rep["invited"] or rep["updated"] or rep["cancelled"]:
            done += 1
    return done


def cancel_messages_for_deleted(event) -> list:
    """Перед удалением события: письма-отмены участникам, получившим приглашение письмом."""
    from .invitations import build_messages

    emails = list(
        GuestDelivery.objects.filter(event=event, channel=GuestDelivery.Channel.EMAIL).values_list("email", flat=True)
    )
    if not emails:
        return []
    names = _names(emails)
    return build_messages(event, emails, "CANCEL", attendees=[(names.get(e, ""), e) for e in emails])


def had_exchange_meeting(event) -> bool:
    return GuestDelivery.objects.filter(event=event, channel=GuestDelivery.Channel.EXCHANGE).exists()


# ---------------------------------------------------------------- наши события в чужих календарях


def own_event_from_uid(uid: str):
    """Событие нашего календаря по UID вида calendar-event-<id>@<домен> (встреча, разосланная нами)."""
    domain = getattr(settings, "INTEGRATIONS_UID_DOMAIN", "") or "calendar.local"
    m = re.match(rf"^calendar-event-(\d+)@{re.escape(domain)}$", uid or "")
    if not m:
        return None
    return Event.objects.filter(pk=int(m.group(1))).first()
