import json
import os
import shutil
import tempfile
from datetime import datetime
from urllib.parse import quote
from xml.etree import ElementTree

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from django.utils import timezone

from .models import Attachment, Event


def aware(*args):
    return timezone.make_aware(datetime(*args))


def login_admin(client):
    """Тесты API работают от имени суперпользователя (роль «Администратор»)."""
    admin = get_user_model().objects.create_superuser("root", "root@example.com", "x")
    client.force_login(admin)
    return admin


class CalendarPageTests(TestCase):
    def test_anonymous_redirected_to_login(self):
        resp = self.client.get(reverse("events:calendar"))
        self.assertEqual(resp.status_code, 302)
        self.assertIn(reverse("login"), resp["Location"])
        self.assertEqual(self.client.get(reverse("events:events-collection")).status_code, 401)

    def test_login_page_renders(self):
        self.assertContains(self.client.get(reverse("login")), 'name="password"')

    def test_page_renders_and_sets_csrf_cookie(self):
        login_admin(self.client)
        resp = self.client.get(reverse("events:calendar"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="grid"')
        self.assertContains(resp, "© Валентин Одинцов, 2026")
        # адреса скрипта и стилей меняются вместе с файлами — браузер не оставит старый скрипт из кэша
        self.assertRegex(resp.content.decode(), r'events/calendar\.js\?v=[1-9]\d+"')
        self.assertRegex(resp.content.decode(), r'events/calendar\.css\?v=[1-9]\d+"')
        self.assertIn("csrftoken", resp.cookies)


class EventApiTests(TestCase):
    def setUp(self):
        self.admin = login_admin(self.client)
        self.url = reverse("events:events-collection")

    def post(self, payload):
        return self.client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_create_event(self):
        resp = self.post(
            {"title": "Встреча", "start": "2026-10-12T10:00", "end": "2026-10-12T11:30", "color": "green"}
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()
        self.assertEqual(data["title"], "Встреча")
        self.assertEqual(data["start"], "2026-10-12T10:00")
        self.assertEqual(data["end"], "2026-10-12T11:30")
        self.assertFalse(data["all_day"])
        self.assertEqual(Event.objects.count(), 1)

    def test_create_all_day(self):
        resp = self.post({"title": "Отпуск", "start": "2026-10-20T00:00", "end": "2026-10-24T23:59", "all_day": True})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertTrue(resp.json()["all_day"])

    def test_validation_errors(self):
        resp = self.post({"title": "  ", "start": "2026-10-12T10:00"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("title", resp.json()["errors"])

        resp = self.post({"title": "Назад во времени", "start": "2026-10-12T10:00", "end": "2026-10-12T09:00"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("end", resp.json()["errors"])

    def test_bad_json(self):
        resp = self.client.post(self.url, "{oops", content_type="application/json")
        self.assertEqual(resp.status_code, 400)

    def test_range_filter_includes_overlapping_events(self):
        inside = Event.objects.create(title="Внутри", start=aware(2026, 10, 5, 9), end=aware(2026, 10, 5, 10))
        spanning = Event.objects.create(title="Через границу", start=aware(2026, 9, 28, 9), end=aware(2026, 10, 2, 18))
        no_end = Event.objects.create(title="Без конца", start=aware(2026, 10, 7, 12))
        Event.objects.create(title="Раньше", start=aware(2026, 9, 1, 9), end=aware(2026, 9, 1, 10))
        Event.objects.create(title="Позже", start=aware(2026, 11, 1, 9), end=aware(2026, 11, 1, 10))

        resp = self.client.get(self.url, {"start": "2026-10-01", "end": "2026-11-01"})
        ids = {e["id"] for e in resp.json()["events"]}
        self.assertEqual(ids, {inside.id, spanning.id, no_end.id})

    def test_search(self):
        Event.objects.create(title="Йога", start=aware(2026, 10, 5, 9))
        Event.objects.create(title="Спортзал", location="Йога-студия", start=aware(2026, 10, 6, 9))
        Event.objects.create(title="Обед", start=aware(2026, 10, 7, 9))
        resp = self.client.get(self.url, {"q": "йога"})
        self.assertEqual({e["title"] for e in resp.json()["events"]}, {"Йога", "Спортзал"})
        resp = self.client.get(self.url, {"q": "ОБЕД"})
        self.assertEqual([e["title"] for e in resp.json()["events"]], ["Обед"])

    def test_search_text_updates_on_edit(self):
        ev = Event.objects.create(title="Старое", start=aware(2026, 10, 5, 9))
        self.client.put(
            reverse("events:event-detail", args=[ev.pk]),
            json.dumps({"title": "Стоматолог", "start": "2026-10-05T09:00"}),
            content_type="application/json",
        )
        titles = [e["title"] for e in self.client.get(self.url, {"q": "СТОМАТ"}).json()["events"]]
        self.assertEqual(titles, ["Стоматолог"])
        self.assertEqual(self.client.get(self.url, {"q": "старое"}).json()["events"], [])

    def test_update_patch_delete(self):
        ev = Event.objects.create(title="Старое", start=aware(2026, 10, 5, 9), end=aware(2026, 10, 5, 10))
        url = reverse("events:event-detail", args=[ev.pk])

        resp = self.client.put(
            url,
            json.dumps({"title": "Новое", "start": "2026-10-06T14:00", "end": "2026-10-06T15:00", "color": "red"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(resp.json()["title"], "Новое")

        resp = self.client.patch(
            url, json.dumps({"start": "2026-10-08T14:00", "end": "2026-10-08T15:00"}), content_type="application/json"
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()
        self.assertEqual(data["start"], "2026-10-08T14:00")
        self.assertEqual(data["title"], "Новое")
        self.assertEqual(data["color"], "red")

        resp = self.client.delete(url)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(Event.objects.exists())
        self.assertEqual(self.client.get(url).status_code, 404)


TEMP_MEDIA = tempfile.mkdtemp(prefix="calendar-test-media-")


@override_settings(MEDIA_ROOT=TEMP_MEDIA)
class EventExtrasTests(TestCase):
    @classmethod
    def tearDownClass(cls):
        super().tearDownClass()
        shutil.rmtree(TEMP_MEDIA, ignore_errors=True)

    def setUp(self):
        User = get_user_model()
        self.anna = User.objects.create_user("anna", "anna@example.com", "x", first_name="Анна", last_name="Петрова")
        self.ivan = User.objects.create_user("ivan", "ivan@example.com", "x")
        self.admin = login_admin(self.client)
        self.url = reverse("events:events-collection")

    def create(self, **extra):
        payload = {"title": "Планёрка", "start": "2026-10-12T10:00", "end": "2026-10-12T11:00", **extra}
        return self.client.post(self.url, json.dumps(payload), content_type="application/json")

    def test_page_lists_users_for_invite(self):
        resp = self.client.get(reverse("events:calendar"))
        self.assertContains(resp, 'id="users-data"')
        # json_script экранирует кириллицу (\uXXXX), поэтому проверяем данные шаблона
        names = {u["name"] for u in resp.context["users"]}
        self.assertEqual(names, {"Анна Петрова", "ivan", "root"})

    def test_url_and_invitees(self):
        resp = self.create(url="example.com/meet", invitees=[self.anna.pk, self.ivan.pk])
        self.assertEqual(resp.status_code, 201, resp.content)
        data = resp.json()
        self.assertEqual(data["url"], "https://example.com/meet")
        self.assertEqual({u["name"] for u in data["invitees"]}, {"Анна Петрова", "ivan"})

    def test_invalid_url_and_unknown_invitee(self):
        resp = self.create(url="not a url", invitees=[999999])
        self.assertEqual(resp.status_code, 400)
        errors = resp.json()["errors"]
        self.assertIn("url", errors)
        self.assertIn("invitees", errors)

    def test_patch_keeps_invitees(self):
        ev_id = self.create(invitees=[self.anna.pk]).json()["id"]
        resp = self.client.patch(
            reverse("events:event-detail", args=[ev_id]),
            json.dumps({"start": "2026-10-13T10:00", "end": "2026-10-13T11:00"}),
            content_type="application/json",
        )
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual([u["id"] for u in resp.json()["invitees"]], [self.anna.pk])

    def test_attachments_upload_download_delete(self):
        ev_id = self.create().json()["id"]
        upload_url = reverse("events:attachment-upload", args=[ev_id])
        resp = self.client.post(
            upload_url,
            {"files": [SimpleUploadedFile("план.txt", b"hello"), SimpleUploadedFile("b.pdf", b"%PDF-1.4")]},
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        atts = resp.json()["attachments"]
        self.assertEqual([a["name"] for a in atts], ["план.txt", "b.pdf"])
        self.assertEqual(atts[0]["size"], 5)

        event = self.client.get(reverse("events:event-detail", args=[ev_id])).json()
        self.assertEqual(len(event["attachments"]), 2)

        resp = self.client.get(atts[0]["url"])
        self.assertEqual(resp.status_code, 200)
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertEqual(b"".join(resp.streaming_content), b"hello")

        path = Attachment.objects.get(pk=atts[0]["id"]).file.path
        resp = self.client.delete(reverse("events:attachment-delete", args=[atts[0]["id"]]))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(os.path.exists(path))

        # удаление события удаляет и оставшиеся файлы
        path2 = Attachment.objects.get(pk=atts[1]["id"]).file.path
        self.client.delete(reverse("events:event-detail", args=[ev_id]))
        self.assertFalse(Attachment.objects.exists())
        self.assertFalse(os.path.exists(path2))

    def test_upload_requires_files(self):
        ev_id = self.create().json()["id"]
        resp = self.client.post(reverse("events:attachment-upload", args=[ev_id]), {})
        self.assertEqual(resp.status_code, 400)


class ApiErrorFormatTests(TestCase):
    def test_not_found_is_json(self):
        login_admin(self.client)
        resp = self.client.get(reverse("events:event-detail", args=[999999]))
        self.assertEqual(resp.status_code, 404)
        self.assertIn("errors", resp.json())


class ReportTests(TestCase):
    """GET /api/events/report/ — «Отчет по событиям.docx» за период."""

    def setUp(self):
        User = get_user_model()
        self.owner = User.objects.create_user("ivanov", "ivanov@example.com", "x", first_name="Иван", last_name="Иванов")
        self.other = User.objects.create_user("petrov", "petrov@example.com", "x", first_name="Пётр", last_name="Петров")
        self.client.force_login(self.owner)
        self.url = reverse("events:events-report")

    def event(self, title, start, end=None, owner=None, **fields):
        return Event.objects.create(title=title, start=start, end=end, owner=owner or self.owner, **fields)

    def document(self, start="2026-10-01", end="2026-10-31"):
        import io
        import zipfile

        resp = self.client.get(self.url, {"start": start, "end": end})
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        archive = zipfile.ZipFile(io.BytesIO(resp.content))
        self.assertIsNone(archive.testzip())
        for name in archive.namelist():
            if name.endswith((".xml", ".rels")):
                ElementTree.fromstring(archive.read(name))  # каждая часть — корректный XML
        return resp, archive.read("word/document.xml").decode("utf-8")

    def test_file_name_and_type(self):
        resp, xml = self.document()
        self.assertEqual(resp["Content-Type"], "application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        self.assertIn(quote("Отчет по событиям.docx"), resp["Content-Disposition"])
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertIn("за период с 01.10.2026 по 31.10.2026", xml)
        self.assertIn("Пользователь: Иван Иванов", xml)
        self.assertIn("Всего событий: 0", xml)
        self.assertIn("За выбранный период событий нет.", xml)

    def test_all_events_of_period_with_details(self):
        meeting = self.event("Совещание <Бюджет & план>", aware(2026, 10, 12, 10, 0), aware(2026, 10, 12, 11, 30),
                             location="каб. 500", description="Повестка:\n1. Бюджет", url="https://example.com/b",
                             guest_emails="guest@example.com, anna@example.com",
                             participant_names={"anna@example.com": "Анна Сидорова"})
        meeting.invitees.add(self.other)
        self.event("Отпуск", aware(2026, 9, 28, 0, 0), aware(2026, 10, 2, 0, 0), all_day=True)  # начался до периода
        self.event("Последний день", aware(2026, 10, 31, 23, 0))  # граница периода включительно
        self.event("В сентябре", aware(2026, 9, 30, 10, 0), aware(2026, 9, 30, 11, 0))
        self.event("В ноябре", aware(2026, 11, 1, 0, 0), aware(2026, 11, 1, 1, 0))
        _, xml = self.document()
        self.assertIn("Всего событий: 3", xml)
        for text in ("Совещание &lt;Бюджет &amp; план&gt;", "12.10.2026", "10:00–11:30", "каб. 500", "Повестка:", "1. Бюджет",
                     "https://example.com/b", "Пётр Петров", "Анна Сидорова", "guest@example.com",
                     "Отпуск", "28.09.2026 – 02.10.2026", "весь день", "Последний день", "23:00", "Иван Иванов"):
            self.assertIn(text, xml)
        for text in ("В сентябре", "В ноябре", "anna@example.com"):
            self.assertNotIn(text, xml)
        self.assertLess(xml.index("Отпуск"), xml.index("Совещание"))  # по времени начала

    def test_only_readable_events(self):
        self.event("Моё", aware(2026, 10, 5, 9, 0))
        self.event("Чужое", aware(2026, 10, 5, 9, 0), owner=self.other)
        invited = self.event("Позвали", aware(2026, 10, 6, 9, 0), owner=self.other)
        invited.invitees.add(self.owner)
        _, xml = self.document()
        self.assertIn("Моё", xml)
        self.assertIn("Позвали", xml)
        self.assertNotIn("Чужое", xml)
        self.client.force_login(get_user_model().objects.create_superuser("root", "root@example.com", "x"))
        self.assertIn("Чужое", self.document()[1])  # администратор видит все календари

    def test_control_characters_do_not_break_document(self):
        self.event("Тема\x0bс мусором\x00", aware(2026, 10, 5, 9, 0), description="a\x1fb\r\nc")
        self.assertIn("Темас мусором", self.document()[1])

    def test_bad_period(self):
        for params in ({}, {"start": "2026-10-01"}, {"start": "2026-10-31", "end": "2026-10-01"},
                       {"start": "2026-02-30", "end": "2026-03-01"}, {"start": "вчера", "end": "2026-03-01"}):
            resp = self.client.get(self.url, params)
            self.assertEqual(resp.status_code, 400, params)
            self.assertTrue(resp.json()["errors"])

    def test_login_required_and_page_has_button(self):
        page = self.client.get(reverse("events:calendar"))
        # кнопка — обычная отправка формы: отчёт скачивается и без скрипта страницы
        self.assertContains(page, f'id="report-btn" formmethod="get" formaction="{self.url}"')
        self.assertContains(page, 'id="report-start" name="start"')
        self.assertContains(page, 'id="report-end" name="end"')
        self.client.logout()
        self.assertEqual(self.client.get(self.url, {"start": "2026-10-01", "end": "2026-10-31"}).status_code, 401)
