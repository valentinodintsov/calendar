"""
Адаптер CalDAV (RFC 4791) на requests: Яндекс, Mail.ru, Rambler и любой другой CalDAV-сервер.
Вход — логин и пароль приложения (HTTP Basic).
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from datetime import timezone
from urllib.parse import quote, urljoin, urlparse

import requests

from ..ics import EventData, expand_all, parse_vevents, to_ics
from .base import TIMEOUT, AuthError, ConflictError, Provider, ProviderError, RemoteItem

NS = {
    "d": "DAV:",
    "c": "urn:ietf:params:xml:ns:caldav",
    "cs": "http://calendarserver.org/ns/",
    "ic": "http://apple.com/ns/ical/",
}

PROPFIND_PRINCIPAL = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:current-user-principal/></d:prop></d:propfind>"""

PROPFIND_HOME = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><c:calendar-home-set/></d:prop>
</d:propfind>"""

PROPFIND_CALENDARS = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav"
            xmlns:cs="http://calendarserver.org/ns/" xmlns:ic="http://apple.com/ns/ical/">
  <d:prop>
    <d:resourcetype/><d:displayname/><d:current-user-privilege-set/>
    <c:supported-calendar-component-set/><cs:getctag/><ic:calendar-color/>
  </d:prop>
</d:propfind>"""

REPORT_EVENTS = """<?xml version="1.0" encoding="utf-8"?>
<c:calendar-query xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav">
  <d:prop><d:getetag/><c:calendar-data/></d:prop>
  <c:filter>
    <c:comp-filter name="VCALENDAR">
      <c:comp-filter name="VEVENT"><c:time-range start="{start}" end="{end}"/></c:comp-filter>
    </c:comp-filter>
  </c:filter>
</c:calendar-query>"""

PROPFIND_ETAG = """<?xml version="1.0" encoding="utf-8"?>
<d:propfind xmlns:d="DAV:"><d:prop><d:getetag/></d:prop></d:propfind>"""


def _utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


