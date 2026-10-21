# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Отправка писем с приглашениями: через почтовый сервер из админки (MailServer) или настройки EMAIL_* из settings.py.

Отправитель письма — общий адрес почтового сервера. С настройкой INTEGRATIONS_MAIL_FROM_USER = True в поле «От»
ставится адрес пользователя, создавшего событие (вход на сервер — по-прежнему под общей учётной записью): это работает,
только если серверу разрешено отправлять от адресов сотрудников. Отказ сразу (SMTPSenderRefused) обрабатывается —
письмо уходит от общего адреса; но сервер может принять письмо и вернуть его позже («Not allowed address»).

Ошибки SMTP переводятся в понятный текст и записываются в MailServer.last_error — администратор видит их в админке,
а пользователь — в сообщении после сохранения события.
"""

from __future__ import annotations

import logging
import smtplib
import socket
import ssl

from django.conf import settings
from django.core.mail import get_connection
from django.core.mail.backends.smtp import EmailBackend
from django.core.mail.message import sanitize_address
from django.utils import timezone
from django.utils.functional import cached_property

log = logging.getLogger(__name__)

NOT_SENDING_BACKENDS = ("console.EmailBackend", "dummy.EmailBackend", "filebased.EmailBackend")


class MailNotConfigured(Exception):
    pass


class _SmtpBackend(EmailBackend):
    def __init__(self, *args, verify_certificate=True, **kwargs):
        self._verify = verify_certificate
        super().__init__(*args, **kwargs)

    @cached_property
    def ssl_context(self):
        ctx = ssl.create_default_context()
        if not self._verify:  # самоподписанный сертификат внутреннего сервера
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
        return ctx


def active_server():
    from .models import MailServer

    return MailServer.objects.filter(enabled=True).order_by("pk").first()


def from_email() -> str:
    srv = active_server()
    return (srv.from_email if srv else "") or settings.DEFAULT_FROM_EMAIL


def sender_for(event) -> str:
    """
    От кого уходят письма о событии: «Имя Фамилия <адрес>» пользователя, создавшего событие (вошедшего в календарь);
    автор неизвестен (событие из внешнего календаря или админки) — владельца календаря; нет адреса — общий адрес.
    """
    if getattr(settings, "INTEGRATIONS_MAIL_FROM_USER", False):
        for user in (event.created_by if event.created_by_id else None, event.owner if event.owner_id else None):
            email = (getattr(user, "email", "") or "").strip()
            if email and "@" in email:
                name = (user.get_full_name() or "").replace('"', "").strip()
                return f'"{name}" <{email}>' if name else email
    return from_email()


def connection_for(srv):
    return _SmtpBackend(
        host=srv.host,
        port=srv.port,
        username=srv.username or "",
        password=srv.password if srv.username else "",
        use_tls=srv.security == srv.Security.STARTTLS,
        use_ssl=srv.security == srv.Security.SSL,
        timeout=srv.timeout or 15,
        fail_silently=False,
        verify_certificate=srv.verify_certificate,
    )


def connection():
    """Соединение для отправки. MailNotConfigured — если письма никуда не уйдут (вывод в консоль)."""
    srv = active_server()
    if srv:
        return connection_for(srv), srv
    if settings.EMAIL_BACKEND.endswith(NOT_SENDING_BACKENDS):
        raise MailNotConfigured(
            "Почтовый сервер не настроен — письма не отправляются. Администратору: админка → "
            "«Почтовый сервер (отправка приглашений)»."
        )
    return get_connection(fail_silently=False), None


def explain(exc: Exception) -> str:
    """Понятный текст ошибки SMTP."""
    if isinstance(exc, MailNotConfigured):
        return str(exc)
    if isinstance(exc, smtplib.SMTPAuthenticationError):
        return "почтовый сервер отклонил логин или пароль"
    if isinstance(exc, smtplib.SMTPSenderRefused):
        return (f"сервер не разрешает отправлять от адреса {exc.sender}: укажите адрес отправителя, совпадающий "
                "с логином, или дайте учётной записи право «Отправить как»")
    if isinstance(exc, smtplib.SMTPRecipientsRefused):
        return "сервер не принял адрес получателя: " + ", ".join(exc.recipients)
    if isinstance(exc, smtplib.SMTPNotSupportedError):
        return "сервер не поддерживает выбранное шифрование или вход — проверьте порт и «Шифрование»"
    if isinstance(exc, ssl.SSLCertVerificationError):
        return "сертификат почтового сервера не прошёл проверку (снимите «Проверять сертификат» для внутреннего сервера)"
    if isinstance(exc, ssl.SSLError):
        return "ошибка шифрования: проверьте порт и «Шифрование» (587 — STARTTLS, 465 — SSL/TLS)"
    if isinstance(exc, socket.gaierror):
        return "имя почтового сервера не найдено (DNS)"
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return "почтовый сервер не ответил (тайм-аут): проверьте адрес, порт и доступ по сети"
    if isinstance(exc, ConnectionRefusedError):
        return "почтовый сервер отказал в подключении: проверьте адрес и порт"
    if isinstance(exc, smtplib.SMTPException):
        return f"ошибка почтового сервера: {exc}"
    if isinstance(exc, OSError):
        return f"почтовый сервер недоступен: {exc.__class__.__name__}"
    return f"{exc.__class__.__name__}: {exc}"


def _address(value: str) -> str:
    """Адрес без имени, в нижнем регистре: «Иван <I@x.ru>» → «i@x.ru»."""
    try:
        return sanitize_address(value, "utf-8").rsplit("<", 1)[-1].rstrip(">").strip().lower()
    except ValueError:
        return (value or "").strip().lower()


def _note(srv, error=""):
    if srv is None:
        return
    from .models import MailServer

    now = timezone.now()
    if error:
        MailServer.objects.filter(pk=srv.pk).update(last_error=error[:2000], last_error_at=now)
    else:
        MailServer.objects.filter(pk=srv.pk).update(last_error="", last_ok_at=now)


def send(messages) -> tuple[list, dict]:
    """
    Отправляет письма одним соединением. Возвращает (адреса, куда ушло; {адрес: причина} — куда нет).
    Не бросает исключений.
    """
    sent, failed = [], {}
    if not messages:
        return sent, failed
    srv = None
    try:
        conn, srv = connection()
        conn.open()
    except Exception as exc:  # noqa: BLE001
        reason = explain(exc)
        log.warning("Почта: %s", reason)
        _note(srv, reason)
        return sent, {m.to[0]: reason for m in messages}
    try:
        for msg in messages:
            msg.connection = conn
            try:
                try:
                    msg.send()
                except smtplib.SMTPSenderRefused:
                    common = from_email()
                    if _address(msg.from_email) == _address(common):
                        raise
                    # сервер не разрешает отправлять от адреса пользователя — от общего адреса; ответы придут пользователю
                    log.warning("Почта: сервер не принял отправителя %s, письмо уходит от %s", msg.from_email, common)
                    msg.reply_to = msg.reply_to or [msg.from_email]
                    msg.from_email = common
                    msg.send()
                sent.append(msg.to[0])
            except Exception as exc:  # noqa: BLE001
                failed[msg.to[0]] = explain(exc)
                log.warning("Не удалось отправить письмо %s: %s", msg.to[0], failed[msg.to[0]])
    finally:
        try:
            conn.close()
        except Exception:  # noqa: BLE001
            pass
    _note(srv, next(iter(failed.values()), "") if failed and not sent else "")
    return sent, failed


def send_test(srv, to_email: str) -> None:
    """Тестовое письмо (кнопка в админке). Бросает исключение с понятным текстом."""
    from django.core.mail import EmailMessage

    msg = EmailMessage(
        "Календарь: проверка почты",
        "Это тестовое письмо. Если вы его получили, приглашения на события будут доходить.",
        srv.from_email,
        [to_email],
        connection=connection_for(srv),
    )
    try:
        msg.send()
    except Exception as exc:  # noqa: BLE001
        reason = explain(exc)
        _note(srv, reason)
        raise RuntimeError(reason) from exc
    _note(srv)
