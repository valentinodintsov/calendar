from django.db import migrations, models


class Migration(migrations.Migration):
    """Поиск контактов выполняется на сервере каталога: локальная копия адресной книги больше не нужна."""

    dependencies = [
        ("directory", "0001_initial"),
    ]

    operations = [
        migrations.DeleteModel(name="Contact"),
        migrations.RemoveField(model_name="directorysource", name="contacts_count"),
        migrations.RemoveField(model_name="directorysource", name="last_sync_at"),
        migrations.RemoveField(model_name="directorysource", name="last_summary"),
        migrations.AddField(
            model_name="directorysource",
            name="last_error_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Время последней ошибки"),
        ),
        migrations.AddField(
            model_name="directorysource",
            name="last_ok_at",
            field=models.DateTimeField(blank=True, null=True, verbose_name="Последний успешный поиск"),
        ),
        migrations.AddField(
            model_name="directorysource",
            name="max_results",
            field=models.PositiveIntegerField(
                default=50, help_text="Не больше стольких записей за один поиск.", verbose_name="Результатов на запрос"
            ),
        ),
        migrations.AlterField(
            model_name="directorysource",
            name="page_size",
            field=models.PositiveIntegerField(default=500, help_text="Для проверки подключения.", verbose_name="Размер страницы"),
        ),
    ]