class CalDAVProvider(Provider):
    def __init__(self, server_url: str, username: str, password: str, calendar_url: str = "", default_tz=timezone.utc):
        self.server_url = server_url.strip()
        if self.server_url and "://" not in self.server_url:
            self.server_url = "https://" + self.server_url
        self.calendar_url = calendar_url
        self.default_tz = default_tz
        self.session = requests.Session()
        self.session.auth = (username, password)
        self.session.headers["User-Agent"] = "CalendarDjango/1.0 (CalDAV)"

    # ---------------------------------------------------------------- HTTP
    def _request(self, method, url, body=None, headers=None, ok=(200, 201, 204, 207)):
        hdrs = {"Content-Type": "application/xml; charset=utf-8"} if body and not (headers or {}).get("Content-Type") else {}
        hdrs.update(headers or {})
        try:
            resp = self.session.request(
                method, url, data=body.encode("utf-8") if isinstance(body, str) else body,
                headers=hdrs, timeout=TIMEOUT, allow_redirects=False,
            )
            # Редиректы обрабатываем сами: requests превращает PROPFIND в GET на 301/302
            hops = 0
            while resp.status_code in (301, 302, 307, 308) and hops < 5:
                url = urljoin(url, resp.headers.get("Location", ""))
                resp = self.session.request(
                    method, url, data=body.encode("utf-8") if isinstance(body, str) else body,
                    headers=hdrs, timeout=TIMEOUT, allow_redirects=False,
                )
                hops += 1
        except requests.RequestException as exc:
            raise ProviderError(f"Сервер календаря недоступен: {exc.__class__.__name__}") from exc
        if resp.status_code in (401, 403):
            raise AuthError("Сервер отклонил логин или пароль. Для Яндекса и Mail.ru нужен пароль приложения.")
        if resp.status_code == 412:
            raise ConflictError("Событие на сервере изменилось.")
        if resp.status_code not in ok:
            raise ProviderError(f"Сервер календаря ответил {resp.status_code} на {method}.")
        resp.final_url = url
        return resp

    def _propfind(self, url, body, depth="0"):
        resp = self._request("PROPFIND", url, body, {"Depth": depth})
        try:
            return resp.final_url, ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ProviderError("Сервер вернул некорректный ответ (не XML). Проверьте адрес сервера.") from exc

    @staticmethod
    def _responses(root):
        for r in root.findall("d:response", NS):
            href = r.findtext("d:href", default="", namespaces=NS).strip()
            props = {}
            for ps in r.findall("d:propstat", NS):
                status = ps.findtext("d:status", default="", namespaces=NS)
                if " 200 " in status or status.endswith(" 200"):
                    prop = ps.find("d:prop", NS)
                    if prop is not None:
                        for child in prop:
                            props[child.tag] = child
            yield href, props

    @staticmethod
    def _href_in(el):
        if el is None:
            return None
        h = el.find("d:href", NS)
        return h.text.strip() if h is not None and h.text else None

    # ---------------------------------------------------------------- обнаружение календарей
    def list_calendars(self):
        base = self.server_url
        # 1) сам адрес может быть календарём (Mail.ru даёт прямую ссылку на календарь)
        cals = self._calendars_at(base, depth="0")
        if cals:
            return cals
        # 2) principal → calendar-home-set → календари
        principal = None
        for candidate in (base, urljoin(base, "/.well-known/caldav")):
            try:
                url, root = self._propfind(candidate, PROPFIND_PRINCIPAL)
            except AuthError:
                raise
            except ProviderError:
                continue
            for _, props in self._responses(root):
                principal = self._href_in(props.get("{DAV:}current-user-principal"))
                if principal:
                    principal = urljoin(url, principal)
                    break
            if principal:
                break
        home = None
        if principal:
            url, root = self._propfind(principal, PROPFIND_HOME)
            for _, props in self._responses(root):
                home = self._href_in(props.get("{urn:ietf:params:xml:ns:caldav}calendar-home-set"))
                if home:
                    home = urljoin(url, home)
                    break
        cals = self._calendars_at(home or base, depth="1")
        if not cals:
            raise ProviderError("На сервере не найдено календарей. Проверьте адрес сервера.")
        return cals

    def _calendars_at(self, url, depth):
        try:
            final, root = self._propfind(url, PROPFIND_CALENDARS, depth)
        except AuthError:
            raise
        except ProviderError:
            return []
        result = []
        for href, props in self._responses(root):
            rt = props.get("{DAV:}resourcetype")
            if rt is None or rt.find("c:calendar", NS) is None:
                continue
            comps = props.get("{urn:ietf:params:xml:ns:caldav}supported-calendar-component-set")
            if comps is not None and len(comps) and not any(c.get("name") == "VEVENT" for c in comps):
                continue  # календарь задач (VTODO), не событий
            privs = props.get("{DAV:}current-user-privilege-set")
            read_only = False
            if privs is not None and len(privs):
                names = {p[0].tag for p in privs.findall("d:privilege", NS) if len(p)}
                read_only = not ({"{DAV:}write", "{DAV:}write-content", "{DAV:}all"} & names)
            name_el = props.get("{DAV:}displayname")
            color_el = props.get("{http://apple.com/ns/ical/}calendar-color")
            full = urljoin(final, href)
            result.append(
                {
                    "id": full,
                    "name": (name_el.text or "").strip() if name_el is not None else "",
                    "color": (color_el.text or "")[:7] if color_el is not None else "",
                    "read_only": read_only,
                }
            )
        for cal in result:
            cal["name"] = cal["name"] or urlparse(cal["id"]).path.rstrip("/").rsplit("/", 1)[-1]
        return result

    # ---------------------------------------------------------------- события
    def _require_calendar(self):
        if not self.calendar_url:
            raise ProviderError("Не выбран календарь.")
        return self.calendar_url

    def list_events(self, start, end):
        url = self._require_calendar()
        resp = self._request("REPORT", url, REPORT_EVENTS.format(start=_utc(start), end=_utc(end)), {"Depth": "1"})
        try:
            root = ET.fromstring(resp.content)
        except ET.ParseError as exc:
            raise ProviderError("Сервер вернул некорректный список событий.") from exc
        items = []
        for href, props in self._responses(root):
            data_el = props.get("{urn:ietf:params:xml:ns:caldav}calendar-data")
            if data_el is None or not (data_el.text or "").strip():
                continue
            etag = (props.get("{DAV:}getetag").text or "").strip() if props.get("{DAV:}getetag") is not None else ""
            full_href = urljoin(resp.final_url, href)
            for ev in expand_all(parse_vevents(data_el.text, self.default_tz), start, end, self.default_tz):
                items.append(RemoteItem(remote_id=full_href, etag=etag, data=ev))
        return items

    def _etag_of(self, resp, href):
        etag = resp.headers.get("ETag")
        if etag:
            return etag
        # не все серверы возвращают ETag в ответе на PUT — запрашиваем отдельно
        _, root = self._propfind(href, PROPFIND_ETAG)
        for _, props in self._responses(root):
            el = props.get("{DAV:}getetag")
            if el is not None and el.text:
                return el.text.strip()
        return ""

    def create_event(self, data: EventData) -> RemoteItem:
        base = self._require_calendar()
        if not base.endswith("/"):
            base += "/"
        href = urljoin(base, quote(data.uid.replace("/", "_"), safe="@._-") + ".ics")
        resp = self._request(
            "PUT", href, to_ics(data, tz=self.default_tz),
            {"Content-Type": "text/calendar; charset=utf-8", "If-None-Match": "*"},
        )
        return RemoteItem(remote_id=href, etag=self._etag_of(resp, href), data=data)

    def update_event(self, remote_id, etag, data):
        headers = {"Content-Type": "text/calendar; charset=utf-8"}
        if etag:
            headers["If-Match"] = etag
        resp = self._request("PUT", remote_id, to_ics(data, tz=self.default_tz), headers)
        return self._etag_of(resp, remote_id)

    def delete_event(self, remote_id, etag):
        headers = {"If-Match": etag} if etag else {}
        self._request("DELETE", remote_id, headers=headers, ok=(200, 204, 404, 410))
