"""
Мгновенная отправка событий во внешние календари (Exchange, Google, CalDAV) при их создании, изменении и удалении,
а также в календари участников, выбранных из адресной книги AD/LDAP (см. guests.py).

Срабатывает на сохранение/удаление события любым способом (API, перетаскивание, админка) — после фиксации
транзакции и в фоновом потоке, чтобы пользователь не ждал ответа внешнего сервера. Плановая синхронизация
(`sync_calendars`) остаётся страховкой: она доставит то, что не удалось отправить сразу, и заберёт изменения,
сделанные во внешнем календаре (например, в Outlook).
"""

from __future__ import annotations

import logging
import threading
from contextlib import contextmanager

from django.conf import settings
from django.db import IntegrityError, close_old_connections, connection, transaction
from django.db.models import Q
from django.db.models.signals import post_delete, post_save, pre_delete
from django.dispatch import receiver

from events.models import Attachment, Event

log = logging.getLogger(__name__)
_state = threading.local()
_event_locks: dict = {}
_event_locks_guard = threading.Lock()


def event_lock(event_id):
    """Одно событие отправляется в Exchange одним потоком за раз (иначе сохранение и загрузка файла могут создать две копии)."""
    with _event_locks_guard:
        if len(_event_locks) > 1000:
            _event_locks.clear()
        return _event_locks.setdefault(event_id, threading.RLock())


@contextmanager
def suppress_push():
    """Внутри блока изменения событий не отправляются (синхронизация сама пишет события, импортируя их)."""
    prev = getattr(_state, "off", False)
    _state.off = True
    try:
        yield
    finally:
        _state.off = prev


def push_enabled() -> bool:
    return getattr(settings, "INTEGRATIONS_PUSH_ON_SAVE", True) and not getattr(_state, "off", False)


def _run(fn, *args):
    if not getattr(settings, "INTEGRATIONS_PUSH_ASYNC", True):
        fn(*args)
        return

    def target():
        close_old_connections()
        try:
            fn(*args)
        except Exception:  # noqa: BLE001
            log.exception("Ошибка мгновенной отправки во внешний календарь")
        finally:
            connection.close()  # у потока своё соединение с базой — закрываем

    threading.Thread(target=target, name="calendar-push", daemon=True).start()


def _note_error(cal, title, exc):
    from .models import ExternalCalendar

    msg = f"Не удалось сразу отправить «{title}»: {exc}. Повторим при следующей синхронизации."
    ExternalCalendar.objects.filter(pk=cal.pk).update(last_error=msg[:2000])


# ---------------------------------------------------------------- отправка


def push_event(event_id: int) -> dict:
    """Создаёт или обновляет событие во всех подключённых календарях владельца (где разрешён экспорт)."""
    from .models import ExternalCalendar, SyncedEvent
    from .providers import AuthError, ConflictError, ProviderError
    from .sync import _local_fingerprint, build_provider, event_to_data, local_uid

    report = {"created": 0, "updated": 0, "skipped": 0, "errors": []}
    event = Event.objects.filter(pk=event_id).select_related("owner").first()
    if event is None or not event.owner_id:
        return report
    # событие, импортированное из внешнего календаря, в другие внешние календари не копируем
    imported = SyncedEvent.objects.filter(event=event, origin=SyncedEvent.Origin.REMOTE).exists()
    for cal in ExternalCalendar.objects.filter(user_id=event.owner_id, enabled=True):
        if not cal.can_export:
            continue
        local_key = _local_fingerprint(event, cal)
        link = SyncedEvent.objects.filter(calendar=cal, event=event).first()
        if link is None and imported:
            continue
        if link is not None and (link.read_only or link.local_key == local_key):
            report["skipped"] += 1  # только для чтения или содержимое не менялось (например, сменили цвет)
            continue
        try:
            provider = build_provider(cal)
            if link is not None:
                link.etag = provider.update_event(link.remote_id, link.etag, event_to_data(event, link.uid))
                link.local_key = link.remote_key = local_key
                link.save(update_fields=["etag", "local_key", "remote_key", "synced_at"])
                report["updated"] += 1
            else:
                uid = local_uid(event)
                created = provider.create_event(event_to_data(event, uid))
                try:
                    with transaction.atomic():
                        SyncedEvent.objects.create(
                            calendar=cal, event=event, uid=uid, remote_id=created.remote_id, etag=created.etag,
                            local_key=local_key, remote_key=local_key, origin=SyncedEvent.Origin.LOCAL,
                        )
                except IntegrityError:
                    pass  # одновременно успела плановая синхронизация — связь уже есть
                report["created"] += 1
        except ConflictError:
            # во внешнем календаре событие тоже успели изменить — разберётся плановая синхронизация
            report["errors"].append(f"{cal}: изменено одновременно в обоих календарях")
        except (AuthError, ProviderError) as exc:
            report["errors"].append(f"{cal}: {exc}")
            _note_error(cal, event.title, exc)
    return report


