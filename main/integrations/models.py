# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.conf import settings
from django.db import models

from events.models import Event

from . import crypto


class ExternalCalendar(models.Model):
    """Подключённый внешний календарь пользователя. События синхронизируются с его собственным календарём."""

    class Provider(models.TextChoices):
        GOOGLE = "google", "Google Календарь"
        EXCHANGE = "exchange", "Microsoft Exchange (Outlook)"
        YANDEX = "yandex", "Яндекс Календарь"
        MAILRU = "mailru", "Календарь Mail.ru"
        RAMBLER = "rambler", "Рамблер Календарь"
        CALDAV = "caldav", "Другой (CalDAV)"

    class Direction(models.TextChoices):
        BOTH = "both", "В обе стороны"
        IMPORT = "import", "Только оттуда к нам"
        EXPORT = "export", "Только от нас туда"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="external_calendars", verbose_name="Пользователь"
    )
    provider = models.CharField("Сервис", max_length=16, choices=Provider.choices)
    calendar_id = models.CharField("Календарь (адрес или id)", max_length=1000)
    calendar_name = models.CharField("Название календаря", max_length=255, blank=True)
    server_url = models.CharField("Сервер CalDAV", max_length=500, blank=True)
    username = models.CharField("Логин", max_length=255, blank=True)
    secret = models.TextField("Пароль/токен (зашифрован)", blank=True, editable=False)
    direction = models.CharField("Направление", max_length=8, choices=Direction.choices, default=Direction.BOTH)
    color = models.CharField("Цвет событий", max_length=16, choices=Event.Color.choices, default=Event.Color.GRAY)
    remote_read_only = models.BooleanField("Календарь только для чтения", default=False)
    enabled = models.BooleanField("Синхронизировать", default=True)
    last_sync_at = models.DateTimeField("Последняя синхронизация", null=True, blank=True)
    last_error = models.TextField("Последняя ошибка", blank=True)
    last_summary = models.CharField("Итог последней синхронизации", max_length=255, blank=True)
    created_at = models.DateTimeField("Подключён", auto_now_add=True)

    class Meta:
        ordering = ["user_id", "provider", "calendar_name"]
        verbose_name = "Внешний календарь"
        verbose_name_plural = "Внешние календари"
        constraints = [
            models.UniqueConstraint(fields=["user", "provider", "calendar_id"], name="external_calendar_unique"),
        ]

    def __str__(self):
        return f"{self.get_provider_display()}: {self.calendar_name or self.calendar_id}"

    # ---- учётные данные (пароль приложения или OAuth-токены) хранятся зашифрованными
    @property
    def credentials(self) -> dict:
        return crypto.decrypt(self.secret)

    @credentials.setter
    def credentials(self, value: dict):
        self.secret = crypto.encrypt(value or {})

    @property
    def can_import(self):
        return self.direction in (self.Direction.BOTH, self.Direction.IMPORT)

    @property
    def can_export(self):
        return self.direction in (self.Direction.BOTH, self.Direction.EXPORT) and not self.remote_read_only

    def to_dict(self):
        return {
            "id": self.pk,
            "provider": self.provider,
            "provider_label": self.get_provider_display(),
            "calendar_name": self.calendar_name or self.calendar_id,
            "server_url": self.server_url,
            "username": self.username,
            "direction": self.direction,
            "direction_label": self.get_direction_display(),
            "color": self.color,
            "remote_read_only": self.remote_read_only,
            "enabled": self.enabled,
            "last_sync_at": self.last_sync_at.isoformat() if self.last_sync_at else None,
            "last_error": self.last_error,
            "last_summary": self.last_summary,
        }


