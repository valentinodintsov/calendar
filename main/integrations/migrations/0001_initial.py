import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("events", "0004_event_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="ExternalCalendar",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                (
                    "provider",
                    models.CharField(
                        choices=[
                            ("google", "Google Календарь"),
                            ("yandex", "Яндекс Календарь"),
                            ("mailru", "Календарь Mail.ru"),
                            ("rambler", "Рамблер Календарь"),
                            ("caldav", "Другой (CalDAV)"),
                        ],
                        max_length=16,
                        verbose_name="Сервис",
                    ),
                ),
                ("calendar_id", models.CharField(max_length=1000, verbose_name="Календарь (адрес или id)")),
                ("calendar_name", models.CharField(blank=True, max_length=255, verbose_name="Название календаря")),
                ("server_url", models.CharField(blank=True, max_length=500, verbose_name="Сервер CalDAV")),
                ("username", models.CharField(blank=True, max_length=255, verbose_name="Логин")),
                ("secret", models.TextField(blank=True, editable=False, verbose_name="Пароль/токен (зашифрован)")),
                (
                    "direction",
                    models.CharField(
                        choices=[("both", "В обе стороны"), ("import", "Только оттуда к нам"), ("export", "Только от нас туда")],
                        default="both",
                        max_length=8,
                        verbose_name="Направление",
                    ),
                ),
                (
                    "color",
                    models.CharField(
                        choices=[
                            ("blue", "Синий"),
                            ("green", "Зелёный"),
                            ("red", "Красный"),
                            ("orange", "Оранжевый"),
                            ("purple", "Фиолетовый"),
                            ("gray", "Серый"),
                        ],
                        default="gray",
                        max_length=16,
                        verbose_name="Цвет событий",
                    ),
                ),
                ("remote_read_only", models.BooleanField(default=False, verbose_name="Календарь только для чтения")),
                ("enabled", models.BooleanField(default=True, verbose_name="Синхронизировать")),
                ("last_sync_at", models.DateTimeField(blank=True, null=True, verbose_name="Последняя синхронизация")),
                ("last_error", models.TextField(blank=True, verbose_name="Последняя ошибка")),
                ("last_summary", models.CharField(blank=True, max_length=255, verbose_name="Итог последней синхронизации")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Подключён")),
                (
                    "user",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="external_calendars",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Пользователь",
                    ),
                ),
            ],
            options={
                "verbose_name": "Внешний календарь",
                "verbose_name_plural": "Внешние календари",
                "ordering": ["user_id", "provider", "calendar_name"],
                "constraints": [
                    models.UniqueConstraint(fields=("user", "provider", "calendar_id"), name="external_calendar_unique")
                ],
            },
        ),
        migrations.CreateModel(
            name="SyncedEvent",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("uid", models.CharField(max_length=500, verbose_name="UID")),
                ("remote_id", models.CharField(max_length=1000, verbose_name="Идентификатор во внешнем календаре")),
                ("etag", models.CharField(blank=True, max_length=255, verbose_name="ETag")),
                ("local_key", models.CharField(blank=True, max_length=64, verbose_name="Отпечаток нашего события")),
                ("remote_key", models.CharField(blank=True, max_length=64, verbose_name="Отпечаток внешнего события")),
                (
                    "origin",
                    models.CharField(
                        choices=[("local", "Создано у нас"), ("remote", "Импортировано")], max_length=8, verbose_name="Источник"
                    ),
                ),
                ("read_only", models.BooleanField(default=False, verbose_name="Только для чтения")),
                ("synced_at", models.DateTimeField(auto_now=True, verbose_name="Синхронизировано")),
                (
                    "calendar",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="links",
                        to="integrations.externalcalendar",
                        verbose_name="Календарь",
                    ),
                ),
                (
                    "event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="external_links",
                        to="events.event",
                        verbose_name="Событие",
                    ),
                ),
            ],
            options={
                "verbose_name": "Синхронизированное событие",
                "verbose_name_plural": "Синхронизированные события",
                "constraints": [models.UniqueConstraint(fields=("calendar", "uid"), name="synced_event_unique_uid")],
            },
        ),
    ]