def push_and_deliver(event_id: int):
    """Сначала — в календари владельца (с файлами), затем — в календари участников из адресной книги."""
    from .guests import deliver_guests

    with event_lock(event_id):
        push_event(event_id)
        push_attachments(event_id)
        deliver_guests(event_id)


def push_only(event_id: int):
    """Только в календари владельца: приглашения участникам уйдут позже, после загрузки файлов (files_changed)."""
    with event_lock(event_id):
        push_event(event_id)
        push_attachments(event_id)


def push_files(event_id: int):
    """Файл прикрепили (часто сразу после создания события): дожидаемся, пока событие появится в Exchange, и отправляем файл."""
    with event_lock(event_id):
        if _exchange_link(event_id) is None:
            push_event(event_id)  # событие ещё не в Exchange (например, отправка шла в другом потоке и упала)
        push_attachments(event_id)


# ---------------------------------------------------------------- файлы событий → Exchange

PENDING = "pending:"  # файл сейчас отправляется (защита от двойной отправки из разных потоков)


def _exchange_link(event_id):
    from .models import ExternalCalendar, SyncedEvent

    for link in SyncedEvent.objects.filter(
        event_id=event_id, read_only=False, calendar__provider=ExternalCalendar.Provider.EXCHANGE, calendar__enabled=True
    ).select_related("calendar").order_by("pk"):
        if link.calendar.can_export:
            return link
    return None


def _notify_meeting(event, link, provider):
    """Событие — собрание с участниками из адресной книги: разослать им обновление (с новыми файлами)."""
    from .guests import guest_emails, had_exchange_meeting
    from .models import SyncedEvent
    from .sync import event_to_data

    if not had_exchange_meeting(event):
        return
    data = event_to_data(event, link.uid)
    data.attendees = guest_emails(event)
    link.etag = provider.update_event(link.remote_id, link.etag, data, send="all")
    SyncedEvent.objects.filter(pk=link.pk).update(etag=link.etag)


def push_attachments(event_id: int, include_stuck: bool = False) -> dict:
    """
    Прикрепляет к событию в Exchange файлы, которые ещё не отправлены (у них пустой external_id).
    include_stuck — также файлы, отправка которых оборвалась (плановая синхронизация).
    """
    import mimetypes
    import uuid

    from events.models import Attachment

    from .models import SyncedEvent
    from .providers import ProviderError
    from .sync import build_provider

    report = {"sent": [], "errors": []}
    link = _exchange_link(event_id)
    if link is None:
        return report
    qs = Attachment.objects.filter(event_id=event_id)
    qs = qs.filter(Q(external_id="") | Q(external_id__startswith=PENDING)) if include_stuck else qs.filter(external_id="")
    pending = list(qs)
    if not pending:
        return report
    event = Event.objects.select_related("owner").get(pk=event_id)
    provider = build_provider(link.calendar)
    for att in pending:
        token = f"{PENDING}{uuid.uuid4().hex}"
        if not Attachment.objects.filter(pk=att.pk, external_id=att.external_id).update(external_id=token):
            continue  # этот файл уже отправляет другой поток
        try:
            with att.file.open("rb") as fh:
                content = fh.read()
            ctype = mimetypes.guess_type(att.name)[0] or "application/octet-stream"
            aid, change_key = provider.add_attachment(link.remote_id, att.name, content, ctype)
        except (ProviderError, OSError) as exc:
            Attachment.objects.filter(pk=att.pk, external_id=token).update(external_id="")
            report["errors"].append(f"{att.name}: {exc}")
            _note_error(link.calendar, f"{event.title}: файл {att.name}", exc)
            continue
        Attachment.objects.filter(pk=att.pk).update(external_id=aid)
        if change_key:
            link.etag = change_key
        report["sent"].append(att.name)
    if report["sent"]:
        SyncedEvent.objects.filter(pk=link.pk).update(etag=link.etag)
        try:
            _notify_meeting(event, link, provider)
        except ProviderError as exc:
            report["errors"].append(str(exc))
    return report


