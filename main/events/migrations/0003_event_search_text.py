# © 2026 Одинцов Валентин Николаевич. Все права защищены.
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
