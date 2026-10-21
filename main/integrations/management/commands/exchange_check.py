# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Диагностика: как событие календаря выглядит в Microsoft Exchange и почему что-то не дошло.

    python manage.py exchange_check 42          # по id события
    python manage.py exchange_check --title Тест  # по названию (последнее с таким названием)
    python manage.py exchange_check 42 --fix    # перезаписать событие в Exchange и дослать файлы
"""

import traceback

from django.core.management.base import BaseCommand, CommandError

from events.models import Event
from integrations import VERSION
from integrations.models import ExternalCalendar, SyncedEvent
from integrations.providers.exchange import compose_body
from integrations.push import event_lock, push_attachments, push_event
from integrations.sync import build_provider, event_to_data


class Command(BaseCommand):
    help = "Показывает, что календарь отправляет в Exchange и что там лежит; --fix — перезаписать и дослать файлы."

    def add_arguments(self, parser):
        parser.add_argument("event_id", nargs="?", type=int)
        parser.add_argument("--title", help="Название события (берётся последнее созданное)")
        parser.add_argument("--fix", action="store_true", help="Перезаписать событие в Exchange и дослать файлы")

    def out(self, label, value=""):
        self.stdout.write(f"{label:<28}{value}")

    def handle(self, *args, event_id=None, title=None, fix=False, **opts):
        if event_id:
            event = Event.objects.filter(pk=event_id).first()
        elif title:
            event = Event.objects.filter(title=title).order_by("-pk").first()
        else:
            raise CommandError("Укажите id события или --title.")
        if event is None:
            raise CommandError("Событие не найдено.")

        self.stdout.write(self.style.MIGRATE_HEADING(f"Версия интеграций: {VERSION}"))
        self.stdout.write(self.style.MIGRATE_HEADING(f"Событие #{event.pk} «{event.title}», владелец: {event.owner}"))
        self.out("Ссылка:", event.url or "—")
        self.out("Описание:", repr(event.description))
        for att in event.attachments.all():
            self.out("Файл:", f"{att.name} ({att.size} байт), в Exchange: {att.external_id or 'ещё не отправлен'}")
        self.out("Текст для Exchange:", repr(compose_body(event.description, event.url)))

        cals = ExternalCalendar.objects.filter(user=event.owner, provider=ExternalCalendar.Provider.EXCHANGE)
        if not cals:
            self.stdout.write(self.style.WARNING("У владельца не подключён Microsoft Exchange — событие туда не отправляется."))
            return
        for cal in cals:
            self.stdout.write(self.style.MIGRATE_HEADING(f"Календарь Exchange: {cal} (id {cal.pk})"))
            self.out("Включён / запись туда:", f"{cal.enabled} / {cal.can_export} (направление: {cal.get_direction_display()})")
            self.out("Последняя ошибка:", cal.last_error or "—")
            link = SyncedEvent.objects.filter(calendar=cal, event=event).first()
            if fix and cal.enabled and cal.can_export:
                self._fix(event, cal, link)
                link = SyncedEvent.objects.filter(calendar=cal, event=event).first()
            if link is None:
                self.stdout.write(self.style.WARNING("Событие с этим календарём не связано (в Exchange его нет). Запустите с --fix."))
                continue
            self.out("Связь:", f"{link.get_origin_display()}, только чтение: {link.read_only}")
            try:
                info = build_provider(cal).inspect_item(link.remote_id)
            except Exception as exc:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(f"Не удалось прочитать событие из Exchange: {exc}"))
                continue
            self.out("В Exchange — тема:", info["subject"])
            self.out("В Exchange — текст:", repr(info["body"]))
            self.out("В Exchange — отметка ссылки:", info["link_marker"] or "—")
            self.out("В Exchange — вложения:", ", ".join(info["attachments"]) or "—")
            self.out("В Exchange — участники:", ", ".join(info["attendees"]) or "—")
            expected = compose_body(event.description, event.url)
            if info["body"] != expected:
                self.stdout.write(self.style.WARNING("Текст в Exchange отличается от ожидаемого. Запустите с --fix."))
            missing = [a.name for a in event.attachments.all() if a.name not in info["attachments"]]
            if missing:
                self.stdout.write(self.style.WARNING(f"Нет в Exchange файлов: {', '.join(missing)}. Запустите с --fix."))
            if info["body"] == expected and not missing:
                self.stdout.write(self.style.SUCCESS("Событие в Exchange совпадает с календарём."))

    def _fix(self, event, cal, link):
        self.stdout.write("Исправление…")
        with event_lock(event.pk):
            try:
                if link is None:
                    rep = push_event(event.pk)
                    self.out("  отправка события:", rep)
                    link = SyncedEvent.objects.filter(calendar=cal, event=event).first()
                if link is not None and not link.read_only:
                    provider = build_provider(cal)
                    link.etag = provider.update_event(link.remote_id, link.etag, event_to_data(event, link.uid))
                    SyncedEvent.objects.filter(pk=link.pk).update(etag=link.etag)
                    self.out("  текст и ссылка:", "перезаписаны")
                    rep = push_attachments(event.pk, include_stuck=True)
                    self.out("  файлы отправлены:", ", ".join(rep["sent"]) or "—")
                    for err in rep["errors"]:
                        self.stdout.write(self.style.ERROR(f"  ошибка файла: {err}"))
            except Exception:  # noqa: BLE001
                self.stdout.write(self.style.ERROR(traceback.format_exc()))
