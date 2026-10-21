# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0005_externalcalendar_color_palette"),
    ]

    operations = [
        migrations.AddField(
            model_name="syncedevent",
            name="extra_key",
            field=models.CharField(blank=True, max_length=64, verbose_name="Отпечаток участников и вложений"),
        ),
    ]
