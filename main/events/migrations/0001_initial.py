from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Event",
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
                ("title", models.CharField(max_length=200, verbose_name="Название")),
                ("description", models.TextField(blank=True, verbose_name="Описание")),
                (
                    "location",
                    models.CharField(blank=True, max_length=200, verbose_name="Место"),
                ),
                ("start", models.DateTimeField(verbose_name="Начало")),
                (
                    "end",
                    models.DateTimeField(
                        blank=True, null=True, verbose_name="Окончание"
                    ),
                ),
                (
                    "all_day",
                    models.BooleanField(default=False, verbose_name="Весь день"),
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
                        default="blue",
                        max_length=16,
                        verbose_name="Цвет",
                    ),
                ),
                (
                    "created_at",
                    models.DateTimeField(auto_now_add=True, verbose_name="Создано"),
                ),
                (
                    "updated_at",
                    models.DateTimeField(auto_now=True, verbose_name="Изменено"),
                ),
            ],
            options={
                "verbose_name": "Событие",
                "verbose_name_plural": "События",
                "ordering": ["start", "id"],
                "indexes": [
                    models.Index(fields=["start"], name="event_start_idx"),
                    models.Index(fields=["end"], name="event_end_idx"),
                ],
            },
        ),
    ]
