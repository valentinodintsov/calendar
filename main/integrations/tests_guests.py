# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Участники из адресной книги AD/LDAP: событие автоматически попадает в их календарь —
через собрание Microsoft Exchange или письмом с приглашением .ics — и обновляется при изменениях.
"""

import json
import tempfile
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Attachment, Event

from .ics import unfold
from .models import ExternalCalendar, GuestDelivery, MailServer
from .push import pop_delivery_report
from .sync import local_uid, sync_calendar
from .testing_ews import FakeEWS
from .testing_smtp import FakeSMTP

TEMP_MEDIA = tempfile.mkdtemp(prefix="calendar-test-media-")


def ews_time(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Base(TestCase):
    def setUp(self):
        User = get_user_model()
        self.user = User.objects.create_user("ivan", "ivan@yanao.ru", "x", first_name="Иван", last_name="Иванов")
        self.petrov = User.objects.create_user("petrov", "Petrov@yanao.ru", "x", first_name="Пётр", last_name="Петров")
        self.client.force_login(self.user)
        self.start = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)

    def payload(self, **extra):
        local = timezone.localtime(self.start)
        return {"title": "Совещание", "start": local.strftime("%Y-%m-%dT%H:%M"),
                "end": (local + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), **extra}

    def create(self, **extra):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse("events:events-collection"), json.dumps(self.payload(**extra)),
                                    content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def patch(self, ev_id, **data):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.patch(reverse("events:event-detail", args=[ev_id]), json.dumps(data),
                                     content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()

    def delete(self, ev_id):
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.delete(reverse("events:event-detail", args=[ev_id]))
        self.assertEqual(resp.status_code, 200, resp.content)


@override_settings(INTEGRATIONS_PUSH_ASYNC=False)
class ExchangeMeetingTests(Base):
    def setUp(self):
        super().setUp()
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.cal = ExternalCalendar(user=self.user, provider=ExternalCalendar.Provider.EXCHANGE, server_url=self.srv.url,
                                    username=self.srv.username, calendar_id="calendar")
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()

    def item(self):
        self.assertEqual(len(self.srv.items), 1)
        return next(iter(self.srv.items.values()))

    def test_directory_guest_gets_meeting_in_own_calendar(self):
        ev = self.create(guest_emails="Sidorova@yanao.ru, friend@mail.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertEqual(ev["directory_guests"], ["sidorova@yanao.ru"])
        self.assertEqual(self.item()["Attendees"], ["sidorova@yanao.ru"])  # только из адресной книги
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToChangedAndSaveCopy")  # приглашение новому участнику
        row = GuestDelivery.objects.get(event_id=ev["id"])
        self.assertEqual((row.email, row.channel), ("sidorova@yanao.ru", "exchange"))
        self.assertEqual(len(mail.outbox), 0)  # писем не шлём — рассылает Exchange

    def test_manually_typed_guest_is_not_synced(self):
        ev = self.create(guest_emails="sidorova@yanao.ru")
        self.assertEqual(ev["directory_guests"], [])
        self.assertEqual(self.item()["Attendees"], [])
        self.assertFalse(GuestDelivery.objects.exists())

    def test_change_move_remove_and_delete(self):
        ev = self.create(guest_emails="sidorova@yanao.ru, kozlov@yanao.ru",
                         directory_guests=["sidorova@yanao.ru", "kozlov@yanao.ru"])
        # перенос — обновление всем участникам; пометка «из адресной книги» сохраняется при PATCH
        local = timezone.localtime(self.start + timedelta(days=1))
        self.patch(ev["id"], start=local.strftime("%Y-%m-%dT%H:%M"),
                   end=(local + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(self.item()["Start"], ews_time(self.start + timedelta(days=1)))
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToAllAndSaveCopy")
        self.assertEqual(sorted(self.item()["Attendees"]), ["kozlov@yanao.ru", "sidorova@yanao.ru"])
        # смена цвета — никому ничего не отправляется
        n = len(self.srv.sent)
        self.patch(ev["id"], color="red")
        self.assertEqual(len(self.srv.sent), n)
        # участника убрали — ему отмена (Exchange), остальным ничего
        self.patch(ev["id"], guest_emails="sidorova@yanao.ru")
        self.assertEqual(self.item()["Attendees"], ["sidorova@yanao.ru"])
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToChangedAndSaveCopy")
        self.assertEqual(list(GuestDelivery.objects.values_list("email", flat=True)), ["sidorova@yanao.ru"])
        # событие удалили — отмена всем участникам
        self.delete(ev["id"])
        self.assertEqual(self.srv.items, {})
        self.assertEqual(self.srv.sent[-1], {"op": "delete", "mode": "SendToAllAndSaveCopy", "attendees": [],
                                             "before": ["sidorova@yanao.ru"]})

    def test_calendar_user_picked_from_directory(self):
        ev = self.create(invitees=[self.petrov.pk], directory_guests=["petrov@yanao.ru"])
        self.assertEqual(self.item()["Attendees"], ["petrov@yanao.ru"])
        self.assertEqual(ev["directory_guests"], ["petrov@yanao.ru"])

    def test_directory_guest_with_account_sees_event(self):
        ev = self.create(guest_emails="petrov@yanao.ru", directory_guests=["petrov@yanao.ru"])
        self.client.force_login(self.petrov)
        day = timezone.localtime(self.start).date()
        found = self.client.get(reverse("events:events-collection"), {"start": day, "end": day + timedelta(days=1)}).json()
        self.assertEqual([e["id"] for e in found["events"]], [ev["id"]])
        detail = self.client.get(reverse("events:event-detail", args=[ev["id"]])).json()
        self.assertEqual(detail["permissions"], {"read": True, "change": False})
        self.assertEqual(detail["title"], "Совещание")

    def test_invited_users_own_exchange_does_not_duplicate(self):
        ev = self.create(guest_emails="petrov@yanao.ru", directory_guests=["petrov@yanao.ru"])
        # у Петрова свой Exchange: туда Exchange положил нашу встречу с тем же UID
        with FakeEWS() as srv2:
            event = Event.objects.get(pk=ev["id"])
            srv2.add(Subject="Совещание", Start=ews_time(self.start), End=ews_time(self.start + timedelta(hours=1)),
                     UID=local_uid(event))
            cal2 = ExternalCalendar(user=self.petrov, provider=ExternalCalendar.Provider.EXCHANGE, server_url=srv2.url,
                                    username=srv2.username, calendar_id="calendar")
            cal2.credentials = {"password": srv2.password}
            cal2.save()
            res = sync_calendar(cal2)
        self.assertEqual(res.created_local, 0, res.errors)
        self.assertEqual(Event.objects.count(), 1)

    def test_exchange_down_falls_back_to_email(self):
        self.cal.credentials = {"password": "wrong"}
        self.cal.save()
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertEqual(self.srv.items, {})
        # Exchange недоступен — участник сразу получает приглашение письмом
        self.assertEqual([m.to for m in mail.outbox], [["sidorova@yanao.ru"]])
        report = pop_delivery_report(ev["id"])
        self.assertEqual(report["invited"], ["sidorova@yanao.ru"])
        self.assertTrue(report["notes"])
        self.assertEqual(GuestDelivery.objects.get(event_id=ev["id"]).channel, "email")
        # Exchange снова доступен: синхронизация создаёт встречу и добавляет участника без повторного приглашения
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()
        res = sync_calendar(self.cal)
        self.assertEqual(res.created_remote, 1, res.errors)
        self.assertEqual(self.item()["Attendees"], ["sidorova@yanao.ru"])
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToNone")
        self.assertEqual(GuestDelivery.objects.get(event_id=ev["id"]).channel, "exchange")
        self.assertEqual(len(mail.outbox), 1)

    def test_report_for_exchange(self):
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        report = pop_delivery_report(ev["id"])
        self.assertEqual((report["channel"], report["invited"], report["failed"]), ("exchange", ["sidorova@yanao.ru"], {}))

    def test_change_made_in_outlook_is_not_resent(self):
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        iid = next(iter(self.srv.items))
        self.srv.items[iid]["Subject"] = "Совещание (Outlook)"
        self.srv._bump(self.srv.items[iid])
        n = len(self.srv.sent)
        res = sync_calendar(self.cal)
        self.assertEqual(res.updated_local, 1, res.errors)
        self.assertEqual(Event.objects.get(pk=ev["id"]).title, "Совещание (Outlook)")
        self.assertEqual(len(self.srv.sent), n)  # обновление участникам разослал сам Outlook


@override_settings(INTEGRATIONS_PUSH_ASYNC=False)
class EmailInvitationTests(Base):
    """Exchange не подключён — приглашения в календарь участников уходят письмом с .ics."""

    def ics(self, msg):
        return "\n".join(unfold(msg.attachments[0][1]))

    def test_invite_update_cancel(self):
        ev = self.create(guest_emails="sidorova@yanao.ru, friend@mail.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertEqual([m.to for m in mail.outbox], [["sidorova@yanao.ru"]])
        first = self.ics(mail.outbox[0])
        self.assertIn("METHOD:REQUEST", first)
        self.assertIn(f"UID:calendar-event-{ev['id']}@", first)
        self.assertIn("mailto:sidorova@yanao.ru", first)
        self.assertNotIn("friend@mail.ru", first)

        # без изменений содержимого — повторно не пишем
        self.patch(ev["id"], color="red")
        self.assertEqual(len(mail.outbox), 1)

        # изменили время — обновление
        local = timezone.localtime(self.start + timedelta(hours=3))
        self.patch(ev["id"], start=local.strftime("%Y-%m-%dT%H:%M"),
                   end=(local + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"))
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("METHOD:REQUEST", self.ics(mail.outbox[1]))

        # добавили ещё участника — письмо только ему
        self.patch(ev["id"], guest_emails="sidorova@yanao.ru, kozlov@yanao.ru",
                   directory_guests=["sidorova@yanao.ru", "kozlov@yanao.ru"])
        self.assertEqual(mail.outbox[-1].to, ["kozlov@yanao.ru"])
        self.assertEqual(len(mail.outbox), 3)

        # убрали участника — ему отмена
        self.patch(ev["id"], guest_emails="kozlov@yanao.ru")
        self.assertEqual(mail.outbox[-1].to, ["sidorova@yanao.ru"])
        self.assertIn("METHOD:CANCEL", self.ics(mail.outbox[-1]))

        # удалили событие — отмена оставшимся
        self.delete(ev["id"])
        self.assertEqual(mail.outbox[-1].to, ["kozlov@yanao.ru"])
        self.assertIn("METHOD:CANCEL", self.ics(mail.outbox[-1]))
        self.assertFalse(GuestDelivery.objects.exists())

    def test_reminder_goes_into_invitation_and_its_change_sends_update(self):
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"], reminder_minutes=15)
        first = self.ics(mail.outbox[0])
        self.assertIn("BEGIN:VALARM\nDESCRIPTION:REMINDER\nTRIGGER;RELATED=START:-PT15M\nACTION:DISPLAY\nEND:VALARM", first)

        # изменили только напоминание — участнику уходит обновление с новым временем
        self.patch(ev["id"], reminder_minutes=1440)
        self.assertEqual(len(mail.outbox), 2)
        self.assertIn("TRIGGER;RELATED=START:-P1D\n", self.ics(mail.outbox[1]))
        self.patch(ev["id"], reminder_minutes=0)
        self.assertIn("TRIGGER;RELATED=START:-PT0M\n", self.ics(mail.outbox[2]))

        # сняли напоминание — обновление без VALARM
        self.patch(ev["id"], reminder_minutes=None)
        self.assertEqual(len(mail.outbox), 4)
        self.assertNotIn("VALARM", self.ics(mail.outbox[3]))

    def test_cancellation_is_newer_than_invitation(self):
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        self.delete(ev["id"])
        seq = [int(line.split(":")[1]) for m in mail.outbox for line in self.ics(m).split("\n") if line.startswith("SEQUENCE:")]
        self.assertEqual(len(seq), 2)
        self.assertGreater(seq[1], seq[0])
        self.assertIn("STATUS:CANCELLED", self.ics(mail.outbox[1]))
        for msg in mail.outbox:  # часть text/calendar — с методом и одной кодировкой
            header = next(p for p in msg.message().walk() if p.get_content_type() == "text/calendar")["Content-Type"]
            self.assertEqual(header.lower().count("charset"), 1, header)
        self.assertIn("method=cancel", header.lower().replace('"', ""))

    def test_reminder_trigger_format(self):
        from .invitations import reminder_trigger

        self.assertEqual([reminder_trigger(m) for m in (0, 5, 60, 120, 1440, 2880, 10080, 1530)],
                         ["-PT0M", "-PT5M", "-PT1H", "-PT2H", "-P1D", "-P2D", "-P7D", "-P1DT1H30M"])

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.smtp.EmailBackend", EMAIL_HOST="127.0.0.1", EMAIL_PORT=9)
    def test_smtp_down_then_pending_delivery(self):
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertFalse(GuestDelivery.objects.exists())  # не отправилось — отметки нет
        from .guests import deliver_pending

        with self.settings(EMAIL_BACKEND="django.core.mail.backends.locmem.EmailBackend"):
            self.assertEqual(deliver_pending(), 1)
        self.assertEqual(mail.outbox[-1].to, ["sidorova@yanao.ru"])
        self.assertTrue(GuestDelivery.objects.filter(event_id=ev["id"], channel="email").exists())

    @override_settings(EMAIL_BACKEND="django.core.mail.backends.console.EmailBackend")
    def test_mail_not_configured_is_reported(self):
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        report = pop_delivery_report(ev["id"])
        self.assertIn("не настроен", report["failed"]["sidorova@yanao.ru"])
        self.assertFalse(GuestDelivery.objects.exists())

    def test_mail_server_from_admin_is_used(self):
        with FakeSMTP() as smtp:
            srv = MailServer(host="127.0.0.1", port=smtp.port, security="none", username="calendar@yanao.ru",
                             from_email="calendar@yanao.ru")
            srv.password = "secret"
            srv.save()
            ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertEqual(pop_delivery_report(ev["id"])["invited"], ["sidorova@yanao.ru"])
        self.assertEqual(len(smtp.messages), 1)
        mail_from, rcpt, data = smtp.messages[0]
        self.assertEqual((mail_from, rcpt), ("calendar@yanao.ru", ["sidorova@yanao.ru"]))
        self.assertIn("text/calendar", data)
        self.assertIn("METHOD:REQUEST", data)
        self.assertEqual(smtp.logins, [("calendar@yanao.ru", "secret")])
        srv.refresh_from_db()
        self.assertIsNotNone(srv.last_ok_at)

    def test_sender_is_common_address_by_default(self):
        with self.settings(DEFAULT_FROM_EMAIL="calendar@yanao.ru"):
            self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertEqual(mail.outbox[0].from_email, "calendar@yanao.ru")
        self.assertEqual(mail.outbox[0].reply_to, ["ivan@yanao.ru"])

    @override_settings(INTEGRATIONS_MAIL_FROM_USER=True)
    def test_sender_is_user_who_created_event(self):
        self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertEqual(mail.outbox[0].from_email, '"Иван Иванов" <ivan@yanao.ru>')
        # событие в календаре Иванова создал Петров (менеджер) — письмо от Петрова, организатор — владелец
        event = Event.objects.create(title="Планёрка", start=self.start, owner=self.user, created_by=self.petrov)
        from .invitations import build_messages

        [msg] = build_messages(event, ["sidorova@yanao.ru"])
        self.assertEqual(msg.from_email, '"Пётр Петров" <Petrov@yanao.ru>')
        self.assertEqual(msg.reply_to, ["ivan@yanao.ru"])
        self.assertIn("ORGANIZER", msg.attachments[0][1])
        # автор неизвестен — от владельца календаря; настройка выключена — от общего адреса
        event.created_by = None
        self.assertEqual(build_messages(event, ["sidorova@yanao.ru"])[0].from_email, '"Иван Иванов" <ivan@yanao.ru>')
        with self.settings(INTEGRATIONS_MAIL_FROM_USER=False, DEFAULT_FROM_EMAIL="calendar@yanao.ru"):
            self.assertEqual(build_messages(event, ["sidorova@yanao.ru"])[0].from_email, "calendar@yanao.ru")

    @override_settings(INTEGRATIONS_MAIL_FROM_USER=True)
    def test_server_refusing_user_address_falls_back_to_common_address(self):
        with FakeSMTP() as smtp:
            smtp.refused_senders.add("ivan@yanao.ru")
            MailServer.objects.create(host="127.0.0.1", port=smtp.port, security="none", from_email="calendar@yanao.ru")
            ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        self.assertEqual(pop_delivery_report(ev["id"])["invited"], ["sidorova@yanao.ru"])
        [(mail_from, rcpt, data)] = smtp.messages
        self.assertEqual((mail_from, rcpt), ("calendar@yanao.ru", ["sidorova@yanao.ru"]))
        self.assertRegex(data, r"(?m)^Reply-To: .*ivan@yanao\.ru")

    # ---- файлы события в приглашении

    def upload(self, ev_id, *names, query="", size=20):
        files = [SimpleUploadedFile(name, b"x" * size, content_type="text/plain") for name in names]
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(reverse("events:attachment-upload", args=[ev_id]) + query, {"files": files})

    def attached(self, msg):
        return [a[0] for a in msg.attachments]

    def test_new_event_with_files_is_invited_once_with_files(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            # страница сообщает, что следом загрузит файлы, — приглашение ждёт их
            ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"], files_pending=True)
            self.assertEqual(mail.outbox, [])
            resp = self.upload(ev["id"], "Текст.txt", "Повестка.txt")
            self.assertEqual(resp.status_code, 201, resp.content)
            self.assertEqual(resp.json()["delivery"]["invited"], ["sidorova@yanao.ru"])
            [msg] = mail.outbox
            self.assertEqual(self.attached(msg), ["invite.ics", "Текст.txt", "Повестка.txt"])
            self.assertEqual(msg.attachments[1][1], b"x" * 20)
            self.assertIn("Файлы события во вложении: Текст.txt, Повестка.txt", msg.body)

    def test_files_added_and_removed_later_send_update(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
            self.assertEqual(self.attached(mail.outbox[0]), ["invite.ics"])
            # не последняя операция с файлами (?deliver=0) — письма нет
            self.upload(ev["id"], "a.txt", query="?deliver=0")
            self.assertEqual(len(mail.outbox), 1)
            first = self.upload(ev["id"], "b.txt").json()
            self.assertEqual(first["delivery"]["updated"], ["sidorova@yanao.ru"])
            self.assertEqual(self.attached(mail.outbox[-1]), ["invite.ics", "a.txt", "b.txt"])
            # без изменений — повторно не пишем
            self.patch(ev["id"], color="red")
            self.assertEqual(len(mail.outbox), 2)
            with self.captureOnCommitCallbacks(execute=True):
                self.client.delete(reverse("events:attachment-delete", args=[first["attachments"][0]["id"]]))
            self.assertEqual(self.attached(mail.outbox[-1]), ["invite.ics", "a.txt"])
            self.assertEqual(len(mail.outbox), 3)

    def test_rejected_upload_still_sends_waiting_invitation(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"], files_pending=True)
            with self.captureOnCommitCallbacks(execute=True):
                resp = self.client.post(reverse("events:attachment-upload", args=[ev["id"]]), {})
            self.assertEqual(resp.status_code, 400)
            self.assertEqual(self.attached(mail.outbox[0]), ["invite.ics"])

    def test_files_over_mail_limit_are_listed_not_attached(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA, INTEGRATIONS_MAIL_MAX_ATTACHMENTS_MB=1):
            ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"], files_pending=True)
            self.upload(ev["id"], "малый.txt", "большой.txt", query="?deliver=0", size=10)
            Attachment.objects.filter(name="большой.txt").update(size=2 * 1024 * 1024)
            self.upload(ev["id"], "ещё.txt")
            msg = mail.outbox[-1]
            self.assertEqual(self.attached(msg), ["invite.ics", "малый.txt", "ещё.txt"])
            self.assertIn("Не вложены", msg.body)
            self.assertIn("большой.txt", msg.body)

    def test_cancellation_has_no_files(self):
        with override_settings(MEDIA_ROOT=TEMP_MEDIA):
            ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"], files_pending=True)
            self.upload(ev["id"], "a.txt")
            self.delete(ev["id"])
            self.assertEqual(self.attached(mail.outbox[-1]), ["invite.ics"])
            self.assertIn("METHOD:CANCEL", self.ics(mail.outbox[-1]))

    def test_mail_server_error_is_explained(self):
        srv = MailServer(host="127.0.0.1", port=9, security="none", from_email="calendar@yanao.ru")
        srv.save()
        ev = self.create(guest_emails="sidorova@yanao.ru", directory_guests=["sidorova@yanao.ru"])
        reason = pop_delivery_report(ev["id"])["failed"]["sidorova@yanao.ru"]
        self.assertIn("отказал в подключении", reason)
        srv.refresh_from_db()
        self.assertIn("отказал", srv.last_error)

    def test_typed_address_found_in_directory_is_synced(self):
        from unittest import mock

        with mock.patch("directory.search.emails_in_directory", return_value={"sidorova@yanao.ru"}) as lookup:
            ev = self.create(guest_emails="Sidorova@yanao.ru, friend@mail.ru")
        lookup.assert_called_once()
        self.assertEqual(ev["directory_guests"], ["sidorova@yanao.ru"])
        self.assertEqual([m.to for m in mail.outbox], [["sidorova@yanao.ru"]])

    def test_manual_invite_skips_auto_delivered(self):
        ev = self.create(guest_emails="sidorova@yanao.ru, friend@mail.ru", directory_guests=["sidorova@yanao.ru"])
        resp = self.client.post(reverse("integrations:invite", args=[ev["id"]]), "{}", content_type="application/json")
        self.assertEqual(resp.json()["sent"], ["friend@mail.ru"])
        self.assertEqual(len(mail.outbox), 2)

    def test_owner_is_not_invited_to_own_event(self):
        self.create(guest_emails="ivan@yanao.ru", directory_guests=["ivan@yanao.ru"])
        self.assertEqual(len(mail.outbox), 0)
