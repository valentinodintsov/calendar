# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Тесты адаптеров CalDAV (против локального тестового сервера) и Google (с подменой HTTP). Без Django."""

from datetime import datetime, timedelta, timezone
from unittest import TestCase, mock
from zoneinfo import ZoneInfo

from integrations.ics import EventData
from integrations.providers.base import AuthError, ConflictError
from integrations.providers.caldav import CalDAVProvider
from integrations.providers.google import GoogleProvider, authorization_url, event_from_google, event_to_google
from integrations.testing import FakeCalDAV

YEKT = ZoneInfo("Asia/Yekaterinburg")
UTC = timezone.utc
W_START = datetime(2026, 10, 1, tzinfo=YEKT)
W_END = datetime(2026, 11, 1, tzinfo=YEKT)


def sample(uid="ev-1", title="Планёрка", day=12):
    return EventData(
        uid=uid, title=title, description="Повестка", location="Офис", url="https://example.com",
        start=datetime(2026, 10, day, 10, tzinfo=YEKT), end=datetime(2026, 10, day, 11, tzinfo=YEKT),
    )


class CalDAVTests(TestCase):
    def test_discovery_via_principal(self):
        with FakeCalDAV() as srv:
            cals = CalDAVProvider(srv.url, srv.username, srv.password).list_calendars()
            self.assertEqual({c["name"] for c in cals}, {"Работа", "Дом"})  # календарь задач VTODO отброшен
            work = next(c for c in cals if c["name"] == "Работа")
            self.assertEqual(work["id"], srv.url + "/cal/user/work/")
            self.assertEqual(work["color"], "#FF2968")
            self.assertFalse(work["read_only"])

    def test_discovery_with_redirect_and_direct_calendar_url(self):
        with FakeCalDAV(redirect_root=True) as srv:
            self.assertEqual(len(CalDAVProvider(srv.url, srv.username, srv.password).list_calendars()), 2)
            direct = CalDAVProvider(srv.url + "/cal/user/home/", srv.username, srv.password).list_calendars()
            self.assertEqual([c["name"] for c in direct], ["Дом"])

    def test_wrong_password(self):
        with FakeCalDAV() as srv:
            with self.assertRaises(AuthError):
                CalDAVProvider(srv.url, srv.username, "wrong").list_calendars()

    def test_create_list_update_delete(self):
        with FakeCalDAV() as srv:
            p = CalDAVProvider(srv.url, srv.username, srv.password, srv.url + "/cal/user/work/", YEKT)
            item = p.create_event(sample())
            self.assertTrue(item.remote_id.endswith("/cal/user/work/ev-1.ics"))
            self.assertTrue(item.etag)

            [listed] = p.list_events(W_START, W_END)
            self.assertEqual(listed.data.content_key(), sample().content_key())
            self.assertEqual(listed.etag, item.etag)

            new_etag = p.update_event(item.remote_id, item.etag, sample(title="Планёрка (перенос)"))
            self.assertNotEqual(new_etag, item.etag)
            with self.assertRaises(ConflictError):  # устаревший ETag
                p.update_event(item.remote_id, item.etag, sample(title="x"))
            self.assertEqual(p.list_events(W_START, W_END)[0].data.title, "Планёрка (перенос)")

            p.delete_event(item.remote_id, new_etag)
            self.assertEqual(p.list_events(W_START, W_END), [])
            p.delete_event(item.remote_id, new_etag)  # повторное удаление — не ошибка

    def test_recurring_from_server_is_expanded_read_only(self):
        with FakeCalDAV() as srv:
            srv.put("/cal/user/work/r.ics",
                    "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nUID:r\r\nDTSTART:20261005T050000Z\r\n"
                    "DTEND:20261005T060000Z\r\nRRULE:FREQ=WEEKLY;COUNT=3\r\nSUMMARY:Еженедельно\r\n"
                    "END:VEVENT\r\nEND:VCALENDAR\r\n")
            p = CalDAVProvider(srv.url, srv.username, srv.password, srv.url + "/cal/user/work/", YEKT)
            items = p.list_events(W_START, W_END)
            self.assertEqual(len(items), 3)
            self.assertTrue(all(i.data.read_only for i in items))
            self.assertEqual(len({i.data.uid for i in items}), 3)


