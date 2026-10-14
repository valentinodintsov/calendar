"""
Адаптер Microsoft Exchange (on-premises, 2010 SP2 и новее) через Exchange Web Services (EWS, SOAP).
Вход — доменная учётная запись: NTLM (через requests_ntlm) или Basic, выбирается по ответу сервера.

Событие календаря Exchange ↔ EventData:
  Subject ↔ title, Body (текст) ↔ description + url, Location ↔ location,
  Start/End/IsAllDayEvent ↔ start/end/all_day, UID ↔ uid,
  ссылка (url) — в открытом виде в тексте события: «описание», пустая строка, ссылка (у Exchange нет поля «ссылка»);
  расширенное свойство CalendarUrl лишь отмечает, что последний абзац текста — это ссылка,
  attendees ↔ RequiredAttendees: если участники заданы, событие становится собранием и Exchange
  сам рассылает приглашения, обновления и отмены — встреча появляется в календарях участников.
"""

from __future__ import annotations

import base64
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from xml.sax.saxutils import escape

import requests

from ..ics import EventData
from .base import TIMEOUT, AuthError, ConflictError, Provider, ProviderError, RemoteItem

NS = {
    "s": "http://schemas.xmlsoap.org/soap/envelope/",
    "t": "http://schemas.microsoft.com/exchange/services/2006/types",
    "m": "http://schemas.microsoft.com/exchange/services/2006/messages",
}
SERVER_VERSION = "Exchange2010_SP2"  # понимают Exchange 2010 SP2, 2013, 2016, 2019
URL_PROP = 'PropertySetId="c11ff724-aa03-4555-9952-8fa248a11c3e" PropertyName="CalendarUrl" PropertyType="String"'
DEFAULT_FOLDER = "calendar"

# IANA → идентификаторы часовых поясов Windows (Exchange понимает только их)
IANA_TO_WINDOWS = {
    "Europe/Kaliningrad": "Kaliningrad Standard Time",
    "Europe/Moscow": "Russian Standard Time",
    "Europe/Samara": "Russia Time Zone 3",
    "Europe/Volgograd": "Volgograd Standard Time",
    "Asia/Yekaterinburg": "Ekaterinburg Standard Time",
    "Asia/Omsk": "Omsk Standard Time",
    "Asia/Novosibirsk": "N. Central Asia Standard Time",
    "Asia/Krasnoyarsk": "North Asia Standard Time",
    "Asia/Irkutsk": "North Asia East Standard Time",
    "Asia/Yakutsk": "Yakutsk Standard Time",
    "Asia/Vladivostok": "Vladivostok Standard Time",
    "Asia/Magadan": "Magadan Standard Time",
    "Asia/Kamchatka": "Russia Time Zone 11",
    "Europe/Minsk": "Belarus Standard Time",
    "Asia/Almaty": "Central Asia Standard Time",
    "Asia/Tashkent": "West Asia Standard Time",
    "Europe/London": "GMT Standard Time",
    "Europe/Berlin": "W. Europe Standard Time",
    "America/New_York": "Eastern Standard Time",
    "UTC": "UTC",
}

SEND_MODES = {"none": "SendToNone", "all": "SendToAllAndSaveCopy", "changed": "SendToChangedAndSaveCopy"}

CONFLICT_CODES = {"ErrorIrresolvableConflict", "ErrorStaleObject", "ErrorChangeKeyRequiredForWriteOperations"}
NOT_FOUND_CODES = {"ErrorItemNotFound", "ErrorInvalidIdMalformed", "ErrorInvalidIdNotAnItemAttachmentId"}


def normalize_ews_url(value: str) -> str:
    """'email.yanao.ru' → 'https://email.yanao.ru/EWS/Exchange.asmx'."""
    value = (value or "").strip()
    if not value:
        return ""
    if "://" not in value:
        value = "https://" + value
    if not re.search(r"/ews/exchange\.asmx$", value, re.IGNORECASE):
        value = value.rstrip("/") + "/EWS/Exchange.asmx"
    return value


