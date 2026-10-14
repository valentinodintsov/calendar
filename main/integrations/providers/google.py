"""
Адаптер Google Calendar API v3 на requests (без google-api-python-client).
Вход — OAuth 2.0: в базе хранится refresh-токен, access-токен обновляется автоматически.
"""

from __future__ import annotations

import time as _time
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote, urlencode

import requests

from ..ics import EventData
from .base import TIMEOUT, AuthError, ConflictError, Provider, ProviderError, RemoteItem

AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
TOKEN_URL = "https://oauth2.googleapis.com/token"
API = "https://www.googleapis.com/calendar/v3"
SCOPE = "https://www.googleapis.com/auth/calendar"


# ---------------------------------------------------------------- OAuth


def authorization_url(client_id: str, redirect_uri: str, state: str) -> str:
    return AUTH_URL + "?" + urlencode(
        {
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": SCOPE,
            "access_type": "offline",  # нужен refresh-токен
            "prompt": "consent",  # чтобы refresh-токен выдавался и при повторном подключении
            "include_granted_scopes": "true",
            "state": state,
        }
    )


def _token_request(data: dict) -> dict:
    try:
        resp = requests.post(TOKEN_URL, data=data, timeout=TIMEOUT)
    except requests.RequestException as exc:
        raise ProviderError("Google недоступен.") from exc
    if resp.status_code != 200:
        raise AuthError("Google отклонил авторизацию. Подключите календарь заново.")
    tokens = resp.json()
    tokens["expires_at"] = _time.time() + int(tokens.get("expires_in", 3600)) - 60
    return tokens


def exchange_code(client_id, client_secret, redirect_uri, code) -> dict:
    return _token_request(
        {
            "code": code,
            "client_id": client_id,
            "client_secret": client_secret,
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }
    )


# ---------------------------------------------------------------- преобразование событий


def _parse_google_time(obj: dict, default_tz):
    if "date" in obj:
        d = date.fromisoformat(obj["date"])
        return datetime.combine(d, datetime.min.time(), tzinfo=default_tz), True
    value = obj["dateTime"].replace("Z", "+00:00")
    return datetime.fromisoformat(value), False


def event_from_google(item: dict, default_tz) -> EventData | None:
    if item.get("status") == "cancelled" or "start" not in item:
        return None
    start, all_day = _parse_google_time(item["start"], default_tz)
    end, _ = _parse_google_time(item.get("end", item["start"]), default_tz)
    if all_day:
        end = max(end - timedelta(minutes=1), start)  # у Google конец дня-события исключён
    uid = item.get("iCalUID") or item["id"]
    read_only = "recurringEventId" in item
    if read_only:
        # экземпляр повторяющегося события: uid = <uid серии>#<исходное время экземпляра>
        orig, _ = _parse_google_time(item.get("originalStartTime", item["start"]), default_tz)
        uid = f"{uid}#{orig.astimezone(timezone.utc):%Y%m%dT%H%M%SZ}"
    props = (item.get("extendedProperties") or {}).get("private") or {}
    updated = item.get("updated")
    return EventData(
        uid=uid,
        title=item.get("summary", ""),
        description=item.get("description", ""),
        location=item.get("location", ""),
        url=props.get("calendar_url") or (item.get("source") or {}).get("url", ""),
        start=start,
        end=end,
        all_day=all_day,
        last_modified=datetime.fromisoformat(updated.replace("Z", "+00:00")) if updated else None,
        read_only=read_only,
    )


def event_to_google(data: EventData, tz) -> dict:
    tz_name = getattr(tz, "key", "UTC")
    if data.all_day:
        start = {"date": data.start.astimezone(tz).date().isoformat()}
        end = {"date": (data.end.astimezone(tz).date() + timedelta(days=1)).isoformat()}
    else:
        start = {"dateTime": data.start.astimezone(tz).isoformat(), "timeZone": tz_name}
        end = {"dateTime": (data.end or data.start).astimezone(tz).isoformat(), "timeZone": tz_name}
    body = {
        "summary": data.title,
        "description": data.description,
        "location": data.location,
        "start": start,
        "end": end,
        "extendedProperties": {"private": {"calendar_url": data.url or ""}},
    }
    if data.url:
        body["source"] = {"title": "Ссылка", "url": data.url}
    return body


# ---------------------------------------------------------------- адаптер


