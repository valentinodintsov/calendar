"""
Минимальная поддержка iCalendar (RFC 5545) без внешних зависимостей:
разбор VEVENT, генерация .ics и развёртка повторяющихся событий (RRULE) в заданном окне.

Нейтральное представление события — EventData; его используют адаптеры CalDAV и Google.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field, replace
from datetime import date, datetime, time, timedelta, timezone as dt_timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

UTC = dt_timezone.utc

# Часто встречающиеся Windows-имена часовых поясов (Outlook, Exchange)
WINDOWS_TZ = {
    "Russian Standard Time": "Europe/Moscow",
    "Ekaterinburg Standard Time": "Asia/Yekaterinburg",
    "N. Central Asia Standard Time": "Asia/Novosibirsk",
    "North Asia Standard Time": "Asia/Krasnoyarsk",
    "North Asia East Standard Time": "Asia/Irkutsk",
    "Yakutsk Standard Time": "Asia/Yakutsk",
    "Vladivostok Standard Time": "Asia/Vladivostok",
    "Kaliningrad Standard Time": "Europe/Kaliningrad",
    "Samara Standard Time": "Europe/Samara",
    "Omsk Standard Time": "Asia/Omsk",
    "UTC": "UTC",
    "GMT Standard Time": "Europe/London",
    "W. Europe Standard Time": "Europe/Berlin",
    "Central Europe Standard Time": "Europe/Budapest",
    "E. Europe Standard Time": "Europe/Chisinau",
    "FLE Standard Time": "Europe/Kiev",
    "Eastern Standard Time": "America/New_York",
    "Pacific Standard Time": "America/Los_Angeles",
}


@dataclass
class EventData:
    """Событие в нейтральном виде. start/end — aware datetime (у all_day — полночь в tz по умолчанию)."""

    uid: str
    title: str = ""
    description: str = ""
    location: str = ""
    url: str = ""
    start: datetime | None = None
    end: datetime | None = None
    all_day: bool = False
    last_modified: datetime | None = None
    read_only: bool = False  # экземпляр повторяющегося события — менять у себя нельзя
    rrule: dict = field(default_factory=dict)
    exdates: list = field(default_factory=list)
    recurrence_id: datetime | None = None
    sequence: int = 0
    # участники (e-mail) — передаются только туда, где сервер сам рассылает приглашения (Exchange);
    # None — участников не трогать; в сравнение содержимого не входят
    attendees: list | None = None
    # напоминание: минут до начала; -1 — выключено; None — сервис не сообщает (не трогать)
    reminder: int | None = None
    # вложения во внешнем календаре: [{"id", "name", "size", "content_type"}] (только чтение; None — сервис не сообщает)
    attachments: list | None = None

    def content_key(self, with_reminder=False):
        """Значимые поля — для сравнения «изменилось ли». Напоминание — только там, где сервис его хранит (Exchange)."""
        reminder = ("reminder", self.reminder) if with_reminder and self.reminder is not None and self.reminder >= 0 else ()
        return reminder + (
            self.title,
            self.description,
            self.location,
            self.url,
            self.start.astimezone(UTC).isoformat() if self.start else "",
            self.end.astimezone(UTC).isoformat() if self.end else "",
            self.all_day,
        )


# ---------------------------------------------------------------- разбор


def unfold(text: str) -> list[str]:
    """Склеивает перенесённые строки (RFC 5545, 3.1)."""
    lines: list[str] = []
    for raw in re.split(r"\r\n|\n|\r", text):
        if raw[:1] in (" ", "\t") and lines:
            lines[-1] += raw[1:]
        elif raw:
            lines.append(raw)
    return lines


def parse_line(line: str):
    """'DTSTART;TZID=Europe/Moscow:20261012T100000' → ('DTSTART', {'TZID': 'Europe/Moscow'}, '20261012T100000')."""
    in_quotes = False
    colon = -1
    for i, ch in enumerate(line):
        if ch == '"':
            in_quotes = not in_quotes
        elif ch == ":" and not in_quotes:
            colon = i
            break
    if colon < 0:
        return None
    head, value = line[:colon], line[colon + 1 :]
    parts = []
    buf, in_quotes = "", False
    for ch in head:
        if ch == '"':
            in_quotes = not in_quotes
        if ch == ";" and not in_quotes:
            parts.append(buf)
            buf = ""
        else:
            buf += ch
    parts.append(buf)
    name = parts[0].upper()
    params = {}
    for p in parts[1:]:
        if "=" in p:
            k, v = p.split("=", 1)
            params[k.upper()] = v.strip('"')
    return name, params, value


def unescape(value: str) -> str:
    out, i = [], 0
    while i < len(value):
        ch = value[i]
        if ch == "\\" and i + 1 < len(value):
            nxt = value[i + 1]
            out.append("\n" if nxt in "nN" else nxt)
            i += 2
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def get_zone(tzid: str | None, default_tz):
    if not tzid:
        return default_tz
    tzid = tzid.strip()
    name = WINDOWS_TZ.get(tzid, tzid)
    # встречаются TZID вида "/mozilla.org/20050126_1/Europe/Moscow"
    if name.startswith("/"):
        name = "/".join(name.split("/")[-2:])
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return default_tz


def parse_dt(value: str, params: dict, default_tz):
    """Возвращает (aware datetime, is_date)."""
    value = value.strip()
    if params.get("VALUE", "").upper() == "DATE" or re.fullmatch(r"\d{8}", value):
        d = datetime.strptime(value[:8], "%Y%m%d").date()
        return datetime.combine(d, time.min, tzinfo=default_tz), True
    m = re.fullmatch(r"(\d{8})T(\d{4})(\d{2})?(Z?)", value)
    if not m:
        raise ValueError(f"Неизвестный формат даты: {value}")
    naive = datetime.strptime(m.group(1) + m.group(2) + (m.group(3) or "00"), "%Y%m%d%H%M%S")
    if m.group(4):
        return naive.replace(tzinfo=UTC), False
    return naive.replace(tzinfo=get_zone(params.get("TZID"), default_tz)), False


def parse_duration(value: str) -> timedelta:
    m = re.fullmatch(r"([+-])?P(?:(\d+)W)?(?:(\d+)D)?(?:T(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?)?", value.strip())
    if not m:
        return timedelta(0)
    sign = -1 if m.group(1) == "-" else 1
    w, d, h, mi, s = (int(x or 0) for x in m.groups()[1:])
    return sign * timedelta(weeks=w, days=d, hours=h, minutes=mi, seconds=s)


def parse_rrule(value: str) -> dict:
    rule = {}
    for part in value.split(";"):
        if "=" in part:
            k, v = part.split("=", 1)
            rule[k.upper()] = v.upper() if k.upper() != "UNTIL" else v
    return rule


def parse_vevents(text: str, default_tz=UTC) -> list[EventData]:
    """Все VEVENT из текста .ics (вложенные VALARM пропускаются, отменённые события — тоже)."""
    events = []
    stack: list[str] = []
    props: list | None = None
    for line in unfold(text):
        parsed = parse_line(line)
        if not parsed:
            continue
        name, params, value = parsed
        if name == "BEGIN":
            stack.append(value.upper())
            if value.upper() == "VEVENT":
                props = []
            continue
        if name == "END":
            comp = stack.pop() if stack else ""
            if comp == "VEVENT" and props is not None:
                ev = _build_event(props, default_tz)
                if ev:
                    events.append(ev)
                props = None
            continue
        if props is not None and stack and stack[-1] == "VEVENT":
            props.append((name, params, value))
    return events


def _build_event(props, default_tz) -> EventData | None:
    data = {}
    exdates = []
    duration = None
    for name, params, value in props:
        if name == "STATUS" and value.upper() == "CANCELLED":
            return None
        if name in ("DTSTART", "DTEND", "RECURRENCE-ID"):
            data[name] = parse_dt(value, params, default_tz)
        elif name == "DURATION":
            duration = parse_duration(value)
        elif name == "EXDATE":
            for v in value.split(","):
                exdates.append(parse_dt(v, params, default_tz)[0])
        elif name == "LAST-MODIFIED":
            try:
                data[name] = parse_dt(value, params, UTC)[0]
            except ValueError:
                pass
        elif name in ("UID", "SUMMARY", "DESCRIPTION", "LOCATION", "URL", "RRULE", "SEQUENCE"):
            data.setdefault(name, value)
    if "UID" not in data or "DTSTART" not in data:
        return None

    start, all_day = data["DTSTART"]
    if "DTEND" in data:
        end = data["DTEND"][0]
    elif duration is not None:
        end = start + duration
    else:
        end = start + (timedelta(days=1) if all_day else timedelta(0))
    if all_day:
        # в iCalendar конец дня-события исключён; у нас — 23:59 последнего дня
        end = max(end - timedelta(minutes=1), start)

    try:
        sequence = int(data.get("SEQUENCE", 0))
    except ValueError:
        sequence = 0
    return EventData(
        uid=unescape(data["UID"]),
        title=unescape(data.get("SUMMARY", "")),
        description=unescape(data.get("DESCRIPTION", "")),
        location=unescape(data.get("LOCATION", "")),
        url=unescape(data.get("URL", "")),
        start=start,
        end=end,
        all_day=all_day,
        last_modified=data.get("LAST-MODIFIED"),
        rrule=parse_rrule(data["RRULE"]) if "RRULE" in data else {},
        exdates=exdates,
        recurrence_id=data["RECURRENCE-ID"][0] if "RECURRENCE-ID" in data else None,
        sequence=sequence,
    )


# ---------------------------------------------------------------- повторяющиеся события

WEEKDAYS = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]


def _add_months(d: date, months: int) -> date | None:
    y, m = divmod(d.month - 1 + months, 12)
    try:
        return d.replace(year=d.year + y, month=m + 1)
    except ValueError:
        return None  # 31-е в месяце без 31-го — по RFC такое повторение пропускается


def _nth_weekday(year: int, month: int, weekday: int, n: int) -> date | None:
    first = date(year, month, 1)
    if n > 0:
        d = first + timedelta(days=(weekday - first.weekday()) % 7 + 7 * (n - 1))
    else:
        nxt = date(year + (month == 12), month % 12 + 1, 1)
        last = nxt - timedelta(days=1)
        d = last - timedelta(days=(last.weekday() - weekday) % 7 + 7 * (-n - 1))
    return d if d.month == month else None


def _occurrence_dates(start: date, rule: dict, limit_date: date, max_count: int):
    """Даты повторений (в местной дате события) по упрощённой поддержке RRULE."""
    freq = rule.get("FREQ")
    interval = max(1, int(rule.get("INTERVAL", "1") or 1))
    count = int(rule["COUNT"]) if rule.get("COUNT", "").isdigit() else None
    byday = [x for x in rule.get("BYDAY", "").split(",") if x]
    bymonthday = [int(x) for x in rule.get("BYMONTHDAY", "").split(",") if x.lstrip("-").isdigit()]
    produced = 0

    def emit(d):
        nonlocal produced
        produced += 1
        return d

    step = 0
    while produced < max_count:
        if freq == "DAILY":
            candidates = [start + timedelta(days=step * interval)]
        elif freq == "WEEKLY":
            week_start = start - timedelta(days=start.weekday()) + timedelta(weeks=step * interval)
            days = [WEEKDAYS.index(b[-2:]) for b in byday if b[-2:] in WEEKDAYS] or [start.weekday()]
            candidates = sorted(week_start + timedelta(days=wd) for wd in set(days))
        elif freq == "MONTHLY":
            month = _add_months(start.replace(day=1), step * interval)
            candidates = []
            if bymonthday:
                for md in bymonthday:
                    nxt = _add_months(month, 1)
                    last_day = (nxt - timedelta(days=1)).day
                    day = md if md > 0 else last_day + md + 1
                    if 1 <= day <= last_day:
                        candidates.append(month.replace(day=day))
            elif byday:
                for b in byday:
                    m = re.fullmatch(r"([+-]?\d)?(MO|TU|WE|TH|FR|SA|SU)", b)
                    if m and m.group(1):
                        d = _nth_weekday(month.year, month.month, WEEKDAYS.index(m.group(2)), int(m.group(1)))
                        if d:
                            candidates.append(d)
            else:
                d = _add_months(start, step * interval)
                if d:
                    candidates.append(d)
            candidates.sort()
        elif freq == "YEARLY":
            try:
                candidates = [start.replace(year=start.year + step * interval)]
            except ValueError:
                candidates = []  # 29 февраля в невисокосный год
        else:
            return  # неподдерживаемое правило — только исходное событие (см. expand)
        for d in candidates:
            if d < start:
                continue
            if d > limit_date:
                return
            if count is not None and produced >= count:
                return
            yield emit(d)
            if produced >= max_count:
                return
        step += 1
        if step > 5000:
            return


def expand(ev: EventData, window_start: datetime, window_end: datetime, default_tz=UTC, max_count=1000):
    """
    Развёртывает повторяющееся событие в отдельные экземпляры в окне [window_start, window_end).
    Экземпляры получают uid «<uid>#<дата начала>» и read_only=True.
    Неповторяющееся событие возвращается как есть, если пересекается с окном.
    """
    duration = ev.end - ev.start
    if not ev.rrule:
        return [ev] if ev.end >= window_start and ev.start < window_end else []

    tz = ev.start.tzinfo or default_tz
    local_start = ev.start.astimezone(tz)
    until = None
    if ev.rrule.get("UNTIL"):
        try:
            until = parse_dt(ev.rrule["UNTIL"], {}, tz)[0]
        except ValueError:
            until = None
    limit = window_end.astimezone(tz).date()
    if until:
        limit = min(limit, until.astimezone(tz).date())
    excluded = {x.astimezone(UTC) for x in ev.exdates}

    result = []
    if ev.rrule.get("FREQ") not in ("DAILY", "WEEKLY", "MONTHLY", "YEARLY"):
        dates = [local_start.date()]
    else:
        dates = _occurrence_dates(local_start.date(), ev.rrule, limit, max_count)
    for d in dates:
        start = datetime.combine(d, local_start.timetz().replace(tzinfo=None), tzinfo=tz)
        if until and start > until:
            break
        if start.astimezone(UTC) in excluded:
            continue
        end = start + duration
        if end < window_start or start >= window_end:
            continue
        result.append(
            replace(
                ev,
                uid=f"{ev.uid}#{start.astimezone(UTC):%Y%m%dT%H%M%SZ}",
                start=start,
                end=end,
                rrule={},
                exdates=[],
                read_only=True,
                recurrence_id=start,
            )
        )
    return result


def expand_all(events: list[EventData], window_start, window_end, default_tz=UTC) -> list[EventData]:
    """
    Развёртывает список VEVENT одного или нескольких ресурсов: повторения + изменённые экземпляры
    (RECURRENCE-ID заменяют соответствующее повторение).
    """
    overrides = {}
    masters = []
    for ev in events:
        if ev.recurrence_id is not None:
            overrides[(ev.uid, ev.recurrence_id.astimezone(UTC))] = ev
        else:
            masters.append(ev)
    result = []
    for ev in masters:
        if not ev.rrule:
            if ev.end >= window_start and ev.start < window_end:
                result.append(ev)
            continue
        for inst in expand(ev, window_start, window_end, default_tz):
            key = (ev.uid, inst.recurrence_id.astimezone(UTC))
            if key in overrides:
                o = overrides.pop(key)
                inst = replace(o, uid=inst.uid, read_only=True, rrule={}, exdates=[])
            result.append(inst)
    # изменённые экземпляры, перенесённые в окно из-за его пределов
    for (uid, rid), o in overrides.items():
        if o.end >= window_start and o.start < window_end:
            result.append(replace(o, uid=f"{uid}#{rid:%Y%m%dT%H%M%SZ}", read_only=True))
    return result


# ---------------------------------------------------------------- генерация


def escape(value: str) -> str:
    return (
        (value or "")
        .replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\r\n", "\\n")
        .replace("\n", "\\n")
    )


def fold(line: str) -> str:
    """Перенос длинных строк по 75 октетов UTF-8 (RFC 5545, 3.1)."""
    out, cur, size = [], "", 0
    for ch in line:
        n = len(ch.encode("utf-8"))
        if size + n > (75 if not out else 74):
            out.append(cur)
            cur, size = "", 0
        cur += ch
        size += n
    out.append(cur)
    return "\r\n ".join(out)


def fmt_utc(dt: datetime) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def vevent_lines(ev: EventData, extra: list[str] | None = None, tz=UTC) -> list[str]:
    lines = ["BEGIN:VEVENT", f"UID:{escape(ev.uid)}", f"DTSTAMP:{fmt_utc(datetime.now(UTC))}"]
    if ev.all_day:
        start_d = ev.start.astimezone(tz).date()
        end_d = ev.end.astimezone(tz).date() + timedelta(days=1)  # конец исключён
        lines += [f"DTSTART;VALUE=DATE:{start_d:%Y%m%d}", f"DTEND;VALUE=DATE:{end_d:%Y%m%d}"]
    else:
        lines += [f"DTSTART:{fmt_utc(ev.start)}", f"DTEND:{fmt_utc(ev.end or ev.start)}"]
    lines.append(f"SUMMARY:{escape(ev.title)}")
    if ev.description:
        lines.append(f"DESCRIPTION:{escape(ev.description)}")
    if ev.location:
        lines.append(f"LOCATION:{escape(ev.location)}")
    if ev.url:
        lines.append(f"URL:{ev.url}")
    if ev.last_modified:
        lines.append(f"LAST-MODIFIED:{fmt_utc(ev.last_modified)}")
    if ev.sequence:
        lines.append(f"SEQUENCE:{ev.sequence}")
    lines += extra or []
    lines.append("END:VEVENT")
    return lines


def build_calendar(vevents: list[list[str]], method: str | None = None) -> str:
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Calendar Django//RU", "CALSCALE:GREGORIAN"]
    if method:
        lines.append(f"METHOD:{method}")
    for v in vevents:
        lines += v
    lines.append("END:VCALENDAR")
    return "\r\n".join(fold(line) for line in lines) + "\r\n"


def to_ics(ev: EventData, tz=UTC) -> str:
    """Один VEVENT в отдельном VCALENDAR — так событие хранится на CalDAV-сервере."""
    return build_calendar([vevent_lines(ev, tz=tz)])
