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
