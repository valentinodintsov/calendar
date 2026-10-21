# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Приглашения на события по электронной почте с файлом .ics (iTIP, RFC 5546).
Работает с любой почтой — Rambler, Mail.ru, Яндекс, Gmail, Outlook: событие добавляется
в календарь получателя одним нажатием. UID совпадает с тем, под которым событие
выгружается во внешние календари, поэтому дубликатов не появляется.
"""

import mimetypes

from django.conf import settings
from django.core.mail import EmailMultiAlternatives
from django.utils import timezone

from events.models import user_display_name

from . import mailer
from .ics import build_calendar, escape, vevent_lines
from .sync import event_to_data, local_uid


def recipients(event):
    """Адреса приглашённых сотрудников и гостей (без организатора, без повторов)."""
    organizer = (event.owner.email if event.owner_id else "").lower()
    seen, result = set(), []
    for name, email in [(user_display_name(u), u.email) for u in event.invitees.all()] + [("", e) for e in event.guest_list()]:
        key = (email or "").strip().lower()
        if key and key != organizer and key not in seen:
            seen.add(key)
            result.append((name, email.strip()))
    return result


def reminder_trigger(minutes: int) -> str:
    """Минуты до начала → длительность iCalendar: 5 → -PT5M, 120 → -PT2H, 1440 → -P1D, 0 → -PT0M."""
    days, rest = divmod(max(int(minutes), 0), 1440)
    hours, mins = divmod(rest, 60)
    clock = (f"{hours}H" if hours else "") + (f"{mins}M" if mins else "")
    if not days and not clock:
        clock = "0M"
    return "-P" + (f"{days}D" if days else "") + (f"T{clock}" if clock else "")


def valarm_lines(minutes: int) -> list[str]:
    """VALARM в том виде, в каком его пишут сами Exchange и Outlook, — так его разбирают все календари."""
    return ["BEGIN:VALARM", "DESCRIPTION:REMINDER", f"TRIGGER;RELATED=START:{reminder_trigger(minutes)}",
            "ACTION:DISPLAY", "END:VALARM"]


def build_invitation(event, method="REQUEST", attendees=None) -> str:
    """attendees — [(имя, e-mail)] для строк ATTENDEE; по умолчанию все приглашённые и гости."""
    data = event_to_data(event, local_uid(event))
    data.sequence = int(event.updated_at.timestamp()) if event.updated_at else 0
    extra = []
    owner = event.owner
    if owner and owner.email:
        extra.append(f'ORGANIZER;CN="{escape(user_display_name(owner))}":mailto:{owner.email}')
    for name, email in recipients(event) if attendees is None else attendees:
        cn = f';CN="{escape(name)}"' if name else ""
        extra.append(f"ATTENDEE{cn};ROLE=REQ-PARTICIPANT;PARTSTAT=NEEDS-ACTION;RSVP=TRUE:mailto:{email}")
    if method == "CANCEL":
        # отмена должна быть новее последнего приглашения (RFC 5546), иначе календарь получателя вправе её пропустить;
        # событие при удалении не меняется, поэтому номер берём от текущего времени
        data.sequence = max(data.sequence, int(timezone.now().timestamp())) + 1
        extra.append("STATUS:CANCELLED")
    elif event.reminder_minutes is not None:
        # напоминание — календарь получателя покажет его за указанное время до начала
        extra += valarm_lines(event.reminder_minutes)
    return build_calendar([vevent_lines(data, extra, tz=timezone.get_default_timezone())], method=method)


def _when(event):
    tz = timezone.get_default_timezone()
    start = timezone.localtime(event.start, tz)
    if event.all_day:
        end = timezone.localtime(event.end or event.start, tz)
        return f"{start:%d.%m.%Y}" + (f" — {end:%d.%m.%Y}" if end.date() != start.date() else "") + ", весь день"
    end = timezone.localtime(event.end or event.start, tz)
    return f"{start:%d.%m.%Y %H:%M} — {end:%H:%M}" if end.date() == start.date() else f"{start:%d.%m.%Y %H:%M} — {end:%d.%m.%Y %H:%M}"


def event_files(event) -> tuple[list, list]:
    """
    Файлы события для письма: ([(имя, содержимое, тип)], [имена файлов, которые не вложены]).
    Вкладываем, пока общий размер не превысит INTEGRATIONS_MAIL_MAX_ATTACHMENTS_MB (почтовые серверы ограничивают письмо).
    """
    limit = int(getattr(settings, "INTEGRATIONS_MAIL_MAX_ATTACHMENTS_MB", 20)) * 1024 * 1024
    files, skipped, total = [], [], 0
    for att in event.attachments.all():
        if total + att.size > limit:
            skipped.append(att.name)
            continue
        try:
            with att.file.open("rb") as fh:
                content = fh.read()
        except (OSError, ValueError):
            skipped.append(att.name)
            continue
        total += len(content)
        files.append((att.name, content, mimetypes.guess_type(att.name)[0] or "application/octet-stream"))
    return files, skipped


def build_messages(event, emails, method="REQUEST", attendees=None) -> list:
    """Письма с приглашением (или отменой) — каждому адресату отдельное, адреса друг другу не видны."""
    if not emails:
        return []
    ics = build_invitation(event, method, attendees)
    organizer = user_display_name(event.owner) if event.owner_id else "Календарь"
    if method == "CANCEL":
        subject = f"Отменено: {event.title}"
        intro = f"{organizer} отменил(а) событие."
    else:
        subject = f"Приглашение: {event.title} — {_when(event)}"
        intro = f"{organizer} приглашает вас на событие."
    lines = [intro, "", event.title, _when(event)]
    if event.location:
        lines.append(f"Место: {event.location}")
    if event.url:
        lines.append(f"Ссылка: {event.url}")
    if event.description:
        lines += ["", event.description]
    # файлы события — вложениями письма: почтовые серверы переносят их в событие календаря получателя
    files, skipped = event_files(event) if method != "CANCEL" else ([], [])
    if files:
        lines += ["", "Файлы события во вложении: " + ", ".join(name for name, _, _ in files)]
    if skipped:
        lines += ["", "Не вложены (слишком большой размер письма), доступны в календаре: " + ", ".join(skipped)]
    if method == "CANCEL":
        lines += ["", "Если событие осталось в вашем календаре, откройте вложенный файл invite.ics — оно будет удалено."]
    else:
        lines += ["", "Чтобы добавить событие в свой календарь, откройте вложенный файл invite.ics."]
    body = "\n".join(lines)
    reply_to = [event.owner.email] if event.owner_id and event.owner.email else None

    sender = mailer.sender_for(event)
    messages = []
    for email in emails:
        msg = EmailMultiAlternatives(subject, body, sender, [email], reply_to=reply_to)
        # text/calendar как альтернативная часть — почтовые клиенты показывают кнопки «Принять/Отклонить»,
        # а Exchange/Outlook сами ставят событие в календарь получателя
        # кодировку не указываем: Django добавит её сам, а повторный charset в заголовке сбивает разбор письма
        msg.attach_alternative(ics, f"text/calendar; method={method}")
        msg.attach("invite.ics", ics, "application/ics")
        for name, content, ctype in files:
            if ctype.startswith(("text/", "message/")):
                ctype = "application/octet-stream"  # содержимое как есть: Django перекодирует текст и разбирает письма
            msg.attach(name, content, ctype)
        messages.append(msg)
    return messages


def send_messages(messages) -> tuple[list, dict]:
    """Отправляет письма одним соединением: (куда ушло, {адрес: причина ошибки})."""
    return mailer.send(messages)

