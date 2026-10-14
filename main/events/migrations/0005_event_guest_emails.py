from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0004_event_owner"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="guest_emails",
            field=models.TextField(
                blank=True,
                help_text="Внешние участники: адреса через запятую. Им уходят приглашения с файлом .ics.",
                verbose_name="Гости (e-mail)",
            ),
        ),
    ]
