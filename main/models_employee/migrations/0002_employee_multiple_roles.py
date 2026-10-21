# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.db import migrations, models


def roles_to_flags(apps, schema_editor):
    """
    Одна роль → набор флагов. Каждый сотрудник — владелец своего календаря;
    менеджером он становится через делегирование (Delegation), поэтому флага у менеджера нет.
    """
    Employee = apps.get_model("models_employee", "Employee")
    for emp in Employee.objects.select_related("user"):
        emp.is_owner = True
        emp.is_observer = emp.role == "observer"
        emp.is_admin = emp.role == "admin" or emp.user.is_superuser
        emp.save(update_fields=["is_owner", "is_observer", "is_admin"])


def flags_to_roles(apps, schema_editor):
    Employee = apps.get_model("models_employee", "Employee")
    for emp in Employee.objects.all():
        emp.role = "admin" if emp.is_admin else "observer" if emp.is_observer else "owner"
        emp.save(update_fields=["role"])


class Migration(migrations.Migration):

    dependencies = [
        ("models_employee", "0001_initial"),
    ]

    operations = [
        migrations.AddField(
            model_name="employee",
            name="is_owner",
            field=models.BooleanField(
                default=True,
                help_text="Ведёт свой календарь: создаёт, читает и изменяет свои события, делегирует права.",
                verbose_name="Владелец",
            ),
        ),
        migrations.AddField(
            model_name="employee",
            name="is_observer",
            field=models.BooleanField(
                default=False,
                help_text="Читает и изменяет события всех календарей.",
                verbose_name="Наблюдатель",
            ),
        ),
        migrations.AddField(
            model_name="employee",
            name="is_admin",
            field=models.BooleanField(
                default=False,
                help_text="Полный доступ ко всем событиям и управление ролями (даёт вход в админку).",
                verbose_name="Администратор",
            ),
        ),
        migrations.RunPython(roles_to_flags, flags_to_roles),
        migrations.RemoveField(
            model_name="employee",
            name="role",
        ),
    ]
