# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Простейший SMTP-сервер в памяти — для тестов отправки приглашений (без шифрования, вход AUTH PLAIN/LOGIN)."""

import base64
import re
import socketserver
import threading


class FakeSMTP:
    def __init__(self):
        self.messages = []  # [(mail_from, [rcpt], data)]
        self.logins = []  # [(user, password)]
        self.refused_senders = set()  # адреса, от которых сервер не разрешает отправлять
        server = self

        class Handler(socketserver.StreamRequestHandler):
            def reply(self, line):
                self.wfile.write((line + "\r\n").encode())

            def handle(self):
                self.reply("220 fake ESMTP")
                mail_from, rcpt = "", []
                while True:
                    raw = self.rfile.readline()
                    if not raw:
                        return
                    line = raw.decode("utf-8", "replace").rstrip("\r\n")
                    cmd = line.split(" ", 1)[0].upper()
                    if cmd in ("EHLO", "HELO"):
                        self.wfile.write(b"250-fake\r\n250 AUTH PLAIN LOGIN\r\n")
                    elif cmd == "AUTH":
                        parts = line.split()
                        if parts[1].upper() == "PLAIN":
                            token = parts[2] if len(parts) > 2 else ""
                            if not token:
                                self.reply("334 ")
                                token = self.rfile.readline().decode().strip()
                            _, user, pwd = base64.b64decode(token).decode().split("\0")
                        else:
                            self.reply("334 VXNlcm5hbWU6")
                            user = base64.b64decode(self.rfile.readline().strip()).decode()
                            self.reply("334 UGFzc3dvcmQ6")
                            pwd = base64.b64decode(self.rfile.readline().strip()).decode()
                        server.logins.append((user, pwd))
                        self.reply("235 ok")
                    elif cmd == "MAIL":
                        mail_from = re.search(r"<([^>]*)>", line).group(1)
                        rcpt = []
                        if mail_from.lower() in server.refused_senders:
                            self.reply("550 sender rejected")
                            continue
                        self.reply("250 ok")
                    elif cmd == "RCPT":
                        rcpt.append(re.search(r"<([^>]*)>", line).group(1))
                        self.reply("250 ok")
                    elif cmd == "DATA":
                        self.reply("354 go")
                        chunks = []
                        while True:
                            data_line = self.rfile.readline()
                            if not data_line or data_line in (b".\r\n", b".\n"):
                                break
                            chunks.append(data_line)
                        server.messages.append((mail_from, rcpt, b"".join(chunks).decode("utf-8", "replace")))
                        self.reply("250 queued")
                    elif cmd == "QUIT":
                        self.reply("221 bye")
                        return
                    else:  # RSET, NOOP и прочее
                        self.reply("250 ok")

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.server.shutdown()
        self.server.server_close()
