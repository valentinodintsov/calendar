from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models
from django.db.models.signals import post_delete
from django.dispatch import receiver
from django.urls import reverse
from django.utils import timezone


def user_display_name(user):
    return user.get_full_name() or user.get_username()


class Event(models.Model):
    class Color(models.TextChoices):
        # 24 цвета; порядок — как в палитре формы (по кругу оттенков, в конце — нейтральные)
        RED = "red", "Красный"
        CRIMSON = "crimson", "Малиновый"
        WINE = "wine", "Бордовый"
        PINK = "pink", "Розовый"
        FUCHSIA = "fuchsia", "Фуксия"
        PURPLE = "purple", "Фиолетовый"
        INDIGO = "indigo", "Индиго"
        NAVY = "navy", "Тёмно-синий"
        BLUE = "blue", "Синий"
        SKY = "sky", "Голубой"
        CYAN = "cyan", "Бирюзовый"
        TEAL = "teal", "Морская волна"
        EMERALD = "emerald", "Изумрудный"
        GREEN = "green", "Зелёный"
        LIME = "lime", "Салатовый"
        OLIVE = "olive", "Оливковый"
        YELLOW = "yellow", "Жёлтый"
        ORANGE = "orange", "Оранжевый"
        TANGERINE = "tangerine", "Мандариновый"
        BROWN = "brown", "Коричневый"
        SAND = "sand", "Песочный"
        GRAY = "gray", "Серый"
        SLATE = "slate", "Грифельный"
        GRAPHITE = "graphite", "Графитовый"

    REMINDER_CHOICES = [(0, "В момент начала"), (5, "За 5 минут"), (10, "За 10 минут"), (15, "За 15 минут"), (30, "За 30 минут"), (60, "За 1 час"), (120, "За 2 часа"), (1440, "За 1 день"), (2880, "За 2 дня"), (10080, "За 1 неделю")]

    title = models.CharField("Тема", max_length=200)
    description = models.TextField("Описание", blank=True)
    location = models.CharField("Где", max_length=200, blank=True)
    url = models.URLField("Ссылка", max_length=500, blank=True)
    guest_emails = models.TextField(
        "Гости (e-mail)",
        blank=True,
        help_text="Внешние участники: адреса через запятую. Им уходят приглашения с файлом .ics.",
    )
    directory_guests = models.TextField(
        "Гости из адресной книги",
        blank=True,
        default="",
        help_text="Адреса гостей, выбранных из каталога AD/LDAP (в нижнем регистре, вида ,a@x,b@y,). "
        "Событие автоматически появляется в их календаре.",
    )
    participant_names = models.JSONField(
        "Имена участников", default=dict, blank=True,
        help_text="{e-mail: имя} — для показа участников по имени (из адресной книги).",
    )
    reminder_minutes = models.PositiveIntegerField(
        "Напоминание", null=True, blank=True, choices=REMINDER_CHOICES,
        help_text="За сколько минут до начала напомнить. Пусто — без напоминания.",
    )
    start = models.DateTimeField("Начало")
    end = models.DateTimeField("Окончание", null=True, blank=True)
    all_day = models.BooleanField("Весь день", default=False)
    color = models.CharField(
        "Цвет", max_length=16, choices=Color.choices, default=Color.BLUE
    )
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="owned_events",
        verbose_name="Владелец",
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="created_events",
        verbose_name="Автор",
        editable=False,
    )
    invitees = models.ManyToManyField(
        settings.AUTH_USER_MODEL,
        blank=True,
        related_name="invited_events",
        verbose_name="Приглашённые",
    )
    # Текст для поиска в нижнем регистре. SQLite сравнивает без учёта регистра
    # только латиницу, поэтому для кириллицы нормализуем сами.
    search_text = models.TextField("Текст для поиска", blank=True, default="", editable=False)
    created_at = models.DateTimeField("Создано", auto_now_add=True)
    updated_at = models.DateTimeField("Изменено", auto_now=True)

    class Meta:
        ordering = ["start", "id"]
        indexes = [
            models.Index(fields=["start"], name="event_start_idx"),
            models.Index(fields=["end"], name="event_end_idx"),
        ]
        verbose_name = "Событие"
        verbose_name_plural = "События"

    def __str__(self):
        return f"{self.title} ({timezone.localtime(self.start):%d.%m.%Y %H:%M})"

    def guest_list(self):
        return [e for e in (x.strip() for x in self.guest_emails.replace(";", ",").split(",")) if e]

    def directory_guest_list(self):
        """Адреса участников, выбранных из адресной книги AD/LDAP (гости и приглашённые сотрудники), в нижнем регистре."""
        return [e for e in (self.directory_guests or "").split(",") if e]

    @staticmethod
    def pack_directory_guests(emails):
        emails = sorted({e.strip().lower() for e in emails if e and e.strip()})
        return f",{','.join(emails)}," if emails else ""

    def clean(self):
        if self.end and self.start and self.end < self.start:
            raise ValidationError({"end": "Окончание не может быть раньше начала."})

    @staticmethod
    def normalize(text):
        return (text or "").casefold()

    def build_search_text(self):
        return self.normalize(" ".join([self.title, self.location, self.description]))

    def save(self, *args, **kwargs):
        self.search_text = self.build_search_text()
        update_fields = kwargs.get("update_fields")
        if update_fields is not None:
            kwargs["update_fields"] = {*update_fields, "search_text"}
        super().save(*args, **kwargs)

    def to_dict(self, perms=None):
        """
        perms — права текущего пользователя на событие ({"read", "change"}).
        Без права чтения подробности события не отдаются (restricted: true).
        """

        def fmt(dt):
            return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M") if dt else None

        data = {
            "id": self.pk,
            "title": self.title,
            "description": self.description,
            "location": self.location,
            "url": self.url,
            "guest_emails": self.guest_list(),
            "participant_names": self.participant_names if isinstance(self.participant_names, dict) else {},
            "reminder_minutes": self.reminder_minutes,
            "directory_guests": self.directory_guest_list(),
            "start": fmt(self.start),
            "end": fmt(self.end or self.start),
            "all_day": self.all_day,
            "color": self.color,
            # .all() использует prefetch_related, если он был сделан
            "invitees": [
                {"id": u.pk, "name": user_display_name(u), "email": u.email} for u in self.invitees.all()
            ],
            "attachments": [a.to_dict() for a in self.attachments.all()],
            "owner": (
                {"id": self.owner_id, "name": user_display_name(self.owner)} if self.owner_id else None
            ),
        }
        if perms is not None:
            data["permissions"] = dict(perms)
            if not perms.get("read"):
                data.update(
                    description="",
                    location="",
                    url="",
                    guest_emails=[],
                    participant_names={},
                    directory_guests=[],
                    invitees=[],
                    attachments=[],
                    restricted=True,
                )
        return data