def windows_tz(tz) -> str:
    return IANA_TO_WINDOWS.get(getattr(tz, "key", str(tz)), "UTC")


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def _text(el, path) -> str:
    found = el.find(path, NS)
    return (found.text or "") if found is not None else ""


_XML_BAD = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ufffe\uffff]")


def _x(text) -> str:
    """Текст для XML: экранирование и удаление символов, недопустимых в XML 1.0 (иначе Exchange отвергнет запрос)."""
    return escape(_XML_BAD.sub("", text or ""))


def _norm_body(text: str) -> str:
    return (text or "").replace("\r\n", "\n").replace("\r", "\n").strip()


LINK_SEPARATOR = "\n\n"  # между описанием и ссылкой — два перевода строки
_URL_RE = re.compile(r"^[a-z][a-z0-9+.-]*://\S+$", re.IGNORECASE)


def compose_body(description: str, url: str) -> str:
    """Текст события в Exchange: описание, затем через пустую строку — ссылка (видна в Outlook и OWA)."""
    description = _norm_body(description)
    url = (url or "").strip()
    if not url:
        return description
    return f"{description}{LINK_SEPARATOR}{url}" if description else url


def split_body(body: str, marker_url: str) -> tuple[str, str]:
    """
    Обратно: (описание, ссылка). Ссылкой считается последний абзац, только если у события есть отметка
    CalendarUrl (событие с ссылкой создано или изменено календарём) — так описание, которое само
    заканчивается адресом, не теряется. Ссылку можно поправить прямо в Outlook (последний абзац текста).
    Если в тексте ссылки нет (событие записано старой версией календаря, где ссылка была скрытой),
    берётся ссылка из отметки — она не теряется.
    """
    body = _norm_body(body)
    if not marker_url:
        return body, ""
    head, sep, last = body.rpartition(LINK_SEPARATOR)
    last = last.strip()
    if _URL_RE.match(last):
        return (head.strip() if sep else ""), last
    return body, marker_url.strip()


def link_missing_in_body(body: str, marker_url: str) -> bool:
    """Ссылка есть только в скрытой отметке, а в тексте её нет — событие нужно перезаписать."""
    if not marker_url:
        return False
    last = _norm_body(body).rpartition(LINK_SEPARATOR)[2].strip()
    return not _URL_RE.match(last)


def envelope(body: str, tz_id: str = "UTC") -> str:
    return (
        '<?xml version="1.0" encoding="utf-8"?>'
        '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/" '
        'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types" '
        'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages">'
        f'<soap:Header><t:RequestServerVersion Version="{SERVER_VERSION}"/>'
        f'<t:TimeZoneContext><t:TimeZoneDefinition Id="{escape(tz_id)}"/></t:TimeZoneContext></soap:Header>'
        f"<soap:Body>{body}</soap:Body></soap:Envelope>"
    )


def _folder_xml(folder_id: str) -> str:
    if not folder_id or folder_id == DEFAULT_FOLDER:
        return f'<t:DistinguishedFolderId Id="{DEFAULT_FOLDER}"/>'
    return f'<t:FolderId Id="{escape(folder_id, {chr(34): "&quot;"})}"/>'


def _item_id_xml(remote_id: str, change_key: str = "") -> str:
    q = {'"': "&quot;"}
    ck = f' ChangeKey="{escape(change_key, q)}"' if change_key else ""
    return f'<t:ItemId Id="{escape(remote_id, q)}"{ck}/>'


def _times(data: EventData, tz):
    """Start/End в UTC. Событие на весь день: полночь первого дня — полночь дня после последнего (в tz)."""
    if data.all_day:
        start_d = data.start.astimezone(tz).date()
        end_d = (data.end or data.start).astimezone(tz).date() + timedelta(days=1)
        start = datetime.combine(start_d, datetime.min.time(), tzinfo=tz)
        end = datetime.combine(end_d, datetime.min.time(), tzinfo=tz)
        return _utc(start), _utc(end)
    return _utc(data.start), _utc(data.end or data.start)


