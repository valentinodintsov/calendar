# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.core.management.base import BaseCommand

from integrations.guests import deliver_pending
from integrations.models import ExternalCalendar
from integrations.sync import sync_calendar


class Command(BaseCommand):
    help = (
        "Синхронизирует подключённые внешние календари (Exchange, Google, Яндекс, Mail.ru, Rambler, CalDAV) "
        "и досылает участникам из адресной книги приглашения, которые не удалось отправить сразу. "
        "Запускайте по расписанию, например каждые 10 минут из cron."
    )

    def add_arguments(self, parser):
        parser.add_argument("--user", help="Только календари этого пользователя (логин)")
        parser.add_argument("--id", type=int, action="append", help="Только календарь с этим id (можно несколько)")

    def handle(self, *args, **opts):
        qs = ExternalCalendar.objects.filter(enabled=True).select_related("user")
        if opts.get("user"):
            qs = qs.filter(user__username=opts["user"])
        if opts.get("id"):
            qs = qs.filter(pk__in=opts["id"])
        failed = 0
        for cal in qs:
            res = sync_calendar(cal)
            line = f"[{cal.pk}] {cal.user} — {cal}: {res.summary()}"
            if res.errors:
                failed += 1
                self.stderr.write(self.style.ERROR(line + " — " + "; ".join(res.errors[:3])))
            else:
                self.stdout.write(self.style.SUCCESS(line))
        if not opts.get("id"):
            user = None
            if opts.get("user"):
                from django.contrib.auth import get_user_model

                user = get_user_model().objects.filter(username=opts["user"]).first()
            sent = deliver_pending(user) if (user or not opts.get("user")) else 0
            if sent:
                self.stdout.write(f"Участникам из адресной книги досланы приглашения по событиям: {sent}")
        if not qs.exists():
            self.stdout.write("Нет подключённых календарей.")
        elif failed:
            self.stderr.write(self.style.WARNING(f"Календарей с ошибками: {failed}"))