class GoogleMappingTests(TestCase):
    def test_timed_event(self):
        ev = event_from_google({
            "id": "g1", "iCalUID": "g1@google.com", "etag": '"1"', "summary": "Звонок",
            "start": {"dateTime": "2026-10-12T10:00:00+05:00"}, "end": {"dateTime": "2026-10-12T10:30:00+05:00"},
            "updated": "2026-10-01T08:00:00.000Z",
            "extendedProperties": {"private": {"calendar_url": "https://example.com"}},
        }, YEKT)
        self.assertEqual(ev.uid, "g1@google.com")
        self.assertEqual(ev.url, "https://example.com")
        self.assertEqual(ev.end - ev.start, timedelta(minutes=30))
        self.assertFalse(ev.read_only)

    def test_all_day_and_recurring_instance(self):
        ev = event_from_google({
            "id": "g2_20261020", "iCalUID": "g2@google.com", "recurringEventId": "g2",
            "originalStartTime": {"date": "2026-10-20"},
            "start": {"date": "2026-10-20"}, "end": {"date": "2026-10-21"},
        }, YEKT)
        self.assertTrue(ev.all_day and ev.read_only)
        self.assertEqual(ev.end, datetime(2026, 10, 20, 23, 59, tzinfo=YEKT))
        self.assertTrue(ev.uid.startswith("g2@google.com#"))
        self.assertIsNone(event_from_google({"id": "x", "status": "cancelled"}, YEKT))

    def test_to_google_roundtrip(self):
        src = sample()
        body = event_to_google(src, YEKT)
        self.assertEqual(body["start"]["timeZone"], "Asia/Yekaterinburg")
        back = event_from_google({"id": "1", "iCalUID": src.uid, **body}, YEKT)
        self.assertEqual(back.content_key(), src.content_key())
        allday = EventData(uid="a", title="Отпуск", all_day=True,
                           start=datetime(2026, 10, 20, tzinfo=YEKT), end=datetime(2026, 10, 22, 23, 59, tzinfo=YEKT))
        self.assertEqual(event_to_google(allday, YEKT)["end"], {"date": "2026-10-23"})

    def test_authorization_url(self):
        url = authorization_url("cid", "https://host/cb/", "st")
        self.assertIn("access_type=offline", url)
        self.assertIn("calendar", url)


class GoogleProviderTests(TestCase):
    def _resp(self, status, json=None):
        r = mock.Mock(status_code=status, content=b"{}" if json is not None else b"")
        r.json.return_value = json or {}
        return r

    def test_refreshes_token_and_retries_on_401(self):
        saved = {}
        p = GoogleProvider("cid", "secret", {"refresh_token": "rt", "access_token": "old", "expires_at": 9e18},
                           "primary", YEKT, on_tokens=saved.update)
        with mock.patch("integrations.providers.google.requests.request") as req, \
             mock.patch("integrations.providers.google.requests.post") as post:
            req.side_effect = [self._resp(401), self._resp(200, {"items": []})]
            post.return_value = self._resp(200, {"access_token": "new", "expires_in": 3600})
            self.assertEqual(p.list_events(W_START, W_END), [])
            self.assertEqual(req.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer new")
            self.assertEqual(saved["access_token"], "new")

    def test_update_conflict(self):
        p = GoogleProvider("cid", "secret", {"refresh_token": "rt", "access_token": "a", "expires_at": 9e18}, "primary", YEKT)
        with mock.patch("integrations.providers.google.requests.request", return_value=self._resp(412)):
            with self.assertRaises(ConflictError):
                p.update_event("e1", '"1"', sample())