def push_attachment_delete(event_id: int, external_id: str) -> None:
    """Файл удалили у нас — удаляем его и из события в Exchange."""
    from .models import SyncedEvent
    from .providers import ProviderError
    from .sync import build_provider

    event = Event.objects.filter(pk=event_id).first()
    link = _exchange_link(event_id) if event else None
    if link is None:
        return  # событие удалено целиком (вместе с файлами) или не связано с Exchange
    provider = build_provider(link.calendar)
    try:
        change_key = provider.delete_attachment(external_id)
        if change_key:
            link.etag = change_key
            SyncedEvent.objects.filter(pk=link.pk).update(etag=change_key)
        _notify_meeting(event, link, provider)
    except ProviderError as exc:
        _note_error(link.calendar, f"{event.title}: удаление файла", exc)


def push_deletes(link_ids: list[int], notify_exchange: bool = False) -> dict:
    """
    Удаляет из внешних календарей события, удалённые у нас (по сохранённым связям).
    notify_exchange — событие было собранием Exchange с участниками: Exchange разошлёт им отмену.
    """
    from .models import SyncedEvent
    from .providers import ConflictError, ProviderError
    from .sync import build_provider

    report = {"deleted": 0, "errors": []}
    for link in SyncedEvent.objects.filter(pk__in=link_ids, event__isnull=True).select_related("calendar"):
        cal = link.calendar
        if not (cal.enabled and cal.can_export) or link.read_only:
            continue  # такие случаи решает плановая синхронизация (например, вернёт событие из календаря «только импорт»)
        try:
            if notify_exchange and cal.provider == cal.Provider.EXCHANGE:
                build_provider(cal).delete_event(link.remote_id, link.etag, notify=True)
            else:
                build_provider(cal).delete_event(link.remote_id, link.etag)
        except ConflictError:
            continue  # там его успели изменить — плановая синхронизация вернёт событие к нам
        except ProviderError as exc:
            report["errors"].append(f"{cal}: {exc}")
            _note_error(cal, "удаление", exc)
            continue
        link.delete()
        report["deleted"] += 1
    return report


# ---------------------------------------------------------------- сигналы


@receiver(post_save, sender=Event, dispatch_uid="integrations_push_on_save")
def _on_event_saved(sender, instance, raw=False, **kwargs):
    if raw or not instance.owner_id or not push_enabled():
        return
    pk = instance.pk
    if getattr(instance, "files_pending", False):
        # следом загрузят или удалят файлы — приглашения уйдут после этого, уже с файлами (см. files_changed)
        transaction.on_commit(lambda: _run(push_only, pk))
    elif _has_directory_guests(instance):
        # есть участники из адресной книги — доставляем сразу, в том же запросе,
        # чтобы пользователь увидел, ушли ли приглашения (см. pop_delivery_report)
        transaction.on_commit(lambda: _deliver_now(pk))
    else:
        transaction.on_commit(lambda: _run(push_and_deliver, pk))


