# © 2026 Одинцов Валентин Николаевич. Все права защищены.
import json
import shutil
import tempfile
from datetime import datetime

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from events.models import Event

from .models import Delegation, Employee, Role
from .permissions import Access, get_roles


def aware(*args):
    return timezone.make_aware(datetime(*args))


def make_user(username, owner=True, observer=False, admin=False, **extra):
    email = extra.pop("email", f"{username}@example.com")
    user = get_user_model().objects.create_user(username, email, "x", **extra)
    emp = user.employee  # создан сигналом
    emp.is_owner, emp.is_observer, emp.is_admin = owner, observer, admin
    emp.save()
    user.refresh_from_db()
    return user


class RoleTestCase(TestCase):
    def setUp(self):
        self.owner = make_user("owner")  # руководитель
        self.manager = make_user("manager")  # станет менеджером через делегирование
        self.manager2 = make_user("manager2")
        self.observer = make_user("observer", owner=False, observer=True)  # только наблюдатель
        self.admin = make_user("admin", owner=False, admin=True)  # только администратор
        self.nobody = make_user("nobody", owner=False)  # без ролей
        self.event = Event.objects.create(
            title="Совет директоров",
            description="секретная повестка",
            location="Переговорная 1",
            start=aware(2026, 10, 12, 10),
            end=aware(2026, 10, 12, 11),
            owner=self.owner,
        )
        self.list_url = reverse("events:events-collection")
        self.detail_url = reverse("events:event-detail", args=[self.event.pk])

    def as_user(self, user):
        self.client.force_login(user)

    def delegate(self, manager=None, event=None, owner=None, **flags):
        return Delegation.objects.create(
            owner=owner or self.owner, manager=manager or self.manager, event=event, **flags
        )

    def list_events(self, **params):
        params = {"start": "2026-10-01", "end": "2026-11-01", **params}
        return self.client.get(self.list_url, params).json()["events"]

    def put(self, url=None, **changes):
        payload = {"title": "Новое название", "start": "2026-10-12T10:00", "end": "2026-10-12T11:00", **changes}
        return self.client.put(url or self.detail_url, json.dumps(payload), content_type="application/json")

    def post(self, **payload):
        payload = {"title": "Новое", "start": "2026-10-13T10:00", "end": "2026-10-13T11:00", **payload}
        return self.client.post(self.list_url, json.dumps(payload), content_type="application/json")

    def tmp_media(self):
        path = tempfile.mkdtemp(prefix="calendar-roles-media-")
        self.addCleanup(shutil.rmtree, path, True)
        return path


class ProfileTests(RoleTestCase):
    def test_new_user_is_owner_of_own_calendar(self):
        user = get_user_model().objects.create_user("new", "new@example.com", "x")
        emp = user.employee
        self.assertEqual((emp.is_owner, emp.is_observer, emp.is_admin), (True, False, False))
        self.assertEqual(get_roles(user), {Role.OWNER})

    def test_superuser_is_admin(self):
        root = get_user_model().objects.create_superuser("root", "root@example.com", "x")
        self.assertTrue(root.employee.is_admin)
        self.assertIn(Role.ADMIN, get_roles(root))

    def test_admin_flag_grants_and_revokes_staff(self):
        self.assertTrue(self.admin.is_staff)
        emp = Employee.objects.get(user=self.admin)
        emp.is_admin = False
        emp.save()
        self.admin.refresh_from_db()
        self.assertFalse(self.admin.is_staff)

    def test_manager_role_comes_from_delegation(self):
        self.assertNotIn(Role.MANAGER, get_roles(self.manager))
        self.delegate(can_read=True)
        self.assertIn(Role.MANAGER, get_roles(self.manager))
        self.assertIn(Role.MANAGER, self.manager.employee.roles())


