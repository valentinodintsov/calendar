# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Тесты адаптера Exchange (EWS) против локального тестового EWS-сервера. Без Django."""

from datetime import datetime, timedelta
from unittest import TestCase
from zoneinfo import ZoneInfo

from integrations.ics import EventData
from integrations.providers.base import AuthError, ConflictError, ProviderError
from integrations.providers.exchange import ExchangeProvider, normalize_ews_url, windows_tz
from integrations.testing_ews import FakeEWS

YEKT = ZoneInfo("Asia/Yekaterinburg")
W_START = datetime(2026, 10, 1, tzinfo=YEKT)
W_END = datetime(2026, 11, 1, tzinfo=YEKT)


def sample(title="Планёрка", day=12, **kw):
    return EventData(
        uid=kw.pop("uid", "calendar-event-1@calendar.local"), title=title, description="Повестка:\n1. Бюджет",
        location="Каб. 305", url="https://meet.example.com/x",
        start=datetime(2026, 10, day, 10, tzinfo=YEKT), end=datetime(2026, 10, day, 11, tzinfo=YEKT), **kw,
    )


class UrlTests(TestCase):
    def test_normalize(self):
        self.assertEqual(normalize_ews_url("email.yanao.ru"), "https://email.yanao.ru/EWS/Exchange.asmx")
        self.assertEqual(normalize_ews_url("https://email.yanao.ru/"), "https://email.yanao.ru/EWS/Exchange.asmx")
        self.assertEqual(normalize_ews_url("https://mail.x/ews/exchange.asmx"), "https://mail.x/ews/exchange.asmx")
        self.assertEqual(windows_tz(YEKT), "Ekaterinburg Standard Time")


class ExchangeTests(TestCase):
    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.p = ExchangeProvider(self.srv.url, self.srv.username, self.srv.password, "calendar", YEKT)

    def test_auth_detection_and_wrong_password(self):
        self.assertEqual(self.p.detect_auth(), "basic")
        bad = ExchangeProvider(self.srv.url, self.srv.username, "wrong", "calendar", YEKT)
        with self.assertRaises(AuthError):
            bad.list_calendars()

    def test_ntlm_required_without_package(self):
        srv = FakeEWS(offer="NTLM").__enter__()
        self.addCleanup(srv.__exit__, None, None, None)
        try:
            import requests_ntlm  # noqa: F401
        except ImportError:
            with self.assertRaises(ProviderError) as ctx:
                ExchangeProvider(srv.url, srv.username, srv.password).detect_auth()
            self.assertIn("requests_ntlm", str(ctx.exception))

    def test_list_calendars(self):
        cals = self.p.list_calendars()
        self.assertEqual([c["id"] for c in cals], ["calendar", "SUB1"])
        self.assertEqual(cals[0]["name"], "Календарь (основной)")

    def test_create_list_update_delete(self):
        item = self.p.create_event(sample())
        stored = self.srv.items[item.remote_id]
        self.assertEqual(stored["UID"], "calendar-event-1@calendar.local")
        self.assertEqual(stored["Start"], "2026-10-12T05:00:00Z")  # 10:00 по Екатеринбургу
        self.assertEqual(stored["Url"], "https://meet.example.com/x")

        [listed] = self.p.list_events(W_START, W_END)
        self.assertEqual(listed.data.content_key(), sample().content_key())  # включая текст и ссылку
        self.assertEqual(listed.etag, item.etag)

        new_ck = self.p.update_event(item.remote_id, item.etag, sample(title="Планёрка (перенос)", day=13))
        self.assertNotEqual(new_ck, item.etag)
        self.assertEqual(self.srv.items[item.remote_id]["Start"], "2026-10-13T05:00:00Z")
        with self.assertRaises(ConflictError):
            self.p.update_event(item.remote_id, item.etag, sample(title="устаревшая версия"))

        changed = sample(title="Без места", day=13)
        changed.location, changed.description = "", ""
        self.p.update_event(item.remote_id, new_ck, changed)
        [listed] = self.p.list_events(W_START, W_END)
        self.assertEqual((listed.data.location, listed.data.description), ("", ""))

        self.p.delete_event(item.remote_id, "")
        self.assertEqual(self.p.list_events(W_START, W_END), [])
        self.p.delete_event(item.remote_id, "")  # повторное удаление — не ошибка

    def test_all_day_event(self):
        ev = EventData(uid="a", title="Отпуск", all_day=True,
                       start=datetime(2026, 10, 20, tzinfo=YEKT), end=datetime(2026, 10, 22, 23, 59, tzinfo=YEKT))
        item = self.p.create_event(ev)
        stored = self.srv.items[item.remote_id]
        self.assertEqual((stored["Start"], stored["End"], stored["IsAllDayEvent"]),
                         ("2026-10-19T19:00:00Z", "2026-10-22T19:00:00Z", "true"))  # полночь 20.10 — полночь 23.10
        [listed] = self.p.list_events(W_START, W_END)
        self.assertEqual((listed.data.start, listed.data.end, listed.data.all_day), (ev.start, ev.end, True))

    def test_events_from_outlook(self):
        self.srv.add(Subject="Из Outlook", Body="Текст\r\nвторая строка\r\n", Start="2026-10-14T04:00:00Z",
                     End="2026-10-14T05:30:00Z", UID="040000008200E00074C5B7101A82E008")
        self.srv.add(Subject="Еженедельная", Start="2026-10-15T04:00:00Z", End="2026-10-15T05:00:00Z",
                     UID="040000008200E0007SERIES", CalendarItemType="Occurrence")
        self.srv.add(Subject="Вне окна", Start="2026-12-01T04:00:00Z", End="2026-12-01T05:00:00Z")
        items = {i.data.title: i for i in self.p.list_events(W_START, W_END)}
        self.assertEqual(set(items), {"Из Outlook", "Еженедельная"})
        outlook = items["Из Outlook"].data
        self.assertEqual(outlook.description, "Текст\nвторая строка")
        self.assertEqual(outlook.end - outlook.start, timedelta(minutes=90))
        self.assertFalse(outlook.read_only)
        weekly = items["Еженедельная"].data
        self.assertTrue(weekly.read_only)
        self.assertTrue(weekly.uid.startswith("040000008200E0007SERIES#"))

    def test_xml_special_characters(self):
        ev = sample(title='Встреча <&> "кавычки"')
        item = self.p.create_event(ev)
        self.assertEqual(self.srv.items[item.remote_id]["Subject"], 'Встреча <&> "кавычки"')


