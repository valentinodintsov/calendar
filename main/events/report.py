# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Отчёт по событиям календаря за период в формате Word (.docx).

Документ собирается без сторонних библиотек: .docx — это zip-архив с XML (WordprocessingML).
Лист A4 в альбомной ориентации: шапка (период, пользователь, число событий) и таблица событий.
"""

from __future__ import annotations

import datetime
import io
import re
import zipfile
from xml.sax.saxutils import escape

from django.utils import timezone

from .models import user_display_name

FILENAME = "Отчет по событиям.docx"
CONTENT_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"

W_NS = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
# Колонки таблицы: заголовок и ширина в twips (1/1440 дюйма); в сумме — ширина листа без полей
COLUMNS = [("№", 560), ("Дата", 1500), ("Время", 1300), ("Событие", 4900), ("Место", 2200), ("Участники", 2900),
           ("Календарь", 1778)]
PAGE_W, PAGE_H, MARGIN = 16838, 11906, 850  # A4 альбомный, поля 1,5 см
# Символы, недопустимые в XML 1.0 (могут прийти в описании из внешнего календаря)
INVALID_XML = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f￾￿]")


# ---------------------------------------------------------------- данные события


def _local(dt):
    return timezone.localtime(dt)


def event_date(event) -> str:
    start, end = _local(event.start).date(), _local(event.end or event.start).date()
    return f"{start:%d.%m.%Y}" if start == end else f"{start:%d.%m.%Y} – {end:%d.%m.%Y}"


def event_time(event) -> str:
    if event.all_day:
        return "весь день"
    start = _local(event.start)
    if not event.end or event.end == event.start:
        return f"{start:%H:%M}"
    return f"{start:%H:%M}–{_local(event.end):%H:%M}"


def event_participants(event) -> list[str]:
    """Приглашённые сотрудники и гости: по имени, если оно известно, иначе по адресу. Без повторов."""
    names = event.participant_names if isinstance(event.participant_names, dict) else {}
    names = {str(k).lower(): str(v).strip() for k, v in names.items() if v}
    result, seen = [], set()
    for user in event.invitees.all():
        key = (user.email or "").strip().lower() or f"user:{user.pk}"
        if key not in seen:
            seen.add(key)
            result.append(user_display_name(user))
    for email in event.guest_list():
        key = email.lower()
        if key not in seen:
            seen.add(key)
            result.append(names.get(key) or email)
    return result


def event_row(number: int, event) -> list:
    """Ячейки строки таблицы; ячейка — список абзацев (текст, жирный)."""
    details = [(event.title, True)]
    if event.description.strip():
        details.append((event.description.strip(), False))
    if event.url:
        details.append((event.url, False))
    return [
        [(str(number), False)],
        [(event_date(event), False)],
        [(event_time(event), False)],
        details,
        [(event.location, False)],
        [(name, False) for name in event_participants(event)] or [("", False)],
        [(user_display_name(event.owner) if event.owner_id else "", False)],
    ]


# ---------------------------------------------------------------- WordprocessingML


def _run(text: str, bold=False, size=None) -> str:
    props = ("<w:b/>" if bold else "") + (f'<w:sz w:val="{size}"/><w:szCs w:val="{size}"/>' if size else "")
    lines = INVALID_XML.sub("", text or "").replace("\r\n", "\n").replace("\r", "\n").split("\n")
    body = "<w:br/>".join(f'<w:t xml:space="preserve">{escape(line)}</w:t>' for line in lines)
    return f"<w:r>{f'<w:rPr>{props}</w:rPr>' if props else ''}{body}</w:r>"


def _paragraph(text: str, bold=False, size=None, align=None, after=0) -> str:
    props = f'<w:spacing w:before="0" w:after="{after}"/>' + (f'<w:jc w:val="{align}"/>' if align else "")
    return f"<w:p><w:pPr>{props}</w:pPr>{_run(text, bold, size)}</w:p>"


def _cell(paragraphs, width: int, header=False) -> str:
    props = f'<w:tcW w:w="{width}" w:type="dxa"/>' + ('<w:shd w:val="clear" w:color="auto" w:fill="E7E6E6"/>' if header else "")
    body = "".join(_paragraph(text, bold or header, 20, "center" if header else None) for text, bold in paragraphs)
    return f"<w:tc><w:tcPr>{props}</w:tcPr>{body}</w:tc>"


def _table(rows) -> str:
    borders = "".join(
        f'<w:{side} w:val="single" w:sz="4" w:space="0" w:color="000000"/>'
        for side in ("top", "left", "bottom", "right", "insideH", "insideV")
    )
    margins = "".join(f'<w:{side} w:w="80" w:type="dxa"/>' for side in ("top", "left", "bottom", "right"))
    head = "".join(_cell([(title, True)], width, header=True) for title, width in COLUMNS)
    body = "".join(
        '<w:tr><w:trPr><w:cantSplit/></w:trPr>'
        + "".join(_cell(cell, width) for cell, (_, width) in zip(row, COLUMNS))
        + "</w:tr>"
        for row in rows
    )
    grid = "".join(f'<w:gridCol w:w="{width}"/>' for _, width in COLUMNS)
    return (
        f'<w:tbl><w:tblPr><w:tblW w:w="{sum(w for _, w in COLUMNS)}" w:type="dxa"/><w:tblBorders>{borders}</w:tblBorders>'
        f'<w:tblLayout w:type="fixed"/><w:tblCellMar>{margins}</w:tblCellMar></w:tblPr>'
        f"<w:tblGrid>{grid}</w:tblGrid>"
        f"<w:tr><w:trPr><w:tblHeader/></w:trPr>{head}</w:tr>{body}</w:tbl>"  # шапка повторяется на каждой странице
    )


def _document(events, start, end, user, generated) -> str:
    events = list(events)
    parts = [
        _paragraph("Отчет по событиям", bold=True, size=28, align="center", after=60),
        _paragraph(f"за период с {start:%d.%m.%Y} по {end:%d.%m.%Y}", size=24, align="center", after=200),
        _paragraph(f"Пользователь: {user_display_name(user)}", size=22),
        _paragraph(f"Отчет сформирован: {_local(generated):%d.%m.%Y %H:%M}", size=22),
        _paragraph(f"Всего событий: {len(events)}", size=22, after=200),
    ]
    if events:
        parts.append(_table(event_row(i, e) for i, e in enumerate(events, 1)))
        parts.append(_paragraph(""))  # документ не должен заканчиваться таблицей
    else:
        parts.append(_paragraph("За выбранный период событий нет.", size=24))
    section = (
        f'<w:sectPr><w:pgSz w:w="{PAGE_W}" w:h="{PAGE_H}" w:orient="landscape"/>'
        f'<w:pgMar w:top="{MARGIN}" w:right="{MARGIN}" w:bottom="{MARGIN}" w:left="{MARGIN}" '
        'w:header="400" w:footer="400" w:gutter="0"/></w:sectPr>'
    )
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        f'<w:document xmlns:w="{W_NS}"><w:body>{"".join(parts)}{section}</w:body></w:document>'
    )


CONTENT_TYPES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">'
    '<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>'
    '<Default Extension="xml" ContentType="application/xml"/>'
    '<Override PartName="/word/document.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>'
    '<Override PartName="/word/styles.xml" '
    'ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>'
    '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>'
    "</Types>"
)
ROOT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" '
    'Target="word/document.xml"/>'
    '<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" '
    'Target="docProps/core.xml"/>'
    "</Relationships>"
)
DOCUMENT_RELS = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
    '<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" '
    'Target="styles.xml"/>'
    "</Relationships>"
)
STYLES = (
    '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
    f'<w:styles xmlns:w="{W_NS}"><w:docDefaults><w:rPrDefault><w:rPr>'
    '<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:cs="Times New Roman" w:eastAsia="Times New Roman"/>'
    '<w:sz w:val="24"/><w:szCs w:val="24"/><w:lang w:val="ru-RU"/>'
    "</w:rPr></w:rPrDefault></w:docDefaults>"
    '<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>'
    "</w:styles>"
)


def _core(generated) -> str:
    stamp = generated.astimezone(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
        '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties" '
        'xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/" '
        'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
        "<dc:title>Отчет по событиям</dc:title><dc:creator>Календарь</dc:creator>"
        f'<dcterms:created xsi:type="dcterms:W3CDTF">{stamp}</dcterms:created>'
        "</cp:coreProperties>"
    )


def build_report(events, start, end, user, generated=None) -> bytes:
    """
    Содержимое файла .docx. events — события в нужном порядке (с owner, invitees);
    start и end — границы периода (даты, включительно); user — для кого отчёт.
    """
    generated = generated or timezone.now()
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("_rels/.rels", ROOT_RELS)
        archive.writestr("word/document.xml", _document(events, start, end, user, generated))
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        archive.writestr("word/styles.xml", STYLES)
        archive.writestr("docProps/core.xml", _core(generated))
    return buffer.getvalue()
