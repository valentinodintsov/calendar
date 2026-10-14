"""
Простой CalDAV-сервер в памяти — для тестов адаптера и синхронизации без доступа к интернету.
Поддерживает: PROPFIND (principal, calendar-home-set, список календарей, getetag),
REPORT calendar-query, PUT (If-Match / If-None-Match), DELETE, Basic-авторизацию.
"""

import base64
import hashlib
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape


class FakeCalDAV:
    def __init__(self, username="user@example.com", password="app-pass", redirect_root=False):
        self.username = username
        self.password = password
        self.redirect_root = redirect_root
        self.calendars = {"/cal/user/work/": "Работа", "/cal/user/home/": "Дом"}
        self.resources = {}  # href -> (etag, ics)
        self.requests = []
        self.lock = threading.Lock()
        handler = self._handler()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- помощники для тестов
    def put(self, href, ics):
        with self.lock:
            etag = '"' + hashlib.md5(ics.encode()).hexdigest()[:12] + '"'
            self.resources[href] = (etag, ics)
            return etag

    def _handler(server):  # noqa: N805 — замыкание на сервер
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _auth_ok(self):
                h = self.headers.get("Authorization", "")
                expected = "Basic " + base64.b64encode(f"{server.username}:{server.password}".encode()).decode()
                return h == expected

            def _send(self, code, body=b"", headers=None):
                self.send_response(code)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if body:
                    self.wfile.write(body)

            def _body(self):
                n = int(self.headers.get("Content-Length") or 0)
                return self.rfile.read(n).decode("utf-8") if n else ""

            def _multistatus(self, parts):
                xml = (
                    '<?xml version="1.0" encoding="utf-8"?>'
                    '<d:multistatus xmlns:d="DAV:" xmlns:c="urn:ietf:params:xml:ns:caldav" '
                    'xmlns:cs="http://calendarserver.org/ns/" xmlns:ic="http://apple.com/ns/ical/">'
                    + "".join(parts) + "</d:multistatus>"
                )
                self._send(207, xml.encode("utf-8"), {"Content-Type": "application/xml; charset=utf-8"})

            @staticmethod
            def _resp(href, props):
                return (f"<d:response><d:href>{escape(href)}</d:href><d:propstat><d:prop>{props}</d:prop>"
                        "<d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response>")

            def do_PROPFIND(self):
                server.requests.append(("PROPFIND", self.path))
                if not self._auth_ok():
                    return self._send(401, headers={"WWW-Authenticate": 'Basic realm="cal"'})
                body = self._body()
                depth = self.headers.get("Depth", "0")
                path = self.path
                if server.redirect_root and path in ("/", "/.well-known/caldav"):
                    return self._send(301, headers={"Location": "/dav/"})
                if "current-user-principal" in body:
                    return self._multistatus([self._resp(path, "<d:current-user-principal><d:href>/principals/user/</d:href></d:current-user-principal>")])
                if "calendar-home-set" in body:
                    return self._multistatus([self._resp(path, "<c:calendar-home-set><d:href>/cal/user/</d:href></c:calendar-home-set>")])
                if "getetag" in body and "resourcetype" not in body:
                    with server.lock:
                        if path not in server.resources:
                            return self._send(404)
                        return self._multistatus([self._resp(path, f"<d:getetag>{server.resources[path][0]}</d:getetag>")])
                # список календарей
                parts = []
                if path in server.calendars:
                    parts.append(self._cal(path))
                elif path == "/cal/user/" and depth == "1":
                    parts.append(self._resp(path, "<d:resourcetype><d:collection/></d:resourcetype>"))
                    parts += [self._cal(h) for h in server.calendars]
                    parts.append(self._resp("/cal/user/tasks/",
                                 "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                                 '<c:supported-calendar-component-set><c:comp name="VTODO"/></c:supported-calendar-component-set>'))
                else:
                    parts.append(self._resp(path, "<d:resourcetype><d:collection/></d:resourcetype>"))
                return self._multistatus(parts)

            def _cal(self, href):
                return self._resp(href,
                    "<d:resourcetype><d:collection/><c:calendar/></d:resourcetype>"
                    f"<d:displayname>{escape(server.calendars[href])}</d:displayname>"
                    '<c:supported-calendar-component-set><c:comp name="VEVENT"/></c:supported-calendar-component-set>'
                    "<ic:calendar-color>#FF2968FF</ic:calendar-color>"
                    "<d:current-user-privilege-set><d:privilege><d:write/></d:privilege></d:current-user-privilege-set>")

            def do_REPORT(self):
                server.requests.append(("REPORT", self.path))
                if not self._auth_ok():
                    return self._send(401)
                body = self._body()
                m = re.search(r'start="(\d{8}T\d{6}Z)" end="(\d{8}T\d{6}Z)"', body)
                parts = []
                with server.lock:
                    for href, (etag, ics) in sorted(server.resources.items()):
                        if not href.startswith(self.path):
                            continue
                        # упрощённый фильтр по времени: берём всё (адаптер сам отсекает окно)
                        parts.append(self._resp(href, f"<d:getetag>{escape(etag)}</d:getetag>"
                                                       f"<c:calendar-data>{escape(ics)}</c:calendar-data>"))
                self.server_range = m.groups() if m else None
                return self._multistatus(parts)

            def do_PUT(self):
                server.requests.append(("PUT", self.path))
                if not self._auth_ok():
                    return self._send(401)
                body = self._body()
                with server.lock:
                    exists = self.path in server.resources
                    if self.headers.get("If-None-Match") == "*" and exists:
                        return self._send(412)
                    if_match = self.headers.get("If-Match")
                    if if_match and (not exists or server.resources[self.path][0] != if_match):
                        return self._send(412)
                etag = server.put(self.path, body)
                return self._send(204 if exists else 201, headers={"ETag": etag})

            def do_DELETE(self):
                server.requests.append(("DELETE", self.path))
                if not self._auth_ok():
                    return self._send(401)
                with server.lock:
                    if self.path not in server.resources:
                        return self._send(404)
                    if_match = self.headers.get("If-Match")
                    if if_match and server.resources[self.path][0] != if_match:
                        return self._send(412)
                    del server.resources[self.path]
                return self._send(204)

        return Handler
