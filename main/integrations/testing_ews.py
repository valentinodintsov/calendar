"""
Простой EWS-сервер (Exchange Web Services) в памяти — для тестов адаптера Exchange без настоящего сервера.
Поддерживает: GetFolder, FindFolder, FindItem (CalendarView), GetItem (Body), CreateItem, UpdateItem, DeleteItem;
вход Basic; проверку ChangeKey и порядка полей CalendarItem при создании;
участников собрания (RequiredAttendees) и журнал рассылок `sent` — что «разослал бы» Exchange.
"""

import base64
import itertools
import threading
import xml.etree.ElementTree as ET
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from xml.sax.saxutils import escape

T = "{http://schemas.microsoft.com/exchange/services/2006/types}"
M = "{http://schemas.microsoft.com/exchange/services/2006/messages}"
CREATE_ORDER = ["Subject", "Body", "ReminderIsSet", "ReminderMinutesBeforeStart", "ExtendedProperty", "UID", "Start", "End", "IsAllDayEvent",
                "LegacyFreeBusyStatus", "Location", "RequiredAttendees", "StartTimeZone", "EndTimeZone"]


def _emails(el):
    return [] if el is None else [(e.text or "") for e in el.iter(T + "EmailAddress")]


def _dt(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


class FakeEWS:
    def __init__(self, username="DOMAIN\\user", password="secret", offer="Basic"):
        self.username, self.password, self.offer = username, password, offer
        self.items = {}  # id -> dict
        self.requests = []
        self.sent = []  # [{"op", "mode", "attendees", "before"}] — рассылки участникам собраний
        self._ids = itertools.count(1)
        self.lock = threading.RLock()  # обработчик под блокировкой вызывает add()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}/EWS/Exchange.asmx"
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()

    # ---- помощники для тестов
    def add(self, **fields):
        with self.lock:
            iid = f"AAMk{next(self._ids)}"
            item = {"Subject": "", "Body": "", "Location": "", "UID": f"uid-{iid}", "IsAllDayEvent": "false",
                    "CalendarItemType": "Single", "Url": "", "ChangeKey": "CK1", "LastModifiedTime": "2026-10-01T00:00:00Z",
                    "Attendees": [], "Optional": [], "Files": [], "ReminderIsSet": "false", "ReminderMinutesBeforeStart": "15"}
            item.update(fields)
            for n, f in enumerate(item["Files"]):
                f.setdefault("Id", f"{iid}:A{n}")
            self.items[iid] = item
            return iid

    def find_file(self, aid):
        for iid, item in self.items.items():
            for f in item["Files"]:
                if f.get("Id") == aid:
                    return iid, f
        return None, None

    def by_subject(self, subject):
        return next((dict(v, Id=k) for k, v in self.items.items() if v["Subject"] == subject), None)

    def _bump(self, item):
        item["ChangeKey"] = "CK" + str(int(item["ChangeKey"][2:]) + 1)

    def _handler(server):  # noqa: N805
        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body=b"", ctype="text/xml; charset=utf-8", headers=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _soap(self, inner):
                xml = ('<?xml version="1.0" encoding="utf-8"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/" '
                       'xmlns:m="http://schemas.microsoft.com/exchange/services/2006/messages" '
                       'xmlns:t="http://schemas.microsoft.com/exchange/services/2006/types"><s:Body>' + inner + "</s:Body></s:Envelope>")
                self._send(200, xml.encode("utf-8"))

            def _fault(self, text):
                xml = ('<?xml version="1.0"?><s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault>'
                       f"<faultcode>s:Client</faultcode><faultstring>{escape(text)}</faultstring></s:Fault></s:Body></s:Envelope>")
                self._send(500, xml.encode("utf-8"))

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                expected = "Basic " + base64.b64encode(f"{server.username}:{server.password}".encode()).decode()
                if self.headers.get("Authorization") != expected:
                    return self._send(401, headers={"WWW-Authenticate": f'{server.offer} realm="email.yanao.ru"'})
                try:
                    body = ET.fromstring(raw).find("{http://schemas.xmlsoap.org/soap/envelope/}Body")[0]
                except Exception:  # noqa: BLE001
                    return self._fault("Request is not valid XML")
                op = body.tag.replace(M, "")
                server.requests.append(op)
                with server.lock:
                    return getattr(self, "op_" + op)(body)

            # ---- операции
            def op_GetFolder(self, body):
                self._soap('<m:GetFolderResponse><m:ResponseMessages><m:GetFolderResponseMessage ResponseClass="Success">'
                           '<m:ResponseCode>NoError</m:ResponseCode><m:Folders><t:CalendarFolder><t:FolderId Id="CAL"/>'
                           "<t:DisplayName>Календарь</t:DisplayName></t:CalendarFolder></m:Folders>"
                           "</m:GetFolderResponseMessage></m:ResponseMessages></m:GetFolderResponse>")

            def op_FindFolder(self, body):
                self._soap('<m:FindFolderResponse><m:ResponseMessages><m:FindFolderResponseMessage ResponseClass="Success">'
                           '<m:ResponseCode>NoError</m:ResponseCode><m:RootFolder><t:Folders><t:CalendarFolder>'
                           '<t:FolderId Id="SUB1"/><t:DisplayName>Совещания</t:DisplayName></t:CalendarFolder></t:Folders>'
                           "</m:RootFolder></m:FindFolderResponseMessage></m:ResponseMessages></m:FindFolderResponse>")

            def _item_xml(self, iid, item, body=False):
                if body:
                    def people(tag, emails):
                        if not emails:
                            return ""
                        return (f"<t:{tag}>" + "".join(
                            f"<t:Attendee><t:Mailbox><t:Name>{escape(e)}</t:Name><t:EmailAddress>{escape(e)}</t:EmailAddress>"
                            "<t:RoutingType>SMTP</t:RoutingType></t:Mailbox><t:ResponseType>Unknown</t:ResponseType></t:Attendee>"
                            for e in emails) + f"</t:{tag}>")
                    for f in item.get("Files", []):  # файлы, подложенные тестом напрямую, — без Id
                        f.setdefault("Id", f"{iid}:N{next(server._ids)}")
                    files = "".join(
                        f'<t:FileAttachment><t:AttachmentId Id="{f["Id"]}"/><t:Name>{escape(f["Name"])}</t:Name>'
                        f'<t:ContentType>{escape(f.get("ContentType", "application/octet-stream"))}</t:ContentType>'
                        f'<t:Size>{len(f["Content"])}</t:Size><t:IsInline>{"true" if f.get("Inline") else "false"}</t:IsInline></t:FileAttachment>'
                        for f in item.get("Files", []))
                    return (f'<t:CalendarItem><t:ItemId Id="{iid}" ChangeKey="{item["ChangeKey"]}"/>'
                            f'<t:Body BodyType="Text">{escape(item["Body"])}</t:Body>'
                            + (f"<t:Attachments>{files}</t:Attachments>" if files else "")
                            + people("RequiredAttendees", item.get("Attendees")) + people("OptionalAttendees", item.get("Optional"))
                            + "</t:CalendarItem>")
                url = (f'<t:ExtendedProperty><t:ExtendedFieldURI PropertySetId="x" PropertyName="CalendarUrl" PropertyType="String"/>'
                       f'<t:Value>{escape(item["Url"])}</t:Value></t:ExtendedProperty>') if item["Url"] else ""
                return (f'<t:CalendarItem><t:ItemId Id="{iid}" ChangeKey="{item["ChangeKey"]}"/>'
                        f'<t:Subject>{escape(item["Subject"])}</t:Subject><t:ReminderIsSet>{item["ReminderIsSet"]}</t:ReminderIsSet>'
                        f'<t:ReminderMinutesBeforeStart>{item["ReminderMinutesBeforeStart"]}</t:ReminderMinutesBeforeStart>'
                        f'{url}<t:LastModifiedTime>{item["LastModifiedTime"]}</t:LastModifiedTime>'
                        f'<t:UID>{escape(item["UID"])}</t:UID><t:Start>{item["Start"]}</t:Start><t:End>{item["End"]}</t:End>'
                        f'<t:IsAllDayEvent>{item["IsAllDayEvent"]}</t:IsAllDayEvent>'
                        + (f'<t:Location>{escape(item["Location"])}</t:Location>' if item["Location"] else "")
                        + f'<t:CalendarItemType>{item["CalendarItemType"]}</t:CalendarItemType></t:CalendarItem>')

            def op_FindItem(self, body):
                view = body.find(M + "CalendarView")
                ws, we = _dt(view.get("StartDate")), _dt(view.get("EndDate"))
                found = [self._item_xml(i, it) for i, it in server.items.items()
                         if _dt(it["Start"]) < we and _dt(it["End"]) > ws]
                self._soap('<m:FindItemResponse><m:ResponseMessages><m:FindItemResponseMessage ResponseClass="Success">'
                           f'<m:ResponseCode>NoError</m:ResponseCode><m:RootFolder TotalItemsInView="{len(found)}"><t:Items>'
                           + "".join(found) + "</t:Items></m:RootFolder></m:FindItemResponseMessage></m:ResponseMessages></m:FindItemResponse>")

            def op_GetItem(self, body):
                msgs = []
                for el in body.iter(T + "ItemId"):
                    iid = el.get("Id")
                    if iid in server.items:
                        msgs.append('<m:GetItemResponseMessage ResponseClass="Success"><m:ResponseCode>NoError</m:ResponseCode>'
                                    f"<m:Items>{self._item_xml(iid, server.items[iid], body=True)}</m:Items></m:GetItemResponseMessage>")
                    else:
                        msgs.append('<m:GetItemResponseMessage ResponseClass="Error"><m:MessageText>not found</m:MessageText>'
                                    "<m:ResponseCode>ErrorItemNotFound</m:ResponseCode><m:Items/></m:GetItemResponseMessage>")
                self._soap("<m:GetItemResponse><m:ResponseMessages>" + "".join(msgs) + "</m:ResponseMessages></m:GetItemResponse>")

            def op_GetAttachment(self, body):
                aid = body.find(f"{M}AttachmentIds/{T}AttachmentId").get("Id")
                _, f = server.find_file(aid)
                if f is None:
                    return self._soap('<m:GetAttachmentResponse><m:ResponseMessages><m:GetAttachmentResponseMessage ResponseClass="Error">'
                                      "<m:MessageText>not found</m:MessageText><m:ResponseCode>ErrorAttachmentNotFound</m:ResponseCode>"
                                      "</m:GetAttachmentResponseMessage></m:ResponseMessages></m:GetAttachmentResponse>")
                content = base64.b64encode(f["Content"]).decode()
                self._soap('<m:GetAttachmentResponse><m:ResponseMessages><m:GetAttachmentResponseMessage ResponseClass="Success">'
                           f'<m:ResponseCode>NoError</m:ResponseCode><m:Attachments><t:FileAttachment><t:AttachmentId Id="{aid}"/>'
                           f'<t:Name>{escape(f["Name"])}</t:Name><t:Content>{content}</t:Content></t:FileAttachment></m:Attachments>'
                           "</m:GetAttachmentResponseMessage></m:ResponseMessages></m:GetAttachmentResponse>")

            def op_CreateAttachment(self, body):
                iid = body.find(M + "ParentItemId").get("Id")
                item = server.items.get(iid)
                fa = body.find(f"{M}Attachments/{T}FileAttachment")
                if item is None:
                    return self._fault("ErrorItemNotFound")
                aid = f"{iid}:N{next(server._ids)}"
                item["Files"].append({"Id": aid, "Name": fa.findtext(T + "Name"),
                                      "ContentType": fa.findtext(T + "ContentType") or "application/octet-stream",
                                      "Content": base64.b64decode(fa.findtext(T + "Content") or "")})
                server._bump(item)
                self._soap('<m:CreateAttachmentResponse><m:ResponseMessages><m:CreateAttachmentResponseMessage ResponseClass="Success">'
                           f'<m:ResponseCode>NoError</m:ResponseCode><m:Attachments><t:FileAttachment><t:AttachmentId Id="{aid}" '
                           f'RootItemId="{iid}" RootItemChangeKey="{item["ChangeKey"]}"/></t:FileAttachment></m:Attachments>'
                           "</m:CreateAttachmentResponseMessage></m:ResponseMessages></m:CreateAttachmentResponse>")

            def op_DeleteAttachment(self, body):
                aid = body.find(f"{M}AttachmentIds/{T}AttachmentId").get("Id")
                iid, f = server.find_file(aid)
                if f is None:
                    return self._soap('<m:DeleteAttachmentResponse><m:ResponseMessages><m:DeleteAttachmentResponseMessage ResponseClass="Error">'
                                      "<m:MessageText>not found</m:MessageText><m:ResponseCode>ErrorAttachmentNotFound</m:ResponseCode>"
                                      "</m:DeleteAttachmentResponseMessage></m:ResponseMessages></m:DeleteAttachmentResponse>")
                item = server.items[iid]
                item["Files"].remove(f)
                server._bump(item)
                self._soap('<m:DeleteAttachmentResponse><m:ResponseMessages><m:DeleteAttachmentResponseMessage ResponseClass="Success">'
                           f'<m:ResponseCode>NoError</m:ResponseCode><m:RootItemId RootItemId="{iid}" RootItemChangeKey="{item["ChangeKey"]}"/>'
                           "</m:DeleteAttachmentResponseMessage></m:ResponseMessages></m:DeleteAttachmentResponse>")

            def op_CreateItem(self, body):
                ci = body.find(f"{M}Items/{T}CalendarItem")
                order = [c.tag.replace(T, "") for c in ci]
                expected = [x for x in CREATE_ORDER if x in order]
                if order != expected:
                    return self._fault(f"The request failed schema validation: unexpected element order {order}")
                fields = {c.tag.replace(T, ""): c for c in ci}
                url = fields["ExtendedProperty"].findtext(T + "Value") if "ExtendedProperty" in fields else ""
                attendees = _emails(fields.get("RequiredAttendees"))
                iid = server.add(**{k: (fields[k].text or "") for k in ("Subject", "Body", "UID", "Start", "End", "IsAllDayEvent", "Location",
                                              "ReminderIsSet", "ReminderMinutesBeforeStart")
                                    if k in fields}, Url=url or "", Attendees=attendees)
                item = server.items[iid]
                server.sent.append({"op": "create", "mode": body.get("SendMeetingInvitations"), "attendees": attendees, "before": []})
                self._soap('<m:CreateItemResponse><m:ResponseMessages><m:CreateItemResponseMessage ResponseClass="Success">'
                           f'<m:ResponseCode>NoError</m:ResponseCode><m:Items><t:CalendarItem><t:ItemId Id="{iid}" ChangeKey="{item["ChangeKey"]}"/>'
                           "</t:CalendarItem></m:Items></m:CreateItemResponseMessage></m:ResponseMessages></m:CreateItemResponse>")

            def op_UpdateItem(self, body):
                change = body.find(f"{M}ItemChanges/{T}ItemChange")
                iid_el = change.find(T + "ItemId")
                iid, ck = iid_el.get("Id"), iid_el.get("ChangeKey")
                item = server.items.get(iid)
                if item is None or (ck and ck != item["ChangeKey"]):
                    code = "ErrorItemNotFound" if item is None else "ErrorIrresolvableConflict"
                    return self._soap('<m:UpdateItemResponse><m:ResponseMessages><m:UpdateItemResponseMessage ResponseClass="Error">'
                                      f"<m:MessageText>{code}</m:MessageText><m:ResponseCode>{code}</m:ResponseCode>"
                                      "</m:UpdateItemResponseMessage></m:ResponseMessages></m:UpdateItemResponse>")
                before = list(item["Attendees"])
                for upd in change.find(T + "Updates"):
                    kind = upd.tag.replace(T, "")
                    if upd.find(T + "ExtendedFieldURI") is not None:
                        value = upd.find(f"{T}CalendarItem/{T}ExtendedProperty/{T}Value")
                        item["Url"] = "" if kind == "DeleteItemField" else (value.text or "")
                        continue
                    name = upd.find(T + "FieldURI").get("FieldURI").split(":")[1]
                    if name == "RequiredAttendees":
                        item["Attendees"] = [] if kind == "DeleteItemField" else _emails(upd.find(f"{T}CalendarItem/{T}RequiredAttendees"))
                    elif kind == "DeleteItemField":
                        item[name] = ""
                    elif name not in ("StartTimeZone", "EndTimeZone"):
                        item[name] = upd.find(f"{T}CalendarItem/{T}{name}").text or ""
                server._bump(item)
                server.sent.append({"op": "update", "mode": body.get("SendMeetingInvitationsOrCancellations"),
                                    "attendees": list(item["Attendees"]), "before": before})
                self._soap('<m:UpdateItemResponse><m:ResponseMessages><m:UpdateItemResponseMessage ResponseClass="Success">'
                           f'<m:ResponseCode>NoError</m:ResponseCode><m:Items><t:CalendarItem><t:ItemId Id="{iid}" ChangeKey="{item["ChangeKey"]}"/>'
                           "</t:CalendarItem></m:Items></m:UpdateItemResponseMessage></m:ResponseMessages></m:UpdateItemResponse>")

            def op_DeleteItem(self, body):
                iid = body.find(f"{M}ItemIds/{T}ItemId").get("Id")
                gone = server.items.pop(iid, None)
                if gone is not None:
                    server.sent.append({"op": "delete", "mode": body.get("SendMeetingCancellations"),
                                        "attendees": [], "before": gone["Attendees"]})
                if gone is None:
                    return self._soap('<m:DeleteItemResponse><m:ResponseMessages><m:DeleteItemResponseMessage ResponseClass="Error">'
                                      "<m:MessageText>not found</m:MessageText><m:ResponseCode>ErrorItemNotFound</m:ResponseCode>"
                                      "</m:DeleteItemResponseMessage></m:ResponseMessages></m:DeleteItemResponse>")
                self._soap('<m:DeleteItemResponse><m:ResponseMessages><m:DeleteItemResponseMessage ResponseClass="Success">'
                           "<m:ResponseCode>NoError</m:ResponseCode></m:DeleteItemResponseMessage></m:ResponseMessages></m:DeleteItemResponse>")

        return Handler