class MeetingTests(TestCase):
    """Участники собрания: Exchange сам рассылает приглашения, обновления и отмены."""

    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.p = ExchangeProvider(self.srv.url, self.srv.username, self.srv.password, "calendar", YEKT)

    def test_plain_event_sends_nothing(self):
        created = self.p.create_event(sample())
        self.p.update_event(created.remote_id, created.etag, sample(title="Новое"))
        self.p.delete_event(created.remote_id, "")
        self.assertEqual([s["mode"] for s in self.srv.sent], ["SendToNone"] * 3)

    def test_meeting_lifecycle(self):
        created = self.p.create_event(sample(attendees=["petrov@yanao.ru", "sidorova@yanao.ru"]))
        self.assertEqual(self.srv.sent[-1], {"op": "create", "mode": "SendToAllAndSaveCopy",
                                             "attendees": ["petrov@yanao.ru", "sidorova@yanao.ru"], "before": []})
        # убрали участника, добавили нового — приглашение и отмена только им
        etag = self.p.update_event(created.remote_id, created.etag,
                                   sample(attendees=["petrov@yanao.ru", "ivanov@yanao.ru"]), send="changed")
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToChangedAndSaveCopy")
        self.assertEqual(self.srv.sent[-1]["attendees"], ["petrov@yanao.ru", "ivanov@yanao.ru"])
        # перенесли — обновление всем
        etag = self.p.update_event(created.remote_id, etag, sample(day=13, attendees=["petrov@yanao.ru"]), send="all")
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToAllAndSaveCopy")
        # обычное обновление содержимого (attendees=None) состав не трогает и никому не пишет
        etag = self.p.update_event(created.remote_id, etag, sample(day=14))
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToNone")
        self.assertEqual(self.srv.items[created.remote_id]["Attendees"], ["petrov@yanao.ru"])
        # убрали всех участников
        self.p.update_event(created.remote_id, etag, sample(day=14, attendees=[]), send="changed")
        self.assertEqual(self.srv.items[created.remote_id]["Attendees"], [])
        self.p.delete_event(created.remote_id, "", notify=True)
        self.assertEqual(self.srv.sent[-1]["mode"], "SendToAllAndSaveCopy")


