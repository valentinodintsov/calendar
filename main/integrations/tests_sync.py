"""Сквозные тесты двусторонней синхронизации против локального CalDAV-сервера."""

from datetime import timedelta, timezone as dt_timezone

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from events.models import Event

from .ics import parse_vevents
from .models import ExternalCalendar, SyncedEvent
from .sync import sync_calendar
from .testing import FakeCalDAV

CAL_PATH = "/cal/user/work/"


def ics_dt(dt):
    return dt.astimezone(dt_timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def remote_ics(uid, title, start, hours=1, extra="", modified=None):
    lm = f"LAST-MODIFIED:{ics_dt(modified)}\r\n" if modified else ""
    return (
        "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nBEGIN:VEVENT\r\n"
        f"UID:{uid}\r\nDTSTART:{ics_dt(start)}\r\nDTEND:{ics_dt(start + timedelta(hours=hours))}\r\n"
        f"SUMMARY:{title}\r\n{lm}{extra}END:VEVENT\r\nEND:VCALENDAR\r\n"
    )


class SyncTests(TestCase):
    def setUp(self):
        self.srv = FakeCalDAV().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.user = get_user_model().objects.create_user("ivan", "ivan@example.com", "x")
        self.cal = self.make_cal()
        self.base = (timezone.now() + timedelta(days=2)).replace(minute=0, second=0, microsecond=0)

    def make_cal(self, **kw):
        fields = {"provider": ExternalCalendar.Provider.YANDEX, "calendar_id": self.srv.url + CAL_PATH,
                  "calendar_name": "Работа", **kw}
        cal = ExternalCalendar(user=self.user, server_url=self.srv.url, username=self.srv.username, **fields)
        cal.credentials = {"password": self.srv.password}
        cal.save()
        return cal

    def sync(self):
        res = sync_calendar(self.cal)
        self.assertEqual(res.errors, [], res.errors)
        return res

    def remote_titles(self):
        titles = []
        for _, ics in self.srv.resources.values():
            titles += [e.title for e in parse_vevents(ics)]
        return sorted(titles)

    def local_event(self, title="Планёрка", **kw):
        return Event.objects.create(
            title=title, start=self.base, end=self.base + timedelta(hours=1), owner=self.user, created_by=self.user, **kw
        )

    # ------------------------------------------------------------------

    def test_import_new_remote_event(self):
        self.srv.put(CAL_PATH + "a.ics", remote_ics("a@yandex", "Звонок с клиентом", self.base))
        res = self.sync()
        self.assertEqual(res.created_local, 1)
        ev = Event.objects.get(title="Звонок с клиентом")
        self.assertEqual(ev.owner, self.user)
        self.assertEqual(ev.color, "gray")
        self.assertEqual(ev.start, self.base)
        self.assertEqual(SyncedEvent.objects.get().origin, "remote")
        # повторная синхронизация ничего не меняет
        self.assertEqual(self.sync().summary(), "изменений нет")

    def test_export_new_local_event(self):
        self.local_event(description="Повестка", location="Офис")
        res = self.sync()
        self.assertEqual(res.created_remote, 1)
        self.assertEqual(self.remote_titles(), ["Планёрка"])
        self.assertEqual(self.sync().summary(), "изменений нет")

    def test_changes_flow_both_ways(self):
        ev = self.local_event()
        self.sync()
        # изменили у нас → уходит туда
        ev.title = "Планёрка (перенос)"
        ev.save()
        self.assertEqual(self.sync().updated_remote, 1)
        self.assertEqual(self.remote_titles(), ["Планёрка (перенос)"])
        # изменили там → приходит к нам
        link = SyncedEvent.objects.get()
        href = link.remote_id.replace(self.srv.url, "")
        self.srv.put(href, remote_ics(link.uid, "Планёрка в 12:00", self.base + timedelta(hours=2)))
        self.assertEqual(self.sync().updated_local, 1)
        ev.refresh_from_db()
        self.assertEqual(ev.title, "Планёрка в 12:00")
        self.assertEqual(ev.start, self.base + timedelta(hours=2))

    def test_deletions_flow_both_ways(self):
        ev = self.local_event()
        self.srv.put(CAL_PATH + "b.ics", remote_ics("b@yandex", "Удалим там", self.base))
        self.sync()
        self.assertEqual(len(self.srv.resources), 2)
        # удалили у нас → удаляется там
        ev.delete()
        self.assertEqual(self.sync().deleted_remote, 1)
        self.assertEqual(self.remote_titles(), ["Удалим там"])
        # удалили там → удаляется у нас
        del self.srv.resources[CAL_PATH + "b.ics"]
        self.assertEqual(self.sync().deleted_local, 1)
        self.assertFalse(Event.objects.exists())
        self.assertFalse(SyncedEvent.objects.exists())

    def test_conflict_newer_change_wins(self):
        ev = self.local_event()
        self.sync()
        link = SyncedEvent.objects.get()
        href = link.remote_id.replace(self.srv.url, "")
        # там изменили давно, у нас — сейчас: побеждает наше
        self.srv.put(href, remote_ics(link.uid, "Старое изменение", self.base, modified=timezone.now() - timedelta(days=1)))
        ev.title = "Свежее изменение"
        ev.save()
        res = self.sync()
        self.assertEqual((res.conflicts, res.updated_remote), (1, 1))
        self.assertEqual(self.remote_titles(), ["Свежее изменение"])
        # там изменили позже, чем у нас: побеждает внешнее
        ev.title = "Наше"
        ev.save()
        self.srv.put(href, remote_ics(link.uid, "Их, позже", self.base, modified=timezone.now() + timedelta(minutes=5)))
        res = self.sync()
        self.assertEqual((res.conflicts, res.updated_local), (1, 1))
        ev.refresh_from_db()
        self.assertEqual(ev.title, "Их, позже")

    def test_import_only_and_export_only(self):
        self.cal.direction = ExternalCalendar.Direction.IMPORT
        self.cal.save()
        self.local_event("Только у нас")
        self.srv.put(CAL_PATH + "c.ics", remote_ics("c@yandex", "Только там", self.base))
        res = self.sync()
        self.assertEqual((res.created_local, res.created_remote), (1, 0))
        self.assertEqual(self.remote_titles(), ["Только там"])

        self.cal.direction = ExternalCalendar.Direction.EXPORT
        self.cal.save()
        Event.objects.all().delete()
        SyncedEvent.objects.all().delete()
        self.local_event("Наше для экспорта")
        res = self.sync()
        self.assertEqual((res.created_local, res.created_remote), (0, 1))
        self.assertEqual(sorted(Event.objects.values_list("title", flat=True)), ["Наше для экспорта"])

    def test_recurring_instances_are_read_only(self):
        self.srv.put(CAL_PATH + "r.ics", remote_ics("r@yandex", "Еженедельно", self.base, extra="RRULE:FREQ=WEEKLY;COUNT=3\r\n"))
        self.assertEqual(self.sync().created_local, 3)
        self.assertTrue(all(SyncedEvent.objects.values_list("read_only", flat=True)))
        ev = Event.objects.order_by("start").first()
        ev.title = "Поменяли у себя"
        ev.save()
        res = self.sync()  # во внешний календарь не уходит, возвращается внешняя версия
        self.assertEqual((res.updated_remote, res.updated_local), (0, 1))
        ev.refresh_from_db()
        self.assertEqual(ev.title, "Еженедельно")

    def test_imported_events_are_not_exported_to_other_calendars(self):
        self.srv.calendars["/cal/user/home/"] = "Дом"
        self.srv.put(CAL_PATH + "d.ics", remote_ics("d@yandex", "Из «Работы»", self.base))
        self.sync()
        other = self.make_cal(calendar_id=self.srv.url + "/cal/user/home/", provider=ExternalCalendar.Provider.MAILRU)
        res = sync_calendar(other)
        self.assertEqual(res.created_remote, 0)

    def test_empty_server_response_does_not_wipe_local(self):
        for i in range(3):
            self.local_event(f"Событие {i}")
        self.sync()
        self.srv.resources.clear()  # сбой: сервер вдруг вернул пустой календарь
        res = self.sync()
        self.assertEqual(res.deleted_local, 0)
        self.assertEqual(Event.objects.count(), 3)

    def test_wrong_password_is_reported(self):
        self.cal.credentials = {"password": "wrong"}
        self.cal.save()
        res = sync_calendar(self.cal)
        self.assertTrue(res.errors)
        self.cal.refresh_from_db()
        self.assertIn("пароль", self.cal.last_error)
        self.assertIsNotNone(self.cal.last_sync_at)

    def test_credentials_are_encrypted(self):
        raw = ExternalCalendar.objects.values_list("secret", flat=True).get(pk=self.cal.pk)
        self.assertNotIn(self.srv.password, raw)
        self.assertEqual(ExternalCalendar.objects.get(pk=self.cal.pk).credentials, {"password": self.srv.password})
