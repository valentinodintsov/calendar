# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Тесты адресной книги (Django): поиск на сервере каталога, доступ всем ролям, настройка только администратору."""

import json
import uuid
from unittest import mock

from django.contrib.auth import get_user_model
from django.core.cache import cache
from django.test import TestCase
from django.urls import reverse

from .ldap_client import DirectoryError, map_entry
from .models import DirectorySource


class FakeClient:
    """
    Подмена DirectoryClient: «сервер каталога» в памяти. search() фильтрует записи по словам запроса,
    как это сделал бы LDAP-фильтр, и считает обращения — чтобы проверить, что поиск идёт на сервер.
    """

    def __init__(self, entries=None, error=None, mapping=None):
        self.entries = entries or []
        self.error = error
        self.calls = []
        self.mapping = mapping

    def search(self, query, limit=20, time_limit=5):
        self.calls.append(query)
        if self.error:
            raise DirectoryError(self.error, network="недоступен" in self.error)
        import re

        tokens = [t.lower().replace("ё", "е") for t in query.split()]
        digits = re.sub(r"\D", "", query)
        out = []
        for dn, attrs in self.entries:
            # objectGUID — двоичный, пропускаем по имени атрибута (не по длине: «+7 912 000-11-22» тоже 16 байт)
            text = " ".join(v.decode("utf-8", "replace") for name, vals in attrs.items() if name.lower() != "objectguid"
                            for v in vals if isinstance(v, bytes)).lower().replace("ё", "е")
            if digits and len(digits) >= 3 and re.fullmatch(r"[\d\s()+\-.]+", query.strip()):
                if digits in re.sub(r"\D", "", text):
                    out.append((dn, attrs))
            elif all(t in text for t in tokens):
                out.append((dn, attrs))
        return out[:limit]

    def test(self, sample_size=5):
        if self.error:
            raise DirectoryError(self.error, network=True, details=["email.yanao.ru → 10.0.0.1", "порт 636: нет ответа"])
        return {"found": min(len(self.entries), sample_size), "samples": [], "attributes": ["mail", "sn"]}


def person(guid, last, first, email, **extra):
    attrs = {"objectGUID": [guid.bytes_le], "sn": [last.encode()], "givenName": [first.encode()],
             "displayName": [f"{last} {first}".encode()], "mail": [email.encode()]}
    attrs.update({k: [v.encode()] for k, v in extra.items()})
    return f"CN={last} {first},CN=yg.loc", attrs


def make_user(username, owner=True, observer=False, admin=False, **extra):
    user = get_user_model().objects.create_user(username, extra.pop("email", f"{username}@example.com"), "x", **extra)
    emp = user.employee
    emp.is_owner, emp.is_observer, emp.is_admin = owner, observer, admin
    emp.save()
    return user


class DirectoryTestCase(TestCase):
    def setUp(self):
        cache.clear()
        # реквизиты из постановки задачи
        self.source = DirectorySource(
            name="YG", kind="ad", host="email.yanao.ru", port=636, base_dn="cn=yg.loc", bind_dn="user@yanao.ru",
        )
        self.source.password = "секрет"
        self.source.save()
        self.entries = [
            person(uuid.uuid4(), "Петров", "Иван", "i.petrov@yanao.ru", title="Начальник отдела", department="Отдел ИТ",
                   telephoneNumber="+7 (34922) 4-12-34"),
            person(uuid.uuid4(), "Петрова", "Анна", "a.petrova@yanao.ru", department="Бухгалтерия"),
            person(uuid.uuid4(), "Сидоров", "Пётр", "p.sidorov@yanao.ru", mobile="+7 912 000-11-22"),
            ("CN=svc,CN=yg.loc", {"objectClass": [b"person"], "description": [b"petrov service"]}),
        ]
        self.fake = FakeClient(self.entries)
        patcher = mock.patch("directory.search.get_client", side_effect=lambda source: self.fake)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.url = reverse("directory:contacts")

    def search(self, q, **params):
        return self.client.get(self.url, {"q": q, **params}).json()