def attendees_xml(emails) -> str:
    return "".join(
        f"<t:Attendee><t:Mailbox><t:EmailAddress>{_x(e)}</t:EmailAddress></t:Mailbox></t:Attendee>" for e in emails
    )


def calendar_item_xml(data: EventData, tz, with_uid=True) -> str:
    """CalendarItem для CreateItem. Порядок элементов важен: Exchange проверяет его по схеме."""
    start, end = _times(data, tz)
    tz_id = escape(windows_tz(tz))
    parts = [f"<t:Subject>{_x(data.title)}</t:Subject>"]
    body = compose_body(data.description, data.url)
    if body:
        parts.append(f'<t:Body BodyType="Text">{_x(body)}</t:Body>')
    if data.reminder is not None and data.reminder >= 0:
        parts.append("<t:ReminderIsSet>true</t:ReminderIsSet>")
        parts.append(f"<t:ReminderMinutesBeforeStart>{int(data.reminder)}</t:ReminderMinutesBeforeStart>")
    else:
        parts.append("<t:ReminderIsSet>false</t:ReminderIsSet>")
    if data.url:  # пустое расширенное свойство не создаём
        parts.append(f"<t:ExtendedProperty><t:ExtendedFieldURI {URL_PROP}/><t:Value>{_x(data.url)}</t:Value></t:ExtendedProperty>")
    if with_uid and data.uid:
        parts.append(f"<t:UID>{_x(data.uid)}</t:UID>")
    parts.append(f"<t:Start>{start}</t:Start><t:End>{end}</t:End>")
    parts.append(f"<t:IsAllDayEvent>{'true' if data.all_day else 'false'}</t:IsAllDayEvent>")
    parts.append("<t:LegacyFreeBusyStatus>Busy</t:LegacyFreeBusyStatus>")
    if data.location:
        parts.append(f"<t:Location>{_x(data.location)}</t:Location>")
    if data.attendees:
        parts.append(f"<t:RequiredAttendees>{attendees_xml(data.attendees)}</t:RequiredAttendees>")
    parts.append(f'<t:StartTimeZone Id="{tz_id}"/><t:EndTimeZone Id="{tz_id}"/>')
    return "<t:CalendarItem>" + "".join(parts) + "</t:CalendarItem>"


def _set(field_uri: str, inner: str) -> str:
    return f'<t:SetItemField><t:FieldURI FieldURI="{field_uri}"/><t:CalendarItem>{inner}</t:CalendarItem></t:SetItemField>'


def _delete(field_uri: str) -> str:
    return f'<t:DeleteItemField><t:FieldURI FieldURI="{field_uri}"/></t:DeleteItemField>'