class GoogleProvider(Provider):
    """
    credentials: {"refresh_token", "access_token", "expires_at"}.
    on_tokens(credentials) вызывается после обновления access-токена — чтобы сохранить его.
    """

    def __init__(self, client_id, client_secret, credentials: dict, calendar_id="", default_tz=timezone.utc, on_tokens=None):
        if not client_id or not client_secret:
            raise ProviderError("Интеграция с Google не настроена: задайте GOOGLE_OAUTH_CLIENT_ID и GOOGLE_OAUTH_CLIENT_SECRET.")
        self.client_id = client_id
        self.client_secret = client_secret
        self.credentials = dict(credentials or {})
        self.calendar_id = calendar_id
        self.default_tz = default_tz
        self.on_tokens = on_tokens

    # ---- токены
    def _access_token(self, force=False):
        creds = self.credentials
        if force or not creds.get("access_token") or creds.get("expires_at", 0) < _time.time():
            if not creds.get("refresh_token"):
                raise AuthError("Нет доступа к Google. Подключите календарь заново.")
            fresh = _token_request(
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": creds["refresh_token"],
                    "grant_type": "refresh_token",
                }
            )
            creds.update(access_token=fresh["access_token"], expires_at=fresh["expires_at"])
            if self.on_tokens:
                self.on_tokens(creds)
        return creds["access_token"]

    def _call(self, method, path, params=None, json=None, headers=None, ok=(200, 201, 204)):
        for attempt in (0, 1):
            hdrs = {"Authorization": f"Bearer {self._access_token(force=attempt == 1)}"}
            hdrs.update(headers or {})
            try:
                resp = requests.request(method, API + path, params=params, json=json, headers=hdrs, timeout=TIMEOUT)
            except requests.RequestException as exc:
                raise ProviderError("Google Календарь недоступен.") from exc
            if resp.status_code == 401 and attempt == 0:
                continue  # access-токен истёк раньше срока — обновляем и повторяем
            break
        if resp.status_code in (401, 403):
            raise AuthError("Google отклонил запрос. Подключите календарь заново или проверьте доступ к нему.")
        if resp.status_code == 412:
            raise ConflictError("Событие в Google изменилось.")
        if resp.status_code not in ok:
            raise ProviderError(f"Google Календарь ответил {resp.status_code}.")
        return resp.json() if resp.content else {}

    def _cal_path(self):
        if not self.calendar_id:
            raise ProviderError("Не выбран календарь.")
        return f"/calendars/{quote(self.calendar_id, safe='')}/events"

    # ---- интерфейс
    def list_calendars(self):
        result, token = [], None
        while True:
            data = self._call("GET", "/users/me/calendarList", params={"pageToken": token} if token else None)
            for c in data.get("items", []):
                result.append(
                    {
                        "id": c["id"],
                        "name": c.get("summaryOverride") or c.get("summary", c["id"]),
                        "color": c.get("backgroundColor", ""),
                        "read_only": c.get("accessRole") not in ("owner", "writer"),
                    }
                )
            token = data.get("nextPageToken")
            if not token:
                return result

    def list_events(self, start, end):
        items, token = [], None
        while True:
            params = {
                "timeMin": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "timeMax": end.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                "singleEvents": "true",  # Google сам разворачивает повторения
                "showDeleted": "false",
                "maxResults": "2500",
            }
            if token:
                params["pageToken"] = token
            data = self._call("GET", self._cal_path(), params=params)
            for it in data.get("items", []):
                ev = event_from_google(it, self.default_tz)
                if ev:
                    items.append(RemoteItem(remote_id=it["id"], etag=it.get("etag", ""), data=ev))
            token = data.get("nextPageToken")
            if not token:
                return items

    def create_event(self, data):
        body = event_to_google(data, self.default_tz)
        body["iCalUID"] = data.uid
        res = self._call("POST", self._cal_path(), json=body)
        return RemoteItem(remote_id=res["id"], etag=res.get("etag", ""), data=data)

    def update_event(self, remote_id, etag, data):
        headers = {"If-Match": etag} if etag else {}
        res = self._call("PUT", f"{self._cal_path()}/{quote(remote_id, safe='')}", json=event_to_google(data, self.default_tz), headers=headers)
        return res.get("etag", "")

    def delete_event(self, remote_id, etag):
        headers = {"If-Match": etag} if etag else {}
        self._call("DELETE", f"{self._cal_path()}/{quote(remote_id, safe='')}", headers=headers, ok=(200, 204, 404, 410))
