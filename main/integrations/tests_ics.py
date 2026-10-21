# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Тесты модуля iCalendar. Не требуют Django и базы данных."""

from datetime import datetime, timedelta, timezone
from unittest import TestCase
from zoneinfo import ZoneInfo

from integrations.ics import EventData, expand_all, fold, parse_vevents, to_ics, unfold

MSK = ZoneInfo("Europe/Moscow")
YEKT = ZoneInfo("Asia/Yekaterinburg")
UTC = timezone.utc

SAMPLE = (
    "BEGIN:VCALENDAR\r\n"
    "VERSION:2.0\r\n"
    "BEGIN:VTIMEZONE\r\nTZID:Europe/Moscow\r\nEND:VTIMEZONE\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:abc-1@yandex.ru\r\n"
    "DTSTART;TZID=Europe/Moscow:20261012T100000\r\n"
    "DTEND;TZID=Europe/Moscow:20261012T113000\r\n"
    "SUMMARY:Совет директоров\\, итоги\r\n"
    "DESCRIPTION:Строка 1\\nСтрока 2 — длинное описание\r\n"
    "  продолжение\r\n"
    "LOCATION:Переговорная\\; 1\r\n"
    "URL:https://meet.example.com/abc\r\n"
    "LAST-MODIFIED:20261001T080000Z\r\n"
    "BEGIN:VALARM\r\nACTION:DISPLAY\r\nTRIGGER:-PT15M\r\nEND:VALARM\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:allday-2\r\n"
    "DTSTART;VALUE=DATE:20261020\r\n"
    "DTEND;VALUE=DATE:20261023\r\n"
    "SUMMARY:Отпуск\r\n"
    "END:VEVENT\r\n"
    "BEGIN:VEVENT\r\n"
    "UID:cancelled\r\nSTATUS:CANCELLED\r\nDTSTART:20261012T100000Z\r\nSUMMARY:x\r\n"
    "END:VEVENT\r\n"
    "END:VCALENDAR\r\n"
)


class ParseTests(TestCase):
    def test_parse_timed_event(self):
        events = parse_vevents(SAMPLE, default_tz=YEKT)
        self.assertEqual([e.uid for e in events], ["abc-1@yandex.ru", "allday-2"])  # отменённое пропущено
        ev = events[0]
        self.assertEqual(ev.title, "Совет директоров, итоги")
        self.assertEqual(ev.description, "Строка 1\nСтрока 2 — длинное описание продолжение")
        self.assertEqual(ev.location, "Переговорная; 1")
        self.assertEqual(ev.url, "https://meet.example.com/abc")
        self.assertEqual(ev.start, datetime(2026, 10, 12, 10, 0, tzinfo=MSK))
        self.assertEqual(ev.end - ev.start, timedelta(hours=1, minutes=30))
        self.assertEqual(ev.last_modified, datetime(2026, 10, 1, 8, 0, tzinfo=UTC))
        self.assertFalse(ev.all_day)

    def test_parse_all_day(self):
        ev = parse_vevents(SAMPLE, default_tz=YEKT)[1]
        self.assertTrue(ev.all_day)
        self.assertEqual(ev.start, datetime(2026, 10, 20, tzinfo=YEKT))
        # DTEND исключён: 20–22 октября, у нас конец — 22.10 23:59
        self.assertEqual(ev.end, datetime(2026, 10, 22, 23, 59, tzinfo=YEKT))

    def test_utc_duration_and_windows_tz(self):
        text = (
            "BEGIN:VEVENT\nUID:1\nDTSTART:20261012T070000Z\nDURATION:PT45M\nSUMMARY:a\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:2\nDTSTART;TZID=Russian Standard Time:20261012T100000\nSUMMARY:b\nEND:VEVENT\n"
        )
        a, b = parse_vevents(text)
        self.assertEqual(a.end - a.start, timedelta(minutes=45))
        self.assertEqual(b.start.astimezone(UTC), datetime(2026, 10, 12, 7, 0, tzinfo=UTC))

    def test_roundtrip(self):
        src = EventData(
            uid="calendar-event-5@example",
            title="Планёрка, важная; очень",
            description="Повестка:\n1. Бюджет\n2. " + "очень длинный текст " * 10,
            location="Офис",
            url="https://example.com/x",
            start=datetime(2026, 10, 12, 10, 0, tzinfo=YEKT),
            end=datetime(2026, 10, 12, 11, 0, tzinfo=YEKT),
        )
        text = to_ics(src)
        for line in text.split("\r\n"):
            self.assertLessEqual(len(line.encode("utf-8")), 75)
        [back] = parse_vevents(text, default_tz=YEKT)
        self.assertEqual(back.content_key(), src.content_key())

    def test_all_day_roundtrip(self):
        src = EventData(
            uid="x", title="Отпуск", all_day=True,
            start=datetime(2026, 10, 20, tzinfo=YEKT), end=datetime(2026, 10, 22, 23, 59, tzinfo=YEKT),
        )
        text = to_ics(src, tz=YEKT)
        self.assertIn("DTSTART;VALUE=DATE:20261020", text)
        self.assertIn("DTEND;VALUE=DATE:20261023", text)
        [back] = parse_vevents(text, default_tz=YEKT)
        self.assertEqual((back.start, back.end, back.all_day), (src.start, src.end, True))

    def test_fold_unfold(self):
        line = "DESCRIPTION:" + "Ж" * 100
        self.assertEqual(unfold(fold(line).replace("\r\n", "\n")), [line])