class FormFieldsRoundTripTests(TestCase):
    """Каждое поле формы события доходит до Exchange и возвращается обратно без потерь."""

    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.p = ExchangeProvider(self.srv.url, self.srv.username, self.srv.password, "calendar", YEKT)

    def back(self, remote_id):
        return next(i.data for i in self.p.list_events(W_START, W_END) if i.remote_id == remote_id)

    def test_all_fields(self):
        data = sample(title="Совет <№1> & итоги")
        data.description = "Строка 1\nСтрока 2\x0b"  # управляющий символ из буфера обмена — не должен ломать запрос
        created = self.p.create_event(data)
        got = self.back(created.remote_id)
        self.assertEqual((got.title, got.description, got.location, got.url),
                         ("Совет <№1> & итоги", "Строка 1\nСтрока 2", "Каб. 305", "https://meet.example.com/x"))
        self.assertEqual((got.start, got.end, got.all_day),
                         (datetime(2026, 10, 12, 10, tzinfo=YEKT), datetime(2026, 10, 12, 11, tzinfo=YEKT), False))
        self.assertEqual(got.uid, "calendar-event-1@calendar.local")

    def test_clearing_fields(self):
        created = self.p.create_event(sample())
        cleared = sample()
        cleared.description = cleared.location = cleared.url = ""
        self.p.update_event(created.remote_id, created.etag, cleared)
        got = self.back(created.remote_id)
        self.assertEqual((got.description, got.location, got.url), ("", "", ""))

    def test_event_without_link_has_no_url_property(self):
        data = sample()
        data.url = ""
        created = self.p.create_event(data)
        self.assertEqual(self.back(created.remote_id).url, "")


class LinkInBodyTests(TestCase):
    """Ссылка хранится в Exchange открыто: в тексте события после описания, через пустую строку."""

    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.p = ExchangeProvider(self.srv.url, self.srv.username, self.srv.password, "calendar", YEKT)

    def back(self, remote_id):
        return next(i.data for i in self.p.list_events(W_START, W_END) if i.remote_id == remote_id)

    def test_compose_and_split(self):
        from integrations.providers.exchange import compose_body, split_body

        self.assertEqual(compose_body("Повестка", "https://bbb.yanao.ru/b/abc"), "Повестка\n\nhttps://bbb.yanao.ru/b/abc")
        self.assertEqual(compose_body("", "https://bbb.yanao.ru/b/abc"), "https://bbb.yanao.ru/b/abc")
        self.assertEqual(compose_body("Повестка", ""), "Повестка")
        self.assertEqual(split_body("Повестка\r\n\r\nhttps://x.ru/1", "https://x.ru/1"), ("Повестка", "https://x.ru/1"))
        self.assertEqual(split_body("https://x.ru/1", "https://x.ru/1"), ("", "https://x.ru/1"))
        # без отметки CalendarUrl адрес в конце — это часть описания
        self.assertEqual(split_body("Материалы:\n\nhttps://x.ru/doc", ""), ("Материалы:\n\nhttps://x.ru/doc", ""))

    def test_link_visible_in_body_and_round_trip(self):
        data = sample()
        data.url = "https://bbb.yanao.ru/b/abc-def"
        created = self.p.create_event(data)
        self.assertEqual(self.srv.items[created.remote_id]["Body"], "Повестка:\n1. Бюджет\n\nhttps://bbb.yanao.ru/b/abc-def")
        got = self.back(created.remote_id)
        self.assertEqual((got.description, got.url), ("Повестка:\n1. Бюджет", "https://bbb.yanao.ru/b/abc-def"))

    def test_description_ending_with_address_is_kept(self):
        data = sample()
        data.description, data.url = "Материалы:\n\nhttps://docs.yanao.ru/1", ""
        created = self.p.create_event(data)
        got = self.back(created.remote_id)
        self.assertEqual((got.description, got.url), ("Материалы:\n\nhttps://docs.yanao.ru/1", ""))

    def test_link_edited_or_removed_in_outlook(self):
        data = sample()
        data.url = "https://bbb.yanao.ru/b/old"
        created = self.p.create_event(data)
        item = self.srv.items[created.remote_id]
        item["Body"] = "Повестка:\n1. Бюджет\n\nhttps://trueconf.yanao.ru/c/new"
        self.assertEqual(self.back(created.remote_id).url, "https://trueconf.yanao.ru/c/new")
        # событие, записанное старой версией (ссылка только в скрытой отметке), — ссылка не теряется
        item["Body"] = "Повестка:\n1. Бюджет"
        items = [i for i in self.p.list_events(W_START, W_END) if i.remote_id == created.remote_id]
        self.assertEqual((items[0].data.description, items[0].data.url), ("Повестка:\n1. Бюджет", "https://bbb.yanao.ru/b/old"))
        self.assertTrue(items[0].needs_rewrite)

    def test_update_moves_link_into_body(self):
        created = self.p.create_event(sample())
        data = sample(title="Новое")
        data.url = "https://trueconf.yanao.ru/c/1"
        self.p.update_event(created.remote_id, created.etag, data)
        self.assertTrue(self.srv.items[created.remote_id]["Body"].endswith("\n\nhttps://trueconf.yanao.ru/c/1"))
        data.url = ""
        self.p.update_event(created.remote_id, self.srv.items[created.remote_id]["ChangeKey"], data)
        self.assertEqual(self.srv.items[created.remote_id]["Body"], "Повестка:\n1. Бюджет")


