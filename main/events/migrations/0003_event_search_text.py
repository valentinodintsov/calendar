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


def fill_search_text(apps, schema_editor):
    Event = apps.get_model("events", "Event")
    for event in Event.objects.all().only("id", "title", "location", "description"):
        text = " ".join([event.title, event.location, event.description]).casefold()
        Event.objects.filter(pk=event.pk).update(search_text=text)


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0002_event_url_invitees_attachment"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="search_text",
            field=models.TextField(
                blank=True, default="", editable=False, verbose_name="Текст для поиска"
            ),
        ),
        migrations.RunPython(fill_search_text, migrations.RunPython.noop),
    ]
