# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Тесты API интеграций и приглашений по e-mail (Django)."""

import json
from datetime import timedelta
from unittest import mock

from django.contrib.auth import get_user_model
from django.core import mail
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from events.models import Event

from .models import ExternalCalendar, SyncedEvent
from .testing import FakeCalDAV
from .tests_sync import CAL_PATH, remote_ics


def make_user(username, owner=True, **extra):
    user = get_user_model().objects.create_user(username, extra.pop("email", f"{username}@example.com"), "x", **extra)
    emp = user.employee
    emp.is_owner = owner
    emp.save()
    return user


class IntegrationsApiTests(TestCase):
    def setUp(self):
        self.srv = FakeCalDAV().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.user = make_user("ivan")
        self.client.force_login(self.user)
        self.base = (timezone.now() + timedelta(days=1)).replace(minute=0, second=0, microsecond=0)

    def post(self, url, data):
        return self.client.post(url, json.dumps(data), content_type="application/json")

    def creds(self, **extra):
        return {"provider": "yandex", "server_url": self.srv.url, "username": self.srv.username,
                "password": self.srv.password, **extra}

    def test_providers_listed(self):
        data = self.client.get(reverse("integrations:calendars")).json()
        codes = [p["code"] for p in data["providers"]]
        self.assertEqual(codes, ["google", "exchange", "yandex", "mailru", "rambler", "caldav"])
        exchange = next(p for p in data["providers"] if p["code"] == "exchange")
        self.assertTrue(exchange["server_url"].endswith("/EWS/Exchange.asmx"))
        yandex = next(p for p in data["providers"] if p["code"] == "yandex")
        self.assertEqual(yandex["server_url"], "https://caldav.yandex.ru")
        google = next(p for p in data["providers"] if p["code"] == "google")
        self.assertFalse(google["available"])  # ключи Google в тестах не заданы

    def test_discover_connect_sync_patch_delete(self):
        resp = self.post(reverse("integrations:discover"), self.creds())
        self.assertEqual(resp.status_code, 200, resp.content)
        cals = resp.json()["calendars"]
        work = next(c for c in cals if c["name"] == "Работа")

        self.srv.put(CAL_PATH + "a.ics", remote_ics("a@yandex", "Из Яндекса", self.base))
        resp = self.post(reverse("integrations:calendars"),
                         self.creds(calendar_id=work["id"], calendar_name="Работа", color="green"))
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertEqual(body["sync"]["created_local"], 1)
        cal_id = body["calendar"]["id"]
        self.assertEqual(Event.objects.get(title="Из Яндекса").color, "green")

        # повторное подключение того же календаря
        resp = self.post(reverse("integrations:calendars"), self.creds(calendar_id=work["id"]))
        self.assertEqual(resp.status_code, 400)
        # в discover он помечен как подключённый
        cals = self.post(reverse("integrations:discover"), self.creds()).json()["calendars"]
        self.assertTrue(next(c for c in cals if c["name"] == "Работа")["connected"])

        url = reverse("integrations:calendar-detail", args=[cal_id])
        resp = self.client.patch(url, json.dumps({"direction": "import", "enabled": False}), content_type="application/json")
        self.assertEqual(resp.json()["calendar"]["direction"], "import")

        resp = self.post(reverse("integrations:sync-one", args=[cal_id]), {})
        self.assertEqual(resp.json()["sync"]["summary"], "изменений нет")

        resp = self.client.delete(url + "?remove_events=1")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Event.objects.exists())
        self.assertFalse(ExternalCalendar.objects.exists())

    def test_wrong_password_on_discover(self):
        resp = self.post(reverse("integrations:discover"), self.creds(password="wrong"))
        self.assertEqual(resp.status_code, 400)
        self.assertIn("пароль", resp.json()["errors"]["__all__"][0])

    def test_other_users_calendar_is_not_accessible(self):
        other = make_user("anna")
        cal = ExternalCalendar(user=other, provider="yandex", calendar_id="x", server_url=self.srv.url, username="u")
        cal.credentials = {"password": "p"}
        cal.save()
        self.assertEqual(self.post(reverse("integrations:sync-one", args=[cal.pk]), {}).status_code, 404)
        self.assertEqual(self.client.delete(reverse("integrations:calendar-detail", args=[cal.pk])).status_code, 404)

    def test_user_without_own_calendar_cannot_connect(self):
        self.client.force_login(make_user("nobody", owner=False))
        self.assertEqual(self.client.get(reverse("integrations:calendars")).status_code, 403)

    def test_google_connect_requires_configuration(self):
        self.assertEqual(self.client.get(reverse("integrations:google-connect")).status_code, 400)

    @override_settings(GOOGLE_OAUTH_CLIENT_ID="cid", GOOGLE_OAUTH_CLIENT_SECRET="secret")
    def test_google_oauth_flow(self):
        resp = self.client.get(reverse("integrations:google-connect"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn("accounts.google.com", resp["Location"])
        state = self.client.session["integrations_google_state"]

        # неверный state
        resp = self.client.get(reverse("integrations:google-callback"), {"code": "c", "state": "bad"})
        self.assertIn("integrations=google-error", resp["Location"])

        self.client.get(reverse("integrations:google-connect"))
        state = self.client.session["integrations_google_state"]
        tokens = {"access_token": "at", "refresh_token": "rt", "expires_at": 9e18}
        with mock.patch("integrations.views.google_api.exchange_code", return_value=tokens):
            resp = self.client.get(reverse("integrations:google-callback"), {"code": "c", "state": state})
        self.assertIn("integrations=google", resp["Location"])
        self.assertEqual(self.client.session["integrations_google_tokens"]["refresh_token"], "rt")

        found = [{"id": "primary", "name": "Иван", "color": "#039be5", "read_only": False}]
        with mock.patch("integrations.providers.google.GoogleProvider.list_calendars", return_value=found):
            resp = self.post(reverse("integrations:discover"), {"provider": "google"})
        self.assertEqual(resp.json()["calendars"][0]["id"], "primary")

        with mock.patch("integrations.providers.google.GoogleProvider.list_events", return_value=[]):
            resp = self.post(reverse("integrations:calendars"), {"provider": "google", "calendar_id": "primary", "calendar_name": "Иван"})
        self.assertEqual(resp.status_code, 201, resp.content)
        cal = ExternalCalendar.objects.get(provider="google")
        self.assertEqual(cal.credentials["refresh_token"], "rt")
        self.assertNotIn("integrations_google_tokens", self.client.session)


class InvitationTests(TestCase):
    def setUp(self):
        self.owner = make_user("owner", first_name="Иван", last_name="Петров")
        self.colleague = make_user("anna", email="anna@corp.ru")
        self.client.force_login(self.owner)
        start = timezone.now() + timedelta(days=3)
        self.event = Event.objects.create(
            title="Совещание", start=start, end=start + timedelta(hours=1), owner=self.owner, created_by=self.owner,
            location="Офис", guest_emails="petr@rambler.ru, Anna@corp.ru",
        )
        self.event.invitees.add(self.colleague)

    def test_invitations_sent_individually_with_ics(self):
        resp = self.client.post(reverse("integrations:invite", args=[self.event.pk]), "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(sorted(resp.json()["sent"]), ["anna@corp.ru", "petr@rambler.ru"])  # без повтора anna
        self.assertEqual(len(mail.outbox), 2)
        msg = mail.outbox[0]
        self.assertEqual(len(msg.to), 1)
        self.assertIn("Приглашение: Совещание", msg.subject)
        ics = msg.attachments[0][1]
        self.assertIn("METHOD:REQUEST", ics)
        self.assertIn(f"UID:calendar-event-{self.event.pk}@", ics)
        self.assertIn("ATTENDEE", ics)
        self.assertIn("ORGANIZER", ics)

    def test_cancellation(self):
        self.client.post(reverse("integrations:invite", args=[self.event.pk]), json.dumps({"cancel": True}),
                         content_type="application/json")
        self.assertIn("METHOD:CANCEL", mail.outbox[0].attachments[0][1])
        self.assertIn("Отменено", mail.outbox[0].subject)

    def test_requires_change_permission(self):
        self.client.force_login(self.colleague)  # приглашённый может читать, но не изменять
        resp = self.client.post(reverse("integrations:invite", args=[self.event.pk]), "{}", content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    def test_guest_emails_validated_in_event_api(self):
        url = reverse("events:events-collection")
        payload = {"title": "x", "start": "2026-10-12T10:00", "end": "2026-10-12T11:00", "guest_emails": "ok@mail.ru, не-адрес"}
        resp = self.client.post(url, json.dumps(payload), content_type="application/json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("guest_emails", resp.json()["errors"])
        payload["guest_emails"] = "a@mail.ru; b@rambler.ru\nA@mail.ru"
        resp = self.client.post(url, json.dumps(payload), content_type="application/json")
        self.assertEqual(resp.json()["guest_emails"], ["a@mail.ru", "b@rambler.ru"])


class SyncCommandTests(TestCase):
    def test_command_runs(self):
        from io import StringIO

        from django.core.management import call_command

        with FakeCalDAV() as srv:
            user = make_user("ivan")
            cal = ExternalCalendar(user=user, provider="caldav", server_url=srv.url, username=srv.username,
                                   calendar_id=srv.url + CAL_PATH)
            cal.credentials = {"password": srv.password}
            cal.save()
            srv.put(CAL_PATH + "z.ics", remote_ics("z", "Из команды", timezone.now() + timedelta(days=1)))
            out = StringIO()
            call_command("sync_calendars", stdout=out, stderr=StringIO())
            self.assertIn("к нам: +1", out.getvalue())
            self.assertEqual(SyncedEvent.objects.count(), 1)
