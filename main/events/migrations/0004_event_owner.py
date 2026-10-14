import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


def assign_owner(apps, schema_editor):
    """Существующим событиям назначаем владельцем первого суперпользователя (если он есть)."""
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    Event = apps.get_model("events", "Event")
    admin = User.objects.filter(is_superuser=True).order_by("pk").first()
    if admin:
        Event.objects.filter(owner__isnull=True).update(owner=admin)


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0003_event_search_text"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="owner",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="owned_events",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Владелец",
            ),
        ),
        migrations.AddField(
            model_name="event",
            name="created_by",
            field=models.ForeignKey(
                blank=True,
                editable=False,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="created_events",
                to=settings.AUTH_USER_MODEL,
                verbose_name="Автор",
            ),
        ),
        migrations.RunPython(assign_owner, migrations.RunPython.noop),
    ]