class CombinedRolesTests(RoleTestCase):
    """Один сотрудник: владелец своего календаря, менеджер руководителя, наблюдатель и администратор."""

    def test_all_roles_at_once(self):
        user = make_user("assistant", owner=True, observer=True, admin=True)
        self.delegate(manager=user, can_create=True, can_read=True)
        access = Access(user)
        self.assertEqual(access.roles, {Role.OWNER, Role.MANAGER, Role.OBSERVER, Role.ADMIN})
        codes = [r["code"] for r in access.me()["roles"]]
        self.assertEqual(codes, ["owner", "manager", "observer", "admin"])

    def test_owner_and_manager_rights_add_up(self):
        self.delegate(can_create=True, can_read=True)
        self.as_user(self.manager)
        own = self.post()  # свой календарь — как владелец
        self.assertEqual(own.status_code, 201, own.content)
        self.assertEqual(own.json()["owner"]["id"], self.manager.pk)
        boss = self.post(owner=self.owner.pk)  # календарь руководителя — как менеджер
        self.assertEqual(boss.status_code, 201, boss.content)
        self.assertEqual(boss.json()["owner"]["id"], self.owner.pk)
        # своё + событие руководителя + созданное у руководителя
        self.assertEqual(len(self.list_events()), 3)
        # чтение без изменения в календаре руководителя
        self.assertEqual(self.put().status_code, 403)
        owners = {o["id"] for o in Access(self.manager).me()["creatable_owners"]}
        self.assertEqual(owners, {self.manager.pk, self.owner.pk})

    def test_manager_of_several_calendars(self):
        boss2 = make_user("boss2")
        other = Event.objects.create(title="Планёрка", start=aware(2026, 10, 15, 9), owner=boss2)
        self.delegate(can_read=True)
        self.delegate(owner=boss2, can_change=True)
        self.as_user(self.manager)
        self.assertEqual({e["id"] for e in self.list_events()}, {self.event.pk, other.pk})
        self.assertEqual(self.put().status_code, 403)
        self.assertEqual(self.put(url=reverse("events:event-detail", args=[other.pk])).status_code, 200)

    def test_owner_plus_observer(self):
        user = make_user("watcher", owner=True, observer=True)
        self.as_user(user)
        self.assertEqual(self.put().status_code, 200)  # наблюдатель изменяет чужое
        self.assertEqual(self.post().status_code, 201)  # владелец создаёт в своём
        self.assertEqual(self.post(owner=self.owner.pk).status_code, 403)  # но не в чужом


class OwnerTests(RoleTestCase):
    def test_owner_has_full_access_to_own_calendar(self):
        self.as_user(self.owner)
        [event] = self.list_events()
        self.assertEqual(event["permissions"], {"read": True, "change": True})
        self.assertEqual(event["description"], "секретная повестка")
        self.assertEqual(self.put().status_code, 200)
        resp = self.post()
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["owner"]["id"], self.owner.pk)

    def test_owner_cannot_touch_other_calendars(self):
        self.as_user(self.manager2)
        self.assertEqual(self.list_events(), [])
        self.assertEqual(self.client.get(self.detail_url).status_code, 404)
        self.assertEqual(self.put().status_code, 404)
        self.assertEqual(self.post(owner=self.owner.pk).status_code, 403)

    def test_owner_cannot_change_event_owner(self):
        self.as_user(self.owner)
        self.assertEqual(self.put(owner=self.manager.pk).status_code, 403)

    def test_user_without_owner_role_cannot_create(self):
        self.as_user(self.nobody)
        self.assertEqual(self.post().status_code, 403)
        self.assertFalse(Access(self.nobody).me()["can_create"])


