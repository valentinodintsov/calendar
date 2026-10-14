"""
Мгновенная отправка событий во внешний календарь (на примере Microsoft Exchange) и синхронизация с Exchange.
Используется локальный тестовый EWS-сервер; отправка выполняется синхронно (INTEGRATIONS_PUSH_ASYNC=False).
"""

import json
from datetime import timedelta
from datetime import timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Event

from .models import ExternalCalendar, SyncedEvent
from .push import push_event, suppress_push
from .sync import sync_calendar
from .testing_ews import FakeEWS


def ews_time(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


@override_settings(INTEGRATIONS_PUSH_ASYNC=False)
class ExchangePushTests(TestCase):
    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.user = get_user_model().objects.create_user("ivan", "ivan@yanao.ru", "x")
        self.client.force_login(self.user)
        self.cal = ExternalCalendar(
            user=self.user, provider=ExternalCalendar.Provider.EXCHANGE, server_url=self.srv.url,
            username=self.srv.username, calendar_id="calendar", calendar_name="Календарь (основной)",
        )
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()
        self.start = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)

    def api_create(self, title="Совещание", **extra):
        local = timezone.localtime(self.start)
        payload = {"title": title, "start": local.strftime("%Y-%m-%dT%H:%M"),
                   "end": (local + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M"), **extra}
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse("events:events-collection"), json.dumps(payload), content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        return resp.json()

    def test_created_event_appears_in_exchange_immediately(self):
        ev = self.api_create(location="Каб. 305", description="Повестка", url="https://meet.example.com/x")
        item = self.srv.by_subject("Совещание")
        self.assertIsNotNone(item, "событие не попало в Exchange")
        self.assertEqual(item["Location"], "Каб. 305")
        self.assertEqual(item["Body"], "Повестка\n\nhttps://meet.example.com/x")  # ссылка — в открытом виде в тексте
        self.assertEqual(item["Url"], "https://meet.example.com/x")
        self.assertEqual(item["Start"], ews_time(self.start))
        link = SyncedEvent.objects.get(event_id=ev["id"])
        self.assertEqual((link.calendar, link.origin), (self.cal, "local"))

    def test_changed_and_moved_event_updated_immediately(self):
        ev = self.api_create()
        url = reverse("events:event-detail", args=[ev["id"]])
        local = timezone.localtime(self.start + timedelta(days=1))
        with self.captureOnCommitCallbacks(execute=True):
            self.client.patch(url, json.dumps({"title": "Совещание (перенос)", "start": local.strftime("%Y-%m-%dT%H:%M"),
                                               "end": (local + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M")}),
                              content_type="application/json")
        item = self.srv.by_subject("Совещание (перенос)")
        self.assertIsNotNone(item)
        self.assertEqual(item["Start"], ews_time(self.start + timedelta(days=1)))
        self.assertEqual(len(self.srv.items), 1)  # обновлено, а не создано заново

    def test_unchanged_content_not_sent_again(self):
        ev = self.api_create()
        before = list(self.srv.requests)
        with self.captureOnCommitCallbacks(execute=True):
            Event.objects.filter(pk=ev["id"]).first().save()  # пересохранили без изменений
            e = Event.objects.get(pk=ev["id"])
            e.color = "red"  # цвет в Exchange не передаётся
            e.save()
        self.assertNotIn("UpdateItem", self.srv.requests[len(before):])

    def test_deleted_event_removed_from_exchange_immediately(self):
        ev = self.api_create()
        self.assertEqual(len(self.srv.items), 1)
        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(reverse("events:event-detail", args=[ev["id"]]))
        self.assertEqual(self.srv.items, {})
        self.assertFalse(SyncedEvent.objects.exists())

    def test_import_only_calendar_not_written(self):
        self.cal.direction = ExternalCalendar.Direction.IMPORT
        self.cal.save()
        self.api_create()
        self.assertEqual(self.srv.items, {})

    def test_other_users_calendar_not_touched(self):
        other = get_user_model().objects.create_user("anna", "anna@yanao.ru", "x")
        with self.captureOnCommitCallbacks(execute=True):
            Event.objects.create(title="Чужое", start=self.start, end=self.start + timedelta(hours=1), owner=other)
        self.assertEqual(self.srv.items, {})

    def test_server_error_is_recorded_and_save_still_works(self):
        self.cal.credentials = {"password": "wrong"}
        self.cal.save()
        ev = self.api_create()  # сохранение не падает
        self.assertTrue(Event.objects.filter(pk=ev["id"]).exists())
        self.cal.refresh_from_db()
        self.assertIn("Не удалось сразу отправить", self.cal.last_error)
        # плановая синхронизация с правильным паролем досылает событие
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()
        res = sync_calendar(self.cal)
        self.assertEqual(res.created_remote, 1, res.errors)
        self.assertIsNotNone(self.srv.by_subject("Совещание"))

    def test_sync_import_does_not_echo_back(self):
        self.srv.add(Subject="Из Outlook", Start=ews_time(self.start), End=ews_time(self.start + timedelta(hours=1)))
        with self.captureOnCommitCallbacks(execute=True):
            res = sync_calendar(self.cal)
        self.assertEqual(res.created_local, 1, res.errors)
        self.assertEqual(self.srv.requests.count("CreateItem"), 0)
        self.assertEqual(self.srv.requests.count("UpdateItem"), 0)

    def test_two_way_sync_with_exchange(self):
        # из Outlook → к нам
        iid = self.srv.add(Subject="Планёрка", Body="Текст", Start=ews_time(self.start),
                           End=ews_time(self.start + timedelta(hours=1)), UID="0400000082OUTLOOK")
        res = sync_calendar(self.cal)
        self.assertEqual(res.created_local, 1, res.errors)
        ev = Event.objects.get(title="Планёрка")
        self.assertEqual((ev.description, ev.start), ("Текст", self.start))
        # повторная синхронизация — без изменений
        self.assertEqual(sync_calendar(self.cal).summary(), "изменений нет")
        # изменили в Outlook → обновилось у нас
        self.srv.items[iid]["Subject"] = "Планёрка (Outlook)"
        self.srv._bump(self.srv.items[iid])
        self.assertEqual(sync_calendar(self.cal).updated_local, 1)
        ev.refresh_from_db()
        self.assertEqual(ev.title, "Планёрка (Outlook)")
        # изменили у нас → сразу ушло в Exchange
        with self.captureOnCommitCallbacks(execute=True):
            ev.title = "Планёрка (календарь)"
            ev.save()
        self.assertEqual(self.srv.items[iid]["Subject"], "Планёрка (календарь)")
        self.assertEqual(sync_calendar(self.cal).summary(), "изменений нет")

    def test_suppress_push(self):
        with self.captureOnCommitCallbacks(execute=True):
            with suppress_push():
                Event.objects.create(title="Тихо", start=self.start, end=self.start + timedelta(hours=1), owner=self.user)
        self.assertEqual(self.srv.items, {})
        e = Event.objects.get(title="Тихо")
        self.assertEqual(push_event(e.pk)["created"], 1)  # явная отправка работает

    def test_connect_exchange_via_api(self):
        self.cal.delete()
        base = {"provider": "exchange", "server_url": self.srv.url, "username": self.srv.username, "password": self.srv.password}
        resp = self.client.post(reverse("integrations:discover"), json.dumps(base), content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        ids = [c["id"] for c in resp.json()["calendars"]]
        self.assertEqual(ids, ["calendar", "SUB1"])
        resp = self.client.post(reverse("integrations:calendars"),
                                json.dumps({**base, "calendar_id": "calendar", "calendar_name": "Календарь"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["calendar"]["provider"], "exchange")


@override_settings(INTEGRATIONS_PUSH_ASYNC=False)
class ImportFromOutlookTests(TestCase):
    """Событие, созданное в Outlook: участники и вложения тоже приходят к нам."""

    def setUp(self):
        import tempfile

        media = tempfile.mkdtemp()
        self.addCleanup(__import__("shutil").rmtree, media, True)
        self.media = override_settings(MEDIA_ROOT=media)
        self.media.enable()
        self.addCleanup(self.media.disable)
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        User = get_user_model()
        self.user = User.objects.create_user("vnodintsov", "vnodintsov@yanao.ru", "x")
        self.colleague = User.objects.create_user("imryabkov", "IMRyabkov@yanao.ru", "x")
        self.cal = ExternalCalendar(user=self.user, provider=ExternalCalendar.Provider.EXCHANGE, server_url=self.srv.url,
                                    username=self.srv.username, calendar_id="calendar")
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()
        day = (timezone.now() + timedelta(days=3)).date()
        self.iid = self.srv.add(
            Subject="Тест", IsAllDayEvent="true", Location="каб. 500", Body="https://bbb.yanao.ru/b/x\n\nТекст",
            Start=f"{day - timedelta(days=1)}T19:00:00Z", End=f"{day}T19:00:00Z", UID="040000008200E0OUTLOOK",
            Attendees=["vnodintsov@yanao.ru", "imryabkov@yanao.ru", "petrov@mail.ru"],
            Files=[{"Name": "file.doc", "ContentType": "application/msword", "Content": b"DOC"}],
        )

    def event(self):
        return Event.objects.get(title="Тест")

    def test_all_fields_imported(self):
        res = sync_calendar(self.cal)
        self.assertEqual(res.created_local, 1, res.errors)
        ev = self.event()
        self.assertEqual((ev.all_day, ev.location), (True, "каб. 500"))
        self.assertEqual(ev.description, "https://bbb.yanao.ru/b/x\n\nТекст")
        self.assertEqual(list(ev.invitees.all()), [self.colleague])  # сотрудник — участником
        self.assertEqual(ev.guest_list(), ["petrov@mail.ru"])  # остальные — гостями; владелец — нет
        self.assertEqual(ev.directory_guests, "")  # Outlook уже разослал приглашения — сами не рассылаем
        att = ev.attachments.get()
        self.assertEqual((att.name, att.size), ("file.doc", 3))
        with att.file.open("rb") as fh:
            self.assertEqual(fh.read(), b"DOC")
        # повторная синхронизация — без дублей и лишних загрузок
        n = self.srv.requests.count("GetAttachment")
        self.assertEqual(sync_calendar(self.cal).summary(), "изменений нет")
        self.assertEqual((ev.attachments.count(), self.srv.requests.count("GetAttachment")), (1, n))

    def test_changes_in_outlook(self):
        sync_calendar(self.cal)
        item = self.srv.items[self.iid]
        item["Attendees"] = ["imryabkov@yanao.ru", "sidorova@yanao.ru"]
        item["Files"] = [{"Name": "new.pdf", "Content": b"PDF"}]
        self.srv._bump(item)
        sync_calendar(self.cal)
        ev = self.event()
        self.assertEqual(ev.guest_list(), ["sidorova@yanao.ru"])
        self.assertEqual(list(ev.attachments.values_list("name", flat=True)), ["new.pdf"])

    def test_invited_colleague_does_not_get_duplicate(self):
        sync_calendar(self.cal)
        with FakeEWS() as srv2:  # у коллеги та же встреча в его Exchange (тот же UID)
            item = dict(self.srv.items[self.iid])
            srv2.add(**{k: v for k, v in item.items() if k not in ("ChangeKey",)})
            cal2 = ExternalCalendar(user=self.colleague, provider=ExternalCalendar.Provider.EXCHANGE, server_url=srv2.url,
                                    username=srv2.username, calendar_id="calendar")
            cal2.credentials = {"password": srv2.password}
            cal2.save()
            res = sync_calendar(cal2)
        self.assertEqual(res.created_local, 0, res.errors)
        self.assertEqual(Event.objects.count(), 1)


@override_settings(INTEGRATIONS_PUSH_ASYNC=False)
class SendToExchangeTests(TestCase):
    """Событие из календаря — в Exchange: ссылка в описании, файлы — вложениями."""

    def setUp(self):
        import shutil
        import tempfile

        media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, media, True)
        self.media = override_settings(MEDIA_ROOT=media)
        self.media.enable()
        self.addCleanup(self.media.disable)
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.user = get_user_model().objects.create_user("vnodintsov", "vnodintsov@yanao.ru", "x")
        self.client.force_login(self.user)
        self.cal = ExternalCalendar(user=self.user, provider=ExternalCalendar.Provider.EXCHANGE, server_url=self.srv.url,
                                    username=self.srv.username, calendar_id="calendar")
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()

    def item(self):
        self.assertEqual(len(self.srv.items), 1)
        return next(iter(self.srv.items.values()))

    def test_link_and_file_reach_exchange(self):
        day = (timezone.localdate() + timedelta(days=2)).isoformat()
        payload = {"title": "Тест", "all_day": True, "start": f"{day}T00:00", "end": f"{day}T23:59",
                   "location": "каб. 500", "url": "https://bbb.yanao.ru", "description": "Текст"}
        with self.captureOnCommitCallbacks(execute=True):
            ev = self.client.post(reverse("events:events-collection"), json.dumps(payload), content_type="application/json").json()
        self.assertEqual(self.item()["Body"], "Текст\n\nhttps://bbb.yanao.ru")
        self.assertEqual(self.item()["IsAllDayEvent"], "true")

        from django.core.files.uploadedfile import SimpleUploadedFile

        upload = SimpleUploadedFile("файл.txt", "Привет".encode(), content_type="text/plain")
        with self.captureOnCommitCallbacks(execute=True):
            resp = self.client.post(reverse("events:attachment-upload", args=[ev["id"]]), {"files": [upload]})
        self.assertEqual(resp.status_code, 201, resp.content)
        files = self.item()["Files"]
        self.assertEqual([(f["Name"], f["Content"]) for f in files], [("файл.txt", "Привет".encode())])
        att_id = resp.json()["attachments"][0]["id"]
        self.assertTrue(Event.objects.get(pk=ev["id"]).attachments.get().external_id)

        # синхронизация не создаёт копию файла и не считает событие изменённым
        self.assertEqual(sync_calendar(self.cal).summary(), "изменений нет")
        self.assertEqual(Event.objects.get(pk=ev["id"]).attachments.count(), 1)

        with self.captureOnCommitCallbacks(execute=True):
            self.client.delete(reverse("events:attachment-delete", args=[att_id]))
        self.assertEqual(self.item()["Files"], [])

    def test_file_added_before_exchange_item_exists_is_sent_by_sync(self):
        self.cal.credentials = {"password": "wrong"}
        self.cal.save()
        start = timezone.localtime(timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        ev = Event.objects.create(title="Позже", start=start, end=start + timedelta(hours=1), owner=self.user)
        from django.core.files.base import ContentFile

        from events.models import Attachment

        Attachment.objects.create(event=ev, file=ContentFile(b"X", name="a.txt"), name="a.txt", size=1)
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()
        res = sync_calendar(self.cal)
        self.assertEqual(res.created_remote, 1, res.errors)
        self.assertEqual([f["Name"] for f in self.item()["Files"]], ["a.txt"])

    def test_old_item_without_link_in_body_is_repaired(self):
        start = timezone.localtime(timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)
        with self.captureOnCommitCallbacks(execute=True):
            Event.objects.create(title="Старое", start=start, end=start + timedelta(hours=1), owner=self.user,
                                 description="Текст", url="https://bbb.yanao.ru")
        item = self.item()
        item["Body"] = "Текст"  # так записывала прошлая версия: ссылка только в скрытом свойстве
        self.srv._bump(item)
        sync_calendar(self.cal)
        self.assertEqual(self.item()["Body"], "Текст\n\nhttps://bbb.yanao.ru")
        self.assertEqual(Event.objects.get(title="Старое").url, "https://bbb.yanao.ru")


@override_settings(INTEGRATIONS_PUSH_ASYNC=False)
class ReminderExchangeTests(TestCase):
    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.user = get_user_model().objects.create_user("ivan", "ivan@yanao.ru", "x")
        self.cal = ExternalCalendar(user=self.user, provider=ExternalCalendar.Provider.EXCHANGE, server_url=self.srv.url,
                                    username=self.srv.username, calendar_id="calendar")
        self.cal.credentials = {"password": self.srv.password}
        self.cal.save()
        self.start = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)

    def test_reminder_goes_to_exchange_and_back(self):
        with self.captureOnCommitCallbacks(execute=True):
            ev = Event.objects.create(title="С напоминанием", start=self.start, end=self.start + timedelta(hours=1),
                                      owner=self.user, reminder_minutes=30)
        item = next(iter(self.srv.items.values()))
        self.assertEqual((item["ReminderIsSet"], item["ReminderMinutesBeforeStart"]), ("true", "30"))
        self.assertEqual(sync_calendar(self.cal).summary(), "изменений нет")
        # поменяли напоминание в Outlook
        item["ReminderMinutesBeforeStart"] = "60"
        self.srv._bump(item)
        self.assertEqual(sync_calendar(self.cal).updated_local, 1)
        ev.refresh_from_db()
        self.assertEqual(ev.reminder_minutes, 60)
        # выключили у нас — выключилось в Exchange
        with self.captureOnCommitCallbacks(execute=True):
            ev.reminder_minutes = None
            ev.save()
        self.assertEqual(item["ReminderIsSet"], "false")

    def test_existing_links_are_not_disturbed(self):
        with self.captureOnCommitCallbacks(execute=True):
            Event.objects.create(title="Без напоминания", start=self.start, end=self.start + timedelta(hours=1), owner=self.user)
        self.assertEqual(sync_calendar(self.cal).summary(), "изменений нет")