class ReadParticipantsAndFilesTests(TestCase):
    """Участники и вложения события, созданного в Outlook, читаются из Exchange."""

    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.p = ExchangeProvider(self.srv.url, self.srv.username, self.srv.password, "calendar", YEKT)

    def test_attendees_and_attachments(self):
        iid = self.srv.add(Subject="Тест", Start="2026-10-14T00:00:00Z", End="2026-10-15T00:00:00Z", IsAllDayEvent="true",
                           Location="каб. 500", Attendees=["vnodintsov@yanao.ru", "IMRyabkov@yanao.ru"],
                           Optional=["/O=YG/OU=EXCHANGE/CN=RECIPIENTS/CN=OLD"],
                           Files=[{"Name": "file.doc", "ContentType": "application/msword", "Content": b"DOC-BYTES"},
                                  {"Name": "image001.png", "Content": b"PNG", "Inline": True}])
        data = next(i.data for i in self.p.list_events(W_START, W_END) if i.remote_id == iid)
        self.assertEqual(data.attendees, ["vnodintsov@yanao.ru", "IMRyabkov@yanao.ru"])  # адрес вида /O=… пропущен
        self.assertEqual([(a["name"], a["size"], a["content_type"]) for a in data.attachments],
                         [("file.doc", 9, "application/msword")])  # картинка внутри текста — не файл
        self.assertEqual(self.p.get_attachment(data.attachments[0]["id"]), b"DOC-BYTES")


class SendAttachmentsTests(TestCase):
    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.p = ExchangeProvider(self.srv.url, self.srv.username, self.srv.password, "calendar", YEKT)

    def test_add_and_delete(self):
        created = self.p.create_event(sample())
        aid, ck = self.p.add_attachment(created.remote_id, "файл.txt", "Привет".encode(), "text/plain")
        files = self.srv.items[created.remote_id]["Files"]
        self.assertEqual([(f["Name"], f["Content"]) for f in files], [("файл.txt", "Привет".encode())])
        self.assertEqual(ck, self.srv.items[created.remote_id]["ChangeKey"])
        data = next(i.data for i in self.p.list_events(W_START, W_END))
        self.assertEqual([a["id"] for a in data.attachments], [aid])
        ck2 = self.p.delete_attachment(aid)
        self.assertEqual(self.srv.items[created.remote_id]["Files"], [])
        self.assertTrue(ck2)
        self.assertEqual(self.p.delete_attachment(aid), "")  # уже удалён — не ошибка


class ReminderTests(TestCase):
    def setUp(self):
        self.srv = FakeEWS().__enter__()
        self.addCleanup(self.srv.__exit__, None, None, None)
        self.p = ExchangeProvider(self.srv.url, self.srv.username, self.srv.password, "calendar", YEKT)

    def back(self, remote_id):
        return next(i.data for i in self.p.list_events(W_START, W_END) if i.remote_id == remote_id)

    def test_reminder_both_ways(self):
        created = self.p.create_event(sample(reminder=30))
        item = self.srv.items[created.remote_id]
        self.assertEqual((item["ReminderIsSet"], item["ReminderMinutesBeforeStart"]), ("true", "30"))
        self.assertEqual(self.back(created.remote_id).reminder, 30)
        self.p.update_event(created.remote_id, item["ChangeKey"], sample(reminder=-1))
        self.assertEqual(item["ReminderIsSet"], "false")
        self.assertEqual(self.back(created.remote_id).reminder, -1)
        item["ReminderIsSet"], item["ReminderMinutesBeforeStart"] = "true", "1440"  # поменяли в Outlook
        self.assertEqual(self.back(created.remote_id).reminder, 1440)

    def test_no_reminder_by_default(self):
        created = self.p.create_event(sample())
        self.assertEqual(self.srv.items[created.remote_id]["ReminderIsSet"], "false")
