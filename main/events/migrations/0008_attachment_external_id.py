from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0007_event_color_palette"),
    ]

    operations = [
        migrations.AddField(
            model_name="attachment",
            name="external_id",
            field=models.CharField(
                blank=True,
                default="",
                editable=False,
                help_text="Для файлов, пришедших из Exchange: по нему видно, что файл уже загружен или удалён там.",
                max_length=1000,
                verbose_name="Идентификатор во внешнем календаре",
            ),
        ),
    ]