class RecurrenceTests(TestCase):
    W_START = datetime(2026, 10, 1, tzinfo=MSK)
    W_END = datetime(2026, 11, 1, tzinfo=MSK)

    def expand(self, body):
        text = "BEGIN:VEVENT\nUID:r\nSUMMARY:R\n" + body + "\nEND:VEVENT\n"
        return expand_all(parse_vevents(text, MSK), self.W_START, self.W_END, MSK)

    def test_weekly_byday_with_exdate_and_until(self):
        res = self.expand(
            "DTSTART;TZID=Europe/Moscow:20260928T090000\nDTEND;TZID=Europe/Moscow:20260928T093000\n"
            "RRULE:FREQ=WEEKLY;BYDAY=MO,WE;UNTIL=20261021T235959Z\n"
            "EXDATE;TZID=Europe/Moscow:20261007T090000"
        )
        days = [e.start.day for e in res]
        self.assertEqual(days, [5, 12, 14, 19, 21])  # 30.09 вне окна, 1 и 7 — нет (1-е не ПН/СР, 7-е исключено)
        self.assertTrue(all(e.read_only for e in res))
        self.assertEqual(res[0].uid, "r#20261005T060000Z")
        self.assertEqual(res[0].end - res[0].start, timedelta(minutes=30))

    def test_daily_count_and_interval(self):
        res = self.expand("DTSTART:20261001T070000Z\nRRULE:FREQ=DAILY;INTERVAL=3;COUNT=4")
        self.assertEqual([e.start.astimezone(UTC).day for e in res], [1, 4, 7, 10])

    def test_monthly_last_friday_and_yearly(self):
        res = self.expand("DTSTART;TZID=Europe/Moscow:20260130T150000\nRRULE:FREQ=MONTHLY;BYDAY=-1FR")
        self.assertEqual([e.start.date().isoformat() for e in res], ["2026-10-30"])
        res = self.expand("DTSTART;VALUE=DATE:20201015\nRRULE:FREQ=YEARLY")
        self.assertEqual([e.start.date().isoformat() for e in res], ["2026-10-15"])
        self.assertTrue(res[0].all_day)

    def test_dst_wall_clock_kept(self):
        # Берлин: 25.10.2026 — переход на зимнее время; встреча остаётся в 10:00 по местному
        berlin = ZoneInfo("Europe/Berlin")
        res = expand_all(
            parse_vevents(
                "BEGIN:VEVENT\nUID:b\nDTSTART;TZID=Europe/Berlin:20261020T100000\n"
                "DTEND;TZID=Europe/Berlin:20261020T110000\nRRULE:FREQ=DAILY;COUNT=10\nSUMMARY:x\nEND:VEVENT\n",
                berlin,
            ),
            self.W_START, self.W_END, berlin,
        )
        self.assertTrue(all(e.start.astimezone(berlin).hour == 10 for e in res))
        self.assertEqual(len({e.start.astimezone(UTC).hour for e in res}), 2)

    def test_override_instance(self):
        text = (
            "BEGIN:VEVENT\nUID:m\nDTSTART:20261005T070000Z\nDTEND:20261005T080000Z\n"
            "RRULE:FREQ=WEEKLY;COUNT=3\nSUMMARY:Планёрка\nEND:VEVENT\n"
            "BEGIN:VEVENT\nUID:m\nRECURRENCE-ID:20261012T070000Z\nDTSTART:20261012T090000Z\n"
            "DTEND:20261012T100000Z\nSUMMARY:Планёрка (перенесена)\nEND:VEVENT\n"
        )
        res = expand_all(parse_vevents(text), self.W_START, self.W_END)
        self.assertEqual([(e.title, e.start.astimezone(UTC).hour) for e in res],
                         [("Планёрка", 7), ("Планёрка (перенесена)", 9), ("Планёрка", 7)])
        self.assertEqual(res[1].uid, "m#20261012T070000Z")