class LiveSearchTests(DirectoryTestCase):
    def setUp(self):
        super().setUp()
        self.client.force_login(make_user("nobody", owner=False))

    def test_search_goes_to_directory_server(self):
        data = self.search("петров")
        self.assertEqual(self.fake.calls, ["петров"])  # запрос ушёл на сервер каталога
        self.assertEqual([r["email"] for r in data["results"]], ["i.petrov@yanao.ru", "a.petrova@yanao.ru"])
        top = data["results"][0]
        self.assertTrue(top["exact"])
        self.assertEqual((top["name"], top["department"], top["source"]), ("Петров Иван", "Отдел ИТ", "YG"))
        self.assertTrue(top["id"].startswith(f"{self.source.pk}:"))

    def test_no_local_copy(self):
        from django.apps import apps

        self.assertFalse(any(m.__name__ == "Contact" for m in apps.get_app_config("directory").get_models()))

    def test_ranking_phone_department_and_yo(self):
        self.assertEqual(self.search("петров иван")["results"][0]["email"], "i.petrov@yanao.ru")
        self.assertEqual([r["email"] for r in self.search("бухгалт")["results"]], ["a.petrova@yanao.ru"])
        self.assertEqual([r["email"] for r in self.search("4-12-34")["results"]], ["i.petrov@yanao.ru"])
        self.assertEqual([r["email"] for r in self.search("9120001122")["results"]], ["p.sidorov@yanao.ru"])
        self.assertEqual(self.search("петр")["results"][0]["email"], "p.sidorov@yanao.ru")  # «Пётр» — полное совпадение

    def test_short_query_does_not_hit_server(self):
        data = self.search("п")
        self.assertEqual(data["results"], [])
        self.assertEqual(data["min_query"], 2)
        data = self.client.get(self.url).json()  # без запроса — только сведения о каталогах
        self.assertTrue(data["configured"])
        self.assertEqual(self.fake.calls, [])

    def test_repeated_query_is_cached(self):
        self.search("петров")
        self.search("Петров")  # тот же запрос с другим регистром
        self.assertEqual(len(self.fake.calls), 1)
        self.search("сидоров")
        self.assertEqual(len(self.fake.calls), 2)

    def test_linked_to_calendar_user_by_email(self):
        user = make_user("ipetrov", email="I.Petrov@yanao.ru")
        res = self.search("петров иван")["results"][0]
        self.assertEqual(res["user_id"], user.pk)
        self.assertIsNone(self.search("сидоров")["results"][0]["user_id"])

    def test_server_error_shown_without_details_to_users(self):
        self.fake.error = "Сервер каталога недоступен: timed out"
        data = self.search("петров")
        self.assertEqual(data["results"], [])
        self.assertEqual(data["errors"], [{"source": "YG", "message": "Каталог временно недоступен."}])
        self.source.refresh_from_db()
        self.assertIn("timed out", self.source.last_error)  # администратор увидит причину
        self.fake.error = None
        cache.clear()
        self.search("петров")
        self.source.refresh_from_db()
        self.assertEqual(self.source.last_error, "")
        self.assertIsNotNone(self.source.last_ok_at)

    def test_two_directories_merged_without_duplicates(self):
        second = DirectorySource(name="Второй", kind="ldap", host="ldap2", base_dn="dc=x")
        second.save()
        other = FakeClient([person(uuid.uuid4(), "Петров", "Иван", "i.petrov@yanao.ru"),
                            person(uuid.uuid4(), "Петровский", "Олег", "o.p@x.ru")])
        with mock.patch("directory.search.get_client", side_effect=lambda s: other if s.pk == second.pk else self.fake):
            emails = [r["email"] for r in self.search("петров")["results"]]
        self.assertEqual(sorted(emails), ["a.petrova@yanao.ru", "i.petrov@yanao.ru", "o.p@x.ru"])

    def test_disabled_source_not_searched(self):
        self.source.enabled = False
        self.source.save()
        data = self.search("петров")
        self.assertFalse(data["configured"])
        self.assertEqual(data["results"], [])
        self.assertEqual(self.fake.calls, [])


