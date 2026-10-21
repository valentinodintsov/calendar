# © 2026 Одинцов Валентин Николаевич. Все права защищены.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import events.models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0001_initial"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="url",
            field=models.URLField(blank=True, max_length=500, verbose_name="Ссылка"),
        ),
        migrations.AddField(
            model_name="event",
            name="invitees",
            field=models.ManyToManyField(
                blank=True,
                related_name="invited_events",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Приглашённые",
            ),
        ),
        migrations.CreateModel(
            name="Attachment",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "file",
                    models.FileField(
                        max_length=500,
                        upload_to=events.models.attachment_path,
                        verbose_name="Файл",
                    ),
                ),
                (
                    "name",
                    models.CharField(blank=True, max_length=255, verbose_name="Имя файла"),
                ),
                (
                    "size",
                    models.PositiveBigIntegerField(
                        default=0, verbose_name="Размер, байт"
                    ),
                ),
                (
                    "uploaded_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Загружен"),
                ),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="attachments",
                        to="events.event",
                        verbose_name="Событие",
                    ),
                ),
            ],
            options={
                "verbose_name": "Вложение",
                "verbose_name_plural": "Вложения",
                "ordering": ["uploaded_at", "id"],
            },
        ),
    ]
