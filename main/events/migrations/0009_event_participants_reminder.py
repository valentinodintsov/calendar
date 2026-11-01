#################################################
#
#   Программа для ЭВМ «Календарь»
#   (агрегатор событий в календарях пользователей)
#
#   © 2026 Одинцов Валентин Николаевич
#   Все права защищены. All rights reserved.
#
#   Автор и правообладатель:
#       Одинцов Валентин Николаевич
#       e-mail: valentinka_santa@mail.ru
#
#   Год создания и первого выпуска: 2026
#
#   Программа охраняется законодательством
#   Российской Федерации об авторском праве
#   (часть четвёртая ГК РФ, ст. 1225, 1259, 1261).
#   Знак охраны авторского права проставлен
#   в соответствии со ст. 1271 ГК РФ.
#
#   Копирование, распространение, модификация,
#   декомпиляция и иное использование программы
#   или её частей без письменного разрешения
#   правообладателя запрещены, если иное
#   не предусмотрено законом.
#
#################################################
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0008_attachment_external_id"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="participant_names",
            field=models.JSONField(
                blank=True, default=dict,
                help_text="{e-mail: имя} — для показа участников по имени (из адресной книги).",
                verbose_name="Имена участников",
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="reminder_minutes",
            field=models.PositiveIntegerField(
                blank=True, null=True, choices=[(0, "В момент начала"), (5, "За 5 минут"), (10, "За 10 минут"), (15, "За 15 минут"), (30, "За 30 минут"), (60, "За 1 час"), (120, "За 2 часа"), (1440, "За 1 день"), (2880, "За 2 дня"), (10080, "За 1 неделю")],
                help_text="За сколько минут до начала напомнить. Пусто — без напоминания.",
                verbose_name="Напоминание",
            ),
        ),
        migrations.AlterField(
            model_name="event", name="title", field=models.CharField(max_length=200, verbose_name="Тема"),
        ),
        migrations.AlterField(
            model_name="event", name="location", field=models.CharField(blank=True, max_length=200, verbose_name="Где"),
        ),
    ]
