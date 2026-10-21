# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0006_event_directory_guests"),
    ]

    operations = [
        migrations.AlterField(
            model_name="event",
            name="color",
            field=models.CharField(
                choices=[
                    ("red", "Красный"),
                    ("crimson", "Малиновый"),
                    ("wine", "Бордовый"),
                    ("pink", "Розовый"),
                    ("fuchsia", "Фуксия"),
                    ("purple", "Фиолетовый"),
                    ("indigo", "Индиго"),
                    ("navy", "Тёмно-синий"),
                    ("blue", "Синий"),
                    ("sky", "Голубой"),
                    ("cyan", "Бирюзовый"),
                    ("teal", "Морская волна"),
                    ("emerald", "Изумрудный"),
                    ("green", "Зелёный"),
                    ("lime", "Салатовый"),
                    ("olive", "Оливковый"),
                    ("yellow", "Жёлтый"),
                    ("orange", "Оранжевый"),
                    ("tangerine", "Мандариновый"),
                    ("brown", "Коричневый"),
                    ("sand", "Песочный"),
                    ("gray", "Серый"),
                    ("slate", "Грифельный"),
                    ("graphite", "Графитовый"),
                ],
                default="blue",
                max_length=16,
                verbose_name="Цвет",
            ),
        ),
    ]