class SyncedEvent(models.Model):
    """Связь «наше событие ↔ событие во внешнем календаре» и состояние на момент последней синхронизации."""

    class Origin(models.TextChoices):
        LOCAL = "local", "Создано у нас"
        REMOTE = "remote", "Импортировано"

    calendar = models.ForeignKey(ExternalCalendar, on_delete=models.CASCADE, related_name="links", verbose_name="Календарь")
    event = models.ForeignKey(
        Event, on_delete=models.SET_NULL, null=True, blank=True, related_name="external_links", verbose_name="Событие"
    )
    uid = models.CharField("UID", max_length=500)
    remote_id = models.CharField("Идентификатор во внешнем календаре", max_length=1000)
    etag = models.CharField("ETag", max_length=255, blank=True)
    local_key = models.CharField("Отпечаток нашего события", max_length=64, blank=True)
    remote_key = models.CharField("Отпечаток внешнего события", max_length=64, blank=True)
    origin = models.CharField("Источник", max_length=8, choices=Origin.choices)
    read_only = models.BooleanField("Только для чтения", default=False)
    extra_key = models.CharField("Отпечаток участников и вложений", max_length=64, blank=True)
    synced_at = models.DateTimeField("Синхронизировано", auto_now=True)

    class Meta:
        verbose_name = "Синхронизированное событие"
        verbose_name_plural = "Синхронизированные события"
        constraints = [models.UniqueConstraint(fields=["calendar", "uid"], name="synced_event_unique_uid")]

    def __str__(self):
        return f"{self.calendar} — {self.uid}"


class GuestDelivery(models.Model):
    """
    Что и как отправлено в календарь участника, выбранного из адресной книги AD/LDAP.
    По этим записям видно, кому нужно приглашение, обновление или отмена.
    """

    class Channel(models.TextChoices):
        EXCHANGE = "exchange", "Собрание Exchange"
        EMAIL = "email", "Письмо с .ics"

    event = models.ForeignKey(Event, on_delete=models.CASCADE, related_name="guest_deliveries", verbose_name="Событие")
    email = models.CharField("E-mail участника", max_length=254)
    channel = models.CharField("Способ", max_length=8, choices=Channel.choices)
    content_key = models.CharField("Отпечаток отправленного события", max_length=64, blank=True)
    sent_at = models.DateTimeField("Отправлено", auto_now=True)

    class Meta:
        verbose_name = "Доставка участнику"
        verbose_name_plural = "Доставка участникам"
        constraints = [models.UniqueConstraint(fields=["event", "email"], name="guest_delivery_unique")]

    def __str__(self):
        return f"{self.event} → {self.email} ({self.get_channel_display()})"


class MailServer(models.Model):
    """
    Почтовый сервер (SMTP), через который уходят приглашения на события.
    Настраивает администратор в админке; используется первый включённый. Если ни одного нет —
    действуют настройки EMAIL_* из settings.py (переменные окружения).
    """

    class Security(models.TextChoices):
        STARTTLS = "starttls", "STARTTLS (обычно порт 587)"
        SSL = "ssl", "SSL/TLS (обычно порт 465)"
        NONE = "none", "Без шифрования (порт 25)"

    name = models.CharField("Название", max_length=100, default="Почта")
    host = models.CharField("Сервер SMTP", max_length=255, help_text="Например, email.yanao.ru")
    port = models.PositiveIntegerField("Порт", default=587)
    security = models.CharField("Шифрование", max_length=8, choices=Security.choices, default=Security.STARTTLS)
    username = models.CharField(
        "Логин", max_length=255, blank=True,
        help_text="Учётная запись для отправки (например, calendar@yanao.ru). Пусто — без входа (если сервер разрешает).",
    )
    secret = models.TextField("Пароль (зашифрован)", blank=True, editable=False)
    from_email = models.CharField(
        "Адрес отправителя", max_length=255,
        help_text="Обычно совпадает с логином: Exchange не даёт отправлять от чужого адреса без права «Отправить как».",
    )
    verify_certificate = models.BooleanField("Проверять сертификат сервера", default=True)
    timeout = models.PositiveIntegerField("Тайм-аут, сек", default=15)
    enabled = models.BooleanField("Включён", default=True)
    last_ok_at = models.DateTimeField("Последняя успешная отправка", null=True, blank=True)
    last_error = models.TextField("Последняя ошибка", blank=True)
    last_error_at = models.DateTimeField("Время последней ошибки", null=True, blank=True)

    class Meta:
        ordering = ["-enabled", "pk"]
        verbose_name = "Почтовый сервер"
        verbose_name_plural = "Почтовый сервер (отправка приглашений)"

    def __str__(self):
        return f"{self.name} ({self.host}:{self.port})"

    @property
    def password(self) -> str:
        return crypto.decrypt(self.secret).get("password", "")

    @password.setter
    def password(self, value: str):
        self.secret = crypto.encrypt({"password": value or ""})
