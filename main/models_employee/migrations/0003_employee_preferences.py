# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("models_employee", "0002_employee_multiple_roles"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="preferences",
            field=models.JSONField(
                blank=True,
                default=dict,
                help_text="Оформление и вид календаря этого пользователя (окно «Настройки»).",
                verbose_name="Личные настройки",
            ),
        ),
    ]
