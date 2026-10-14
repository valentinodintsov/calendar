from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="externalcalendar",
            name="provider",
            field=models.CharField(
                choices=[
                    ("google", "Google Календарь"),
                    ("exchange", "Microsoft Exchange (Outlook)"),
                    ("yandex", "Яндекс Календарь"),
                    ("mailru", "Календарь Mail.ru"),
                    ("rambler", "Рамблер Календарь"),
                    ("caldav", "Другой (CalDAV)"),
                ],
                max_length=16,
                verbose_name="Сервис",
            ),
        ),
    ]