def attachment_path(instance, filename):
    return f"attachments/{timezone.now():%Y/%m}/{filename}"


class Attachment(models.Model):
    event = models.ForeignKey(
        Event, on_delete=models.CASCADE, related_name="attachments", verbose_name="Событие"
    )
    file = models.FileField("Файл", upload_to=attachment_path, max_length=500)
    name = models.CharField("Имя файла", max_length=255, blank=True)
    size = models.PositiveBigIntegerField("Размер, байт", default=0)
    uploaded_at = models.DateTimeField("Загружен", auto_now_add=True)
    external_id = models.CharField(
        "Идентификатор во внешнем календаре", max_length=1000, blank=True, default="", editable=False,
        help_text="Для файлов, пришедших из Exchange: по нему видно, что файл уже загружен или удалён там.",
    )

    class Meta:
        ordering = ["uploaded_at", "id"]
        verbose_name = "Вложение"
        verbose_name_plural = "Вложения"

    def __str__(self):
        return self.name

    def save(self, *args, **kwargs):
        if self.file and not self.name:
            self.name = self.file.name.rsplit("/", 1)[-1][:255]
        if self.file and not self.size:
            try:
                self.size = self.file.size
            except (OSError, ValueError):
                pass
        super().save(*args, **kwargs)

    def to_dict(self):
        return {
            "id": self.pk,
            "name": self.name,
            "size": self.size,
            "url": reverse("events:attachment-download", args=[self.pk]),
        }


@receiver(post_delete, sender=Attachment)
def _delete_attachment_file(sender, instance, **kwargs):
    """Удаляем файл с диска вместе с записью (в т.ч. при удалении события)."""
    if instance.file:
        instance.file.delete(save=False)
