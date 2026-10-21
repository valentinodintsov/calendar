# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Форма события: поле «Участники» (вместо «Приглашённые» и «Гости») и напоминание."""

import json
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from .models import Event


class ParticipantsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.ivan = User.objects.create_user("ivan", "ivan@yanao.ru", "x")
        self.anna = User.objects.create_user("anna", "Anna@yanao.ru", "x", first_name="Анна", last_name="Петрова")
        self.client.force_login(self.ivan)
        self.url = reverse("events:events-collection")

    def create(self, **extra):
        payload = {"title": "Тест", "start": "2026-10-20T10:00", "end": "2026-10-20T11:00", **extra}
        return self.client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_participants_become_invitees_by_email(self):
        resp = self.create(
            guest_emails=["anna@yanao.ru", "petrov@mail.ru", "ivan@yanao.ru"],
            directory_guests=["anna@yanao.ru"],
            participant_names={"anna@yanao.ru": "Петрова Анна", "petrov@mail.ru": "Пётр", "chuzhoy@x.ru": "Лишний"},
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()
        self.assertEqual([(u["id"], u["email"]) for u in data["invitees"]], [(self.anna.pk, "Anna@yanao.ru")])  # без владельца
        self.assertEqual(data["participant_names"], {"anna@yanao.ru": "Петрова Анна", "petrov@mail.ru": "Пётр"})
        self.assertEqual(data["directory_guests"], ["anna@yanao.ru"])
        # Анна видит событие у себя
        self.client.force_login(self.anna)
        self.assertEqual(self.client.get(reverse("events:event-detail", args=[data["id"]])).status_code, 200)

    def test_removing_participant_removes_invitee(self):
        ev = self.create(guest_emails=["anna@yanao.ru"]).json()
        payload = {"title": "Тест", "start": "2026-10-20T10:00", "end": "2026-10-20T11:00", "guest_emails": []}
        resp = self.client.put(reverse("events:event-detail", args=[ev["id"]]), json.dumps(payload), content_type="application/json")
        self.assertEqual(resp.json()["invitees"], [])

    def test_reminder(self):
        ev = self.create(reminder_minutes=15).json()
        self.assertEqual(ev["reminder_minutes"], 15)
        detail = reverse("events:event-detail", args=[ev["id"]])
        # перенос (PATCH) и старые клиенты без поля не сбрасывают напоминание
        resp = self.client.patch(detail, json.dumps({"start": "2026-10-21T10:00", "end": "2026-10-21T11:00"}),
                                 content_type="application/json")
        self.assertEqual(resp.json()["reminder_minutes"], 15)
        resp = self.client.put(detail, json.dumps({"title": "Тест", "start": "2026-10-21T10:00", "end": "2026-10-21T11:00"}),
                               content_type="application/json")
        self.assertEqual(resp.json()["reminder_minutes"], 15)
        resp = self.client.patch(detail, json.dumps({"reminder_minutes": None}), content_type="application/json")
        self.assertIsNone(resp.json()["reminder_minutes"])
        self.assertEqual(self.client.patch(detail, json.dumps({"reminder_minutes": 7}), content_type="application/json").status_code, 400)

    def test_reminders_api(self):
        now = timezone.now()
        soon = Event.objects.create(title="Скоро", start=now + timedelta(minutes=10), end=now + timedelta(minutes=70),
                                    owner=self.ivan, reminder_minutes=15)
        Event.objects.create(title="Без напоминания", start=now + timedelta(minutes=10), owner=self.ivan)
        Event.objects.create(title="Завтра", start=now + timedelta(days=1), owner=self.ivan, reminder_minutes=15)
        guest = Event.objects.create(title="Меня позвали", start=now + timedelta(minutes=5), owner=self.anna, reminder_minutes=5)
        guest.invitees.add(self.ivan)
        Event.objects.create(title="Чужое", start=now + timedelta(minutes=5), owner=self.anna, reminder_minutes=5)
        data = self.client.get(reverse("events:reminders")).json()
        self.assertEqual(sorted(r["title"] for r in data["reminders"]), ["Меня позвали", "Скоро"])
        self.assertEqual(next(r for r in data["reminders"] if r["id"] == soon.pk)["minutes"], 15)