class AccessTests(DirectoryTestCase):
    def test_available_to_every_role(self):
        users = [
            make_user("owner"),
            make_user("observer", owner=False, observer=True),
            make_user("nobody", owner=False),  # без единой роли
            make_user("admin", owner=False, admin=True),
        ]
        for user in users:
            self.client.force_login(user)
            resp = self.client.get(self.url, {"q": "петров"})
            self.assertEqual(resp.status_code, 200, user.username)
            self.assertEqual(resp.json()["total"], 2, user.username)

    def test_anonymous_denied(self):
        self.assertEqual(self.client.get(self.url, {"q": "петров"}).status_code, 401)
        self.assertEqual(self.fake.calls, [])

    def test_meta_hides_connection_details_from_non_admins(self):
        self.client.force_login(make_user("nobody", owner=False))
        src = self.client.get(self.url).json()["sources"][0]
        for key in ("host", "bind_dn", "base_dn", "last_error"):
            self.assertNotIn(key, src)
        self.client.force_login(make_user("admin", owner=False, admin=True))
        data = self.client.get(self.url).json()
        self.assertTrue(data["can_manage"])
        self.assertEqual(data["sources"][0]["host"], "email.yanao.ru")
        self.assertNotIn("секрет", json.dumps(data, ensure_ascii=False))


class AdminOnlyTests(DirectoryTestCase):
    def test_management_api_admin_only(self):
        urls = [("get", reverse("directory:sources")), ("post", reverse("directory:source-test", args=[self.source.pk]))]
        for i, role in enumerate((dict(owner=True), dict(owner=False, observer=True), dict(owner=False))):
            self.client.force_login(make_user(f"user{i}", **role))
            for method, url in urls:
                self.assertEqual(getattr(self.client, method)(url).status_code, 403, url)
            self.assertNotEqual(self.client.get(reverse("admin:directory_directorysource_changelist")).status_code, 200)

    def test_admin_test_connection_with_diagnostics(self):
        self.client.force_login(make_user("admin", owner=False, admin=True))
        with mock.patch("directory.views.get_client", return_value=self.fake):
            res = self.client.post(reverse("directory:source-test", args=[self.source.pk])).json()
        self.assertTrue(res["ok"])
        broken = FakeClient(error="Сервер каталога недоступен: timed out")
        with mock.patch("directory.views.get_client", return_value=broken):
            res = self.client.post(reverse("directory:source-test", args=[self.source.pk])).json()
        self.assertFalse(res["ok"])
        self.assertTrue(res["network"])
        self.assertIn("порт 636", res["details"][1])

    def _form(self, **extra):
        form = {
            "name": "YG", "kind": "ad", "host": "email.yanao.ru", "port": "636", "base_dn": "cn=yg.loc",
            "bind_dn": "user@yanao.ru", "password": "", "enabled": "on", "security": "auto",
            "verify_certificate": "on", "ca_cert_file": "", "search_filter": "", "attribute_map": "{}",
            "page_size": "500", "max_results": "50", "probe_query": "",
        }
        form.update(extra)
        return form

    def test_admin_form_keeps_password_when_blank(self):
        self.client.force_login(make_user("admin", owner=False, admin=True))
        url = reverse("admin:directory_directorysource_change", args=[self.source.pk])
        resp = self.client.post(url, self._form())
        self.assertEqual(resp.status_code, 302, resp.content.decode()[:3000])
        self.source.refresh_from_db()
        self.assertEqual(self.source.password, "секрет")
        self.client.post(url, self._form(password="новый"))
        self.source.refresh_from_db()
        self.assertEqual(self.source.password, "новый")
        self.assertEqual(self.client.post(url, self._form(attribute_map='{"unknown_field": "x"}')).status_code, 200)

    def test_admin_probe_search_button(self):
        self.client.force_login(make_user("admin", owner=False, admin=True))
        url = reverse("admin:directory_directorysource_change", args=[self.source.pk])
        with mock.patch("directory.admin.get_client", return_value=self.fake):
            resp = self.client.post(url, self._form(probe_query="петров", _search="1"), follow=True)
        text = " ".join(str(m) for m in resp.context["messages"])
        self.assertIn("найдено на сервере", text)
        self.assertIn("Петров Иван", text)
        self.assertEqual(self.fake.calls, ["петров"])

    def test_mapping_used_for_results(self):
        mapped = map_entry(*self.entries[0], self.source.connection_settings().effective_mapping())
        self.assertEqual(mapped["department"], "Отдел ИТ")