class ManagerTests(RoleTestCase):
    def test_read_only(self):
        self.delegate(can_read=True)
        self.as_user(self.manager)
        [event] = self.list_events()
        self.assertEqual(event["permissions"], {"read": True, "change": False})
        self.assertEqual(self.put().status_code, 403)
        self.assertEqual(self.client.delete(self.detail_url).status_code, 403)
        self.assertEqual(self.post(owner=self.owner.pk).status_code, 403)

    def test_change_allows_edit_move_delete(self):
        self.delegate(can_change=True)
        self.as_user(self.manager)
        self.assertEqual(self.put().status_code, 200)
        resp = self.client.patch(
            self.detail_url,
            json.dumps({"start": "2026-10-14T10:00", "end": "2026-10-14T11:00"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["owner"]["id"], self.owner.pk)
        self.assertEqual(self.post(owner=self.owner.pk).status_code, 403)
        self.assertEqual(self.client.delete(self.detail_url).status_code, 200)

    def test_create_only(self):
        self.delegate(can_create=True)
        self.as_user(self.manager)
        resp = self.post(owner=self.owner.pk)
        self.assertEqual(resp.status_code, 201, resp.content)
        created = resp.json()
        self.assertEqual(created["permissions"], {"read": False, "change": False})
        self.assertEqual(self.put().status_code, 404)
        with self.settings(MEDIA_ROOT=self.tmp_media()):
            # автор может прикрепить файлы к своему новому событию
            resp = self.client.post(
                reverse("events:attachment-upload", args=[created["id"]]),
                {"files": [SimpleUploadedFile("a.txt", b"x")]},
            )
            self.assertEqual(resp.status_code, 201, resp.content)
            # а к чужим событиям руководителя — нет
            resp = self.client.post(
                reverse("events:attachment-upload", args=[self.event.pk]),
                {"files": [SimpleUploadedFile("b.txt", b"y")]},
            )
            self.assertEqual(resp.status_code, 404)

    def test_event_level_delegation(self):
        other = Event.objects.create(title="Другое", start=aware(2026, 10, 15, 9), owner=self.owner)
        self.delegate(event=self.event, can_change=True)
        self.as_user(self.manager)
        self.assertEqual([e["id"] for e in self.list_events()], [self.event.pk])
        self.assertEqual(self.put().status_code, 200)
        self.assertEqual(self.client.get(reverse("events:event-detail", args=[other.pk])).status_code, 404)

    def test_calendar_managers_setting_reads_all_calendars(self):
        with self.settings(AD_AUTH_DOMAIN="zs.int", CALENDAR_MANAGERS="Manager2@zs.int"):
            self.assertIn(Role.MANAGER, get_roles(self.manager2))
            self.assertIn(Role.MANAGER, self.manager2.employee.roles())
            self.as_user(self.manager2)
            [event] = self.list_events()
            self.assertEqual(event["permissions"], {"read": True, "change": False})
            self.assertEqual(self.put().status_code, 403)
            self.assertEqual(self.client.delete(self.detail_url).status_code, 403)
            self.assertEqual(self.post(owner=self.owner.pk).status_code, 403)
        self.assertNotIn(Role.MANAGER, get_roles(self.manager2))

    def test_invited_user_can_read(self):
        self.event.invitees.add(self.manager2)
        self.as_user(self.manager2)
        [event] = self.list_events()
        self.assertEqual(event["permissions"], {"read": True, "change": False})


class ObserverTests(RoleTestCase):
    def test_observer_reads_and_changes_all_but_cannot_create(self):
        self.as_user(self.observer)
        [event] = self.list_events()
        self.assertEqual(event["permissions"], {"read": True, "change": True})
        self.assertEqual(self.put().status_code, 200)
        self.assertEqual(self.post().status_code, 403)
        self.assertEqual(self.post(owner=self.owner.pk).status_code, 403)
        self.assertFalse(Access(self.observer).me()["can_create"])
        self.assertFalse(Access(self.observer).can_delegate)


class AdminTests(RoleTestCase):
    def test_admin_full_access_and_owner_change(self):
        self.as_user(self.admin)
        [event] = self.list_events()
        self.assertEqual(event["permissions"], {"read": True, "change": True})
        resp = self.put(owner=self.manager.pk)
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["owner"]["id"], self.manager.pk)
        self.assertEqual(self.post(owner=self.owner.pk).status_code, 201)

    def test_admin_manages_roles_in_admin_site(self):
        url = reverse("admin:models_employee_employee_changelist")
        self.as_user(self.admin)
        self.assertEqual(self.client.get(url).status_code, 200)
        self.as_user(self.owner)
        self.assertNotEqual(self.client.get(url).status_code, 200)


class DelegationModelTests(RoleTestCase):
    def test_change_implies_read(self):
        d = self.delegate(can_change=True)
        self.assertTrue(d.can_read)
        self.assertFalse(d.can_create)

    def test_clean_validates(self):
        with self.assertRaises(ValidationError):  # у делегирующего нет своего календаря
            Delegation(owner=self.nobody, manager=self.manager, can_read=True).full_clean()
        with self.assertRaises(ValidationError):  # «Запись» — только на весь календарь
            Delegation(owner=self.owner, manager=self.manager, event=self.event, can_create=True).full_clean()
        # менеджером может стать любой сотрудник, в том числе наблюдатель
        Delegation(owner=self.owner, manager=self.observer, can_read=True).full_clean()


class DelegationApiTests(RoleTestCase):
    url = reverse("models_employee:delegations")

    def post_delegation(self, **data):
        return self.client.post(self.url, json.dumps(data), content_type="application/json")

    def test_owner_lists_and_sets_rights(self):
        self.as_user(self.owner)
        self.assertEqual(self.client.get(self.url).json()["delegations"], [])

        resp = self.post_delegation(manager=self.manager.pk, can_change=True)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        self.assertEqual((body["can_create"], body["can_read"], body["can_change"]), (False, True, True))

        self.post_delegation(manager=self.manager.pk, can_create=True, can_read=True)
        d = Delegation.objects.get(manager=self.manager)
        self.assertEqual((d.can_create, d.can_read, d.can_change), (True, True, False))
        self.assertEqual(Delegation.objects.count(), 1)

        [row] = self.client.get(self.url).json()["delegations"]
        self.assertEqual(row["user"]["id"], self.manager.pk)
        self.assertEqual(row["user"]["username"], "manager")

        self.post_delegation(manager=self.manager.pk)  # все флаги false — права отозваны
        self.assertFalse(Delegation.objects.exists())

    def test_cannot_delegate_to_self(self):
        self.as_user(self.owner)
        self.assertEqual(self.post_delegation(manager=self.owner.pk, can_read=True).status_code, 400)

    def test_without_own_calendar_cannot_delegate(self):
        for user in (self.observer, self.nobody):
            self.as_user(user)
            self.assertEqual(self.client.get(self.url).status_code, 403)


class UserAdminInlineTests(TestCase):
    """Регрессия: создание пользователя в админке вместе с ролями не должно падать с IntegrityError."""

    def test_add_user_with_roles_in_admin(self):
        root = get_user_model().objects.create_superuser("root", "root@example.com", "x")
        self.client.force_login(root)
        resp = self.client.post(
            reverse("admin:auth_user_add"),
            {
                "username": "newbie",
                "usable_password": "true",
                "password1": "Sup3r-Secret-pass",
                "password2": "Sup3r-Secret-pass",
                "employee-TOTAL_FORMS": "1",
                "employee-INITIAL_FORMS": "0",
                "employee-MIN_NUM_FORMS": "0",
                "employee-MAX_NUM_FORMS": "1",
                "employee-0-position": "Секретарь",
                "employee-0-is_owner": "on",
                "employee-0-is_observer": "on",
            },
        )
        self.assertEqual(resp.status_code, 302, resp.content.decode()[:2000])
        user = get_user_model().objects.get(username="newbie")
        self.assertEqual(Employee.objects.filter(user=user).count(), 1)
        emp = user.employee
        self.assertEqual((emp.is_owner, emp.is_observer, emp.is_admin), (True, True, False))
        self.assertEqual(emp.position, "Секретарь")


class CandidateSearchTests(RoleTestCase):
    url = reverse("models_employee:delegation-candidates")

    def setUp(self):
        super().setUp()
        self.ivan = make_user("ipetrov", first_name="Иван", last_name="Петров", email="ivan.petrov@corp.ru")
        self.petr = make_user("pivanov", first_name="Пётр", last_name="Иванов", email="p.ivanov@corp.ru")
        self.anna = make_user("apetrova", first_name="Анна", last_name="Петрова", email="anna@corp.ru")

    def search(self, q):
        resp = self.client.get(self.url, {"q": q})
        self.assertEqual(resp.status_code, 200, resp.content)
        return resp.json()["results"]

    def test_search_by_last_name_first_name_and_email(self):
        self.as_user(self.owner)
        self.assertEqual([r["id"] for r in self.search("петров")][:2], [self.ivan.pk, self.anna.pk])
        self.assertTrue(self.search("петров")[0]["exact"])
        self.assertEqual(self.search("Петров Иван")[0]["id"], self.ivan.pk)
        self.assertEqual(self.search("иван петров")[0]["id"], self.ivan.pk)  # имя и фамилия в любом порядке
        self.assertEqual(self.search("ANNA@CORP.RU")[0]["id"], self.anna.pk)
        self.assertEqual(self.search("ivan.pet")[0]["id"], self.ivan.pk)

    def test_suggestions_narrow_while_typing(self):
        self.as_user(self.owner)
        ids = lambda q: {r["id"] for r in self.search(q)}  # noqa: E731
        self.assertTrue({self.ivan.pk, self.anna.pk} <= ids("пет"))
        self.assertEqual(ids("петрова"), {self.anna.pk})
        self.assertEqual(self.search("петрова")[0]["exact"], True)

    def test_yo_equals_ye_and_exact_first(self):
        self.as_user(self.owner)
        results = self.search("петр")
        self.assertEqual(results[0]["id"], self.petr.pk)  # точное совпадение имени «Пётр»
        self.assertTrue(results[0]["exact"])

    def test_excludes_self_and_empty_query(self):
        self.as_user(self.ivan)
        self.assertNotIn(self.ivan.pk, [r["id"] for r in self.search("петров")])
        self.assertEqual(self.search(""), [])
        self.assertEqual(self.search("нет-такого"), [])

    def test_requires_delegate_right(self):
        self.as_user(self.nobody)
        self.assertEqual(self.client.get(self.url, {"q": "пет"}).status_code, 403)
