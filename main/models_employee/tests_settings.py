# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Личные настройки (шестерёнка): у каждого пользователя свои."""

import json

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse


class MySettingsTests(TestCase):
    def setUp(self):
        User = get_user_model()
        self.ivan = User.objects.create_user("ivan", "ivan@yanao.ru", "x")
        self.anna = User.objects.create_user("anna", "anna@yanao.ru", "x")
        self.url = reverse("models_employee:my-settings")

    def patch(self, data):
        return self.client.patch(self.url, json.dumps(data), content_type="application/json")

    def test_defaults(self):
        self.client.force_login(self.ivan)
        self.assertEqual(self.client.get(self.url).json()["settings"],
                         {"theme": "system", "start_view": "last", "last_view": "month"})

    def test_each_user_has_own_settings(self):
        self.client.force_login(self.ivan)
        resp = self.patch({"theme": "dark", "start_view": "week"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.patch({"last_view": "day"})
        self.assertEqual(self.client.get(self.url).json()["settings"],
                         {"theme": "dark", "start_view": "week", "last_view": "day"})

        self.client.force_login(self.anna)  # у Анны — свои, нетронутые
        self.assertEqual(self.client.get(self.url).json()["settings"]["theme"], "system")
        self.patch({"theme": "light"})
        self.client.force_login(self.ivan)
        self.assertEqual(self.client.get(self.url).json()["settings"]["theme"], "dark")

    def test_page_uses_users_settings(self):
        self.client.force_login(self.ivan)
        self.patch({"theme": "dark", "start_view": "day"})
        resp = self.client.get(reverse("events:calendar"))
        self.assertContains(resp, 'data-theme="dark"')
        self.assertEqual(resp.context["me"]["settings"]["start_view"], "day")
        self.client.force_login(self.anna)
        self.assertNotContains(self.client.get(reverse("events:calendar")), 'data-theme="dark"')

    def test_validation_and_auth(self):
        self.client.force_login(self.ivan)
        self.assertEqual(self.patch({"theme": "pink"}).status_code, 400)
        self.assertEqual(self.patch({"unknown": 1}).status_code, 400)
        self.assertEqual(self.client.get(self.url).json()["settings"]["theme"], "system")
        self.client.logout()
        self.assertEqual(self.client.get(self.url).status_code, 401)