def update_fields_xml(data: EventData, tz) -> str:
    start, end = _times(data, tz)
    body = compose_body(data.description, data.url)
    tz_id = escape(windows_tz(tz))
    ups = [
        _set("calendar:StartTimeZone", f'<t:StartTimeZone Id="{tz_id}"/>'),
        _set("calendar:EndTimeZone", f'<t:EndTimeZone Id="{tz_id}"/>'),
        _set("item:Subject", f"<t:Subject>{_x(data.title)}</t:Subject>"),
        _set("calendar:Start", f"<t:Start>{start}</t:Start>"),
        _set("calendar:End", f"<t:End>{end}</t:End>"),
        _set("calendar:IsAllDayEvent", f"<t:IsAllDayEvent>{'true' if data.all_day else 'false'}</t:IsAllDayEvent>"),
        _set("item:Body", f'<t:Body BodyType="Text">{_x(body)}</t:Body>') if body else _delete("item:Body"),
        _set("calendar:Location", f"<t:Location>{_x(data.location)}</t:Location>") if data.location else _delete("calendar:Location"),
        (
            f"<t:SetItemField><t:ExtendedFieldURI {URL_PROP}/><t:CalendarItem><t:ExtendedProperty><t:ExtendedFieldURI {URL_PROP}/>"
            f"<t:Value>{_x(data.url)}</t:Value></t:ExtendedProperty></t:CalendarItem></t:SetItemField>"
            if data.url
            else f"<t:DeleteItemField><t:ExtendedFieldURI {URL_PROP}/></t:DeleteItemField>"
        ),
    ]
    if data.reminder is not None:  # напоминание: включено за N минут или выключено
        on = data.reminder >= 0
        ups.append(_set("item:ReminderIsSet", f"<t:ReminderIsSet>{'true' if on else 'false'}</t:ReminderIsSet>"))
        if on:
            ups.append(_set("item:ReminderMinutesBeforeStart",
                            f"<t:ReminderMinutesBeforeStart>{int(data.reminder)}</t:ReminderMinutesBeforeStart>"))
    if data.attendees:
        ups.append(_set("calendar:RequiredAttendees", f"<t:RequiredAttendees>{attendees_xml(data.attendees)}</t:RequiredAttendees>"))
    elif data.attendees is not None:
        ups.append(_delete("calendar:RequiredAttendees"))
    return "".join(ups)