def _has_directory_guests(instance) -> bool:
    from .models import GuestDelivery

    return bool(instance.directory_guests) or GuestDelivery.objects.filter(event_id=instance.pk).exists()


def _deliver_now(pk):
    from .guests import deliver_guests

    try:
        with event_lock(pk):
            push_event(pk)
            push_attachments(pk)
    except Exception:  # noqa: BLE001
        log.exception("Ошибка отправки события %s во внешние календари", pk)
    try:
        report = deliver_guests(pk)
    except Exception as exc:  # noqa: BLE001
        log.exception("Ошибка доставки события %s участникам", pk)
        report = {"channel": "", "invited": [], "updated": [], "cancelled": [], "failed": {},
                  "errors": [f"внутренняя ошибка: {exc.__class__.__name__}"], "notes": []}
    reports = getattr(_state, "reports", None)
    if reports is None or len(reports) > 50:  # отчёты, которые никто не забрал (сохранение из админки), не копим
        reports = _state.reports = {}
    reports[pk] = report


def files_changed(pk):
    """
    Файлы события загружены или удалены (events.views): отправляем приглашения, отложенные до загрузки файлов,
    а уже приглашённым письмом — обновление с новым набором файлов. Возвращает отчёт о доставке или None.
    """
    event = Event.objects.filter(pk=pk, owner__isnull=False).first()
    if event is None or not push_enabled() or not _has_directory_guests(event):
        return None
    _deliver_now(pk)
    return pop_delivery_report(pk)


def pop_delivery_report(pk):
    """Итог доставки участникам из адресной книги для события, сохранённого в этом запросе (или None)."""
    reports = getattr(_state, "reports", None) or {}
    return reports.pop(pk, None)


@receiver(pre_delete, sender=Event, dispatch_uid="integrations_push_pre_delete")
def _on_event_deleting(sender, instance, **kwargs):
    if not push_enabled():
        return
    from .guests import cancel_messages_for_deleted, had_exchange_meeting
    from .models import SyncedEvent

    instance._push_link_ids = list(SyncedEvent.objects.filter(event=instance).values_list("pk", flat=True))
    instance._push_notify = had_exchange_meeting(instance)
    try:
        # пока событие ещё есть — готовим отмены участникам, которым приглашение ушло письмом
        instance._guest_cancels = cancel_messages_for_deleted(instance)
    except Exception:  # noqa: BLE001
        log.exception("Не удалось подготовить отмены для события %s", instance.pk)
        instance._guest_cancels = []


def _send_cancels(messages):
    from .invitations import send_messages

    _, failed = send_messages(messages)
    if failed:
        log.warning("Не удалось отправить отмену события: %s", failed)


@receiver(post_delete, sender=Event, dispatch_uid="integrations_push_post_delete")
def _on_event_deleted(sender, instance, **kwargs):
    ids = getattr(instance, "_push_link_ids", None)
    notify = getattr(instance, "_push_notify", False)
    if ids:
        transaction.on_commit(lambda: _run(push_deletes, ids, notify))
    cancels = getattr(instance, "_guest_cancels", None)
    if cancels:
        transaction.on_commit(lambda: _run(_send_cancels, cancels))


@receiver(post_save, sender=Attachment, dispatch_uid="integrations_push_attachment_saved")
def _on_attachment_saved(sender, instance, created=False, raw=False, **kwargs):
    if raw or not created or instance.external_id or not push_enabled():
        return  # файлы, пришедшие из Exchange (с external_id), обратно не отправляем
    event_id = instance.event_id
    transaction.on_commit(lambda: _run(push_files, event_id))


@receiver(post_delete, sender=Attachment, dispatch_uid="integrations_push_attachment_deleted")
def _on_attachment_deleted(sender, instance, **kwargs):
    external_id = instance.external_id
    if not external_id or external_id.startswith(PENDING) or not push_enabled():
        return
    event_id = instance.event_id
    transaction.on_commit(lambda: _run(push_attachment_delete, event_id, external_id))