class ExchangeProvider(Provider):
    """
    credentials: {"password": ..., "auth": "ntlm"|"basic"} — способ входа определяется при подключении.
    calendar_id: "calendar" (основной календарь) или FolderId вложенного календаря.
    """

    def __init__(self, server_url, username, password, calendar_id="", default_tz=timezone.utc, auth=""):
        self.url = normalize_ews_url(server_url)
        if not self.url:
            raise ProviderError("Не указан адрес сервера Exchange.")
        self.username = (username or "").strip()
        self.password = password or ""
        self.calendar_id = calendar_id or DEFAULT_FOLDER
        self.default_tz = default_tz
        self.auth_kind = auth or ""
        self.session = requests.Session()
        self.session.headers.update({"Content-Type": "text/xml; charset=utf-8", "User-Agent": "CalendarDjango/1.0 (EWS)"})

    # ---------------------------------------------------------------- вход
    def _make_auth(self, kind):
        if kind == "ntlm":
            try:
                from requests_ntlm import HttpNtlmAuth
            except ImportError as exc:
                raise ProviderError("Сервер Exchange требует вход NTLM: установите пакет requests_ntlm (pip install requests_ntlm).") from exc
            return HttpNtlmAuth(self.username, self.password)
        return (self.username, self.password)

    def detect_auth(self) -> str:
        """Какой способ входа предлагает сервер: NTLM (предпочтительно) или Basic."""
        try:
            resp = self.session.post(self.url, data=b"", timeout=TIMEOUT, allow_redirects=False)
        except requests.RequestException as exc:
            raise ProviderError(f"Сервер Exchange недоступен: {exc.__class__.__name__}") from exc
        offered = " ".join(resp.headers.get("WWW-Authenticate", "").split(",")).lower()
        if resp.status_code == 404:
            raise ProviderError("По этому адресу нет службы EWS. Проверьте адрес сервера (обычно https://<сервер>/EWS/Exchange.asmx).")
        if "ntlm" in offered or "negotiate" in offered:
            try:
                import requests_ntlm  # noqa: F401

                return "ntlm"
            except ImportError:
                if "basic" in offered:
                    return "basic"
                raise ProviderError("Сервер Exchange требует вход NTLM: установите пакет requests_ntlm (pip install requests_ntlm).")
        return "basic"

    # ---------------------------------------------------------------- SOAP
    def _call(self, body: str):
        if not self.auth_kind:
            self.auth_kind = self.detect_auth()
        self.session.auth = self._make_auth(self.auth_kind)
        try:
            resp = self.session.post(self.url, data=envelope(body, windows_tz(self.default_tz)).encode("utf-8"), timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise ProviderError(f"Сервер Exchange недоступен: {exc.__class__.__name__}") from exc
        if resp.status_code in (401, 403):
            raise AuthError("Exchange отклонил логин или пароль. Логин — DOMAIN\\пользователь или адрес почты.")
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ProviderError(f"Сервер Exchange ответил {resp.status_code}, но не XML. Проверьте адрес EWS.") from exc
        fault = root.find(".//s:Fault", NS)
        if fault is not None:
            raise ProviderError(f"Ошибка Exchange: {fault.findtext('faultstring', default='SOAP Fault').strip()}")
        if resp.status_code != 200:
            raise ProviderError(f"Сервер Exchange ответил {resp.status_code}.")
        return root

    @staticmethod
    def _messages(root):
        return [el for el in root.iter() if el.tag.endswith("ResponseMessage") and el.get("ResponseClass")]

    def _check(self, msg, allow=()):
        if msg.get("ResponseClass") == "Error":
            code = _text(msg, "m:ResponseCode")
            if code in allow:
                return code
            text = _text(msg, "m:MessageText") or code
            if code in CONFLICT_CODES:
                raise ConflictError(f"Событие в Exchange изменилось: {text}")
            if code in ("ErrorAccessDenied", "ErrorImpersonateUserDenied"):
                raise AuthError(f"Нет доступа к календарю Exchange: {text}")
            raise ProviderError(f"Ошибка Exchange ({code}): {text}")
        return None

    # ---------------------------------------------------------------- календари
    def list_calendars(self):
        root = self._call(
            "<m:GetFolder><m:FolderShape><t:BaseShape>Default</t:BaseShape></m:FolderShape>"
            f"<m:FolderIds>{_folder_xml(DEFAULT_FOLDER)}</m:FolderIds></m:GetFolder>"
        )
        for msg in self._messages(root):
            self._check(msg)
        name = _text(root, ".//t:CalendarFolder/t:DisplayName") or "Календарь"
        result = [{"id": DEFAULT_FOLDER, "name": f"{name} (основной)", "color": "", "read_only": False}]
        root = self._call(
            '<m:FindFolder Traversal="Deep"><m:FolderShape><t:BaseShape>Default</t:BaseShape></m:FolderShape>'
            f"<m:ParentFolderIds>{_folder_xml(DEFAULT_FOLDER)}</m:ParentFolderIds></m:FindFolder>"
        )
        for msg in self._messages(root):
            if self._check(msg, allow=("ErrorFolderNotFound",)):
                continue
            for f in msg.iter(f"{{{NS['t']}}}CalendarFolder"):
                fid = f.find("t:FolderId", NS)
                if fid is not None:
                    result.append({"id": fid.get("Id"), "name": _text(f, "t:DisplayName"), "color": "", "read_only": False})
        return result

    # ---------------------------------------------------------------- события
    def list_events(self, start, end):
        root = self._call(
            '<m:FindItem Traversal="Shallow"><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:AdditionalProperties>'
            '<t:FieldURI FieldURI="item:Subject"/><t:FieldURI FieldURI="calendar:Start"/><t:FieldURI FieldURI="calendar:End"/>'
            '<t:FieldURI FieldURI="calendar:IsAllDayEvent"/><t:FieldURI FieldURI="calendar:Location"/>'
            '<t:FieldURI FieldURI="calendar:UID"/><t:FieldURI FieldURI="calendar:CalendarItemType"/>'
            '<t:FieldURI FieldURI="calendar:IsCancelled"/><t:FieldURI FieldURI="item:LastModifiedTime"/>'
            '<t:FieldURI FieldURI="item:ReminderIsSet"/><t:FieldURI FieldURI="item:ReminderMinutesBeforeStart"/>'
            f"<t:ExtendedFieldURI {URL_PROP}/>"
            "</t:AdditionalProperties></m:ItemShape>"
            f'<m:CalendarView MaxEntriesReturned="2000" StartDate="{_utc(start)}" EndDate="{_utc(end)}"/>'
            f"<m:ParentFolderIds>{_folder_xml(self.calendar_id)}</m:ParentFolderIds></m:FindItem>"
        )
        items = []
        for msg in self._messages(root):
            self._check(msg)
            for el in msg.iter(f"{{{NS['t']}}}CalendarItem"):
                ev = self._event(el)
                if ev:
                    items.append(ev)
        # FindItem не возвращает текст, участников и вложения — берём их отдельным запросом пачками
        details = self._details([i.remote_id for i in items])
        for i in items:
            d = details.get(i.remote_id, {"body": "", "attendees": [], "attachments": []})
            # url пока — отметка CalendarUrl; сама ссылка берётся из текста (её могли поправить в Outlook)
            marker = i.data.url
            i.data.description, i.data.url = split_body(d["body"], marker)
            # ссылки нет в тексте (старая запись) или её поправили в Outlook — перезапишем, чтобы текст и отметка совпадали
            i.needs_rewrite = bool(marker) and (link_missing_in_body(d["body"], marker) or i.data.url != marker.strip())
            i.data.attendees = d["attendees"]
            i.data.attachments = d["attachments"]
        return items

    def _event(self, el) -> RemoteItem | None:
        if _text(el, "t:IsCancelled") == "true":
            return None
        item_id = el.find("t:ItemId", NS)
        if item_id is None or not _text(el, "t:Start"):
            return None
        all_day = _text(el, "t:IsAllDayEvent") == "true"
        start, end = _parse_dt(_text(el, "t:Start")), _parse_dt(_text(el, "t:End") or _text(el, "t:Start"))
        if all_day:
            start = datetime.combine(start.astimezone(self.default_tz).date(), datetime.min.time(), tzinfo=self.default_tz)
            end = max(end.astimezone(self.default_tz) - timedelta(minutes=1), start)
        uid = _text(el, "t:UID") or item_id.get("Id")
        kind = _text(el, "t:CalendarItemType")
        read_only = kind in ("Occurrence", "Exception")
        if read_only:
            uid = f"{uid}#{start.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
        url = ""
        for ext in el.findall("t:ExtendedProperty", NS):
            url = _text(ext, "t:Value")
        modified = _text(el, "t:LastModifiedTime")
        data = EventData(
            uid=uid,
            title=_text(el, "t:Subject"),
            location=_text(el, "t:Location"),
            url=url,
            start=start,
            end=end,
            all_day=all_day,
            last_modified=_parse_dt(modified) if modified else None,
            read_only=read_only,
            reminder=self._reminder(el),
        )
        return RemoteItem(remote_id=item_id.get("Id"), etag=item_id.get("ChangeKey", ""), data=data)

    @staticmethod
    def _reminder(el):
        is_set = _text(el, "t:ReminderIsSet")
        if not is_set:
            return None  # сервер не сообщил — напоминание у нас не трогаем
        minutes = _text(el, "t:ReminderMinutesBeforeStart")
        return int(minutes) if is_set == "true" and minutes.isdigit() else -1

    def _details(self, ids):
        """
        Текст, участники и список вложений — FindItem их не возвращает, берём пачками через GetItem.
        {item_id: {"body": str, "attendees": [e-mail], "attachments": [{"id", "name", "size", "content_type"}]}}
        """
        result = {}
        for i in range(0, len(ids), 100):
            chunk = ids[i : i + 100]
            if not chunk:
                continue
            root = self._call(
                '<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:BodyType>Text</t:BodyType>'
                '<t:AdditionalProperties><t:FieldURI FieldURI="item:Body"/><t:FieldURI FieldURI="item:Attachments"/>'
                '<t:FieldURI FieldURI="calendar:RequiredAttendees"/><t:FieldURI FieldURI="calendar:OptionalAttendees"/>'
                "</t:AdditionalProperties></m:ItemShape>"
                f"<m:ItemIds>{''.join(_item_id_xml(x) for x in chunk)}</m:ItemIds></m:GetItem>"
            )
            for msg in self._messages(root):
                if self._check(msg, allow=NOT_FOUND_CODES):
                    continue
                for el in msg.iter(f"{{{NS['t']}}}CalendarItem"):
                    iid = el.find("t:ItemId", NS)
                    if iid is None:
                        continue
                    attendees = []
                    for tag in ("t:RequiredAttendees", "t:OptionalAttendees"):
                        for mb in el.findall(f"{tag}/t:Attendee/t:Mailbox", NS):
                            email = _text(mb, "t:EmailAddress").strip()
                            if "@" in email and email.lower() not in (a.lower() for a in attendees):
                                attendees.append(email)  # адрес вида /O=… (RoutingType EX) пропускаем
                    attachments = []
                    for fa in el.findall("t:Attachments/t:FileAttachment", NS):
                        if _text(fa, "t:IsInline") == "true":
                            continue  # картинки внутри текста письма — не файлы
                        aid = fa.find("t:AttachmentId", NS)
                        if aid is None:
                            continue
                        size = _text(fa, "t:Size")
                        attachments.append({
                            "id": aid.get("Id"),
                            "name": _text(fa, "t:Name") or "файл",
                            "size": int(size) if size.isdigit() else 0,
                            "content_type": _text(fa, "t:ContentType"),
                        })
                    result[iid.get("Id")] = {
                        "body": _norm_body(_text(el, "t:Body")),
                        "attendees": attendees,
                        "attachments": attachments,
                    }
        return result

    def add_attachment(self, remote_id: str, name: str, content: bytes, content_type: str = "") -> tuple[str, str]:
        """Прикрепляет файл к событию. Возвращает (id вложения, новый ChangeKey события)."""
        ctype = f"<t:ContentType>{_x(content_type)}</t:ContentType>" if content_type else ""
        root = self._call(
            f"<m:CreateAttachment><m:ParentItemId Id=\"{escape(remote_id, {chr(34): '&quot;'})}\"/><m:Attachments>"
            f"<t:FileAttachment><t:Name>{_x(name)}</t:Name>{ctype}"
            f"<t:Content>{base64.b64encode(content).decode('ascii')}</t:Content></t:FileAttachment>"
            "</m:Attachments></m:CreateAttachment>"
        )
        for msg in self._messages(root):
            self._check(msg)
            aid = msg.find(".//t:AttachmentId", NS)
            if aid is not None:
                return aid.get("Id"), aid.get("RootItemChangeKey", "")
        raise ProviderError("Exchange не вернул идентификатор вложения.")

    def delete_attachment(self, attachment_id: str) -> str:
        """Удаляет файл из события. Возвращает новый ChangeKey события (или пустую строку)."""
        root = self._call(
            "<m:DeleteAttachment><m:AttachmentIds>"
            f'<t:AttachmentId Id="{escape(attachment_id, {chr(34): "&quot;"})}"/>'
            "</m:AttachmentIds></m:DeleteAttachment>"
        )
        for msg in self._messages(root):
            if self._check(msg, allow=("ErrorAttachmentNotFound", "ErrorItemNotFound", "ErrorInvalidIdMalformed")):
                return ""
            root_id = msg.find(".//m:RootItemId", NS)
            if root_id is not None:
                return root_id.get("RootItemChangeKey", "")
        return ""

    def inspect_item(self, remote_id: str) -> dict:
        """Как событие выглядит в Exchange (для диагностики): тема, текст, отметка ссылки, вложения, участники."""
        root = self._call(
            '<m:GetItem><m:ItemShape><t:BaseShape>IdOnly</t:BaseShape><t:BodyType>Text</t:BodyType>'
            '<t:AdditionalProperties><t:FieldURI FieldURI="item:Subject"/><t:FieldURI FieldURI="item:Body"/>'
            '<t:FieldURI FieldURI="item:Attachments"/><t:FieldURI FieldURI="calendar:RequiredAttendees"/>'
            f"<t:ExtendedFieldURI {URL_PROP}/></t:AdditionalProperties></m:ItemShape>"
            f"<m:ItemIds>{_item_id_xml(remote_id)}</m:ItemIds></m:GetItem>"
        )
        for msg in self._messages(root):
            self._check(msg)
            el = msg.find(".//t:CalendarItem", NS)
            if el is None:
                break
            marker = ""
            for ext in el.findall("t:ExtendedProperty", NS):
                marker = _text(ext, "t:Value")
            return {
                "subject": _text(el, "t:Subject"),
                "body": _norm_body(_text(el, "t:Body")),
                "link_marker": marker,
                "attachments": [_text(fa, "t:Name") for fa in el.findall("t:Attachments/t:FileAttachment", NS)],
                "attendees": [_text(mb, "t:EmailAddress") for mb in el.findall("t:RequiredAttendees/t:Attendee/t:Mailbox", NS)],
            }
        raise ProviderError("Событие не найдено в Exchange.")

    def get_attachment(self, attachment_id: str) -> bytes:
        """Содержимое файла-вложения."""
        root = self._call(
            "<m:GetAttachment><m:AttachmentIds>"
            f'<t:AttachmentId Id="{escape(attachment_id, {chr(34): "&quot;"})}"/>'
            "</m:AttachmentIds></m:GetAttachment>"
        )
        for msg in self._messages(root):
            self._check(msg)
            content = msg.find(".//t:FileAttachment/t:Content", NS)
            if content is not None:
                return base64.b64decode(content.text or "")
        raise ProviderError("Exchange не вернул содержимое вложения.")

    def _item_id_from(self, root):
        for msg in self._messages(root):
            self._check(msg)
            el = msg.find(".//t:ItemId", NS)
            if el is not None:
                return el.get("Id"), el.get("ChangeKey", "")
        raise ProviderError("Exchange не вернул идентификатор события.")

    def create_event(self, data: EventData) -> RemoteItem:
        # с участниками — собрание: Exchange разошлёт приглашения и положит встречу в их календари
        mode = SEND_MODES["all"] if data.attendees else SEND_MODES["none"]
        root = self._call(
            f'<m:CreateItem SendMeetingInvitations="{mode}">'
            f"<m:SavedItemFolderId>{_folder_xml(self.calendar_id)}</m:SavedItemFolderId>"
            f"<m:Items>{calendar_item_xml(data, self.default_tz)}</m:Items></m:CreateItem>"
        )
        item_id, change_key = self._item_id_from(root)
        return RemoteItem(remote_id=item_id, etag=change_key, data=data)

    def update_event(self, remote_id, etag, data, send=None):
        """
        send: "all" — обновление всем участникам; "changed" — только добавленным (приглашение) и удалённым (отмена);
        по умолчанию участникам ничего не отправляется.
        """
        mode = SEND_MODES.get(send or "none", SEND_MODES["none"])
        root = self._call(
            f'<m:UpdateItem ConflictResolution="AutoResolve" SendMeetingInvitationsOrCancellations="{mode}">'
            f"<m:ItemChanges><t:ItemChange>{_item_id_xml(remote_id, etag)}"
            f"<t:Updates>{update_fields_xml(data, self.default_tz)}</t:Updates></t:ItemChange></m:ItemChanges></m:UpdateItem>"
        )
        return self._item_id_from(root)[1]

    def delete_event(self, remote_id, etag, notify=False):
        """notify — разослать участникам собрания отмену (встреча исчезнет из их календарей)."""
        mode = SEND_MODES["all"] if notify else SEND_MODES["none"]
        root = self._call(
            f'<m:DeleteItem DeleteType="MoveToDeletedItems" SendMeetingCancellations="{mode}">'
            f"<m:ItemIds>{_item_id_xml(remote_id)}</m:ItemIds></m:DeleteItem>"
        )
        for msg in self._messages(root):
            self._check(msg, allow=NOT_FOUND_CODES)
