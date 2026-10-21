# © 2026 Одинцов Валентин Николаевич. Все права защищены.
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models

import models_employee.models


def create_profiles(apps, schema_editor):
    """Профиль сотрудника для всех существующих пользователей."""
    app_label, model_name = settings.AUTH_USER_MODEL.split(".")
    User = apps.get_model(app_label, model_name)
    Employee = apps.get_model("models_employee", "Employee")
    default = getattr(settings, "EMPLOYEE_DEFAULT_ROLE", "observer")
    for user in User.objects.all():
        Employee.objects.get_or_create(
            user=user, defaults={"role": "admin" if user.is_superuser else default}
        )


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        ("events", "0004_event_owner"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="Employee",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("owner", "Владелец"),
                            ("manager", "Менеджер"),
                            ("observer", "Наблюдатель"),
                            ("admin", "Администратор"),
                        ],
                        default=models_employee.models.default_role,
                        max_length=16,
                        verbose_name="Роль",
                    ),
                ),
                ("position", models.CharField(blank=True, max_length=150, verbose_name="Должность")),
                (
                    "user",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="employee",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Пользователь",
                    ),
                ),
            ],
            options={
                "verbose_name": "Сотрудник",
                "verbose_name_plural": "Сотрудники",
                "ordering": ["user__username"],
            },
        ),
        migrations.CreateModel(
            name="Delegation",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True, primary_key=True, serialize=False, verbose_name="ID"
                    ),
                ),
                (
                    "can_create",
                    models.BooleanField(
                        default=False,
                        help_text="Создание новых событий в календаре владельца.",
                        verbose_name="Запись",
                    ),
                ),
                ("can_read", models.BooleanField(default=False, verbose_name="Чтение")),
                (
                    "can_change",
                    models.BooleanField(default=False, help_text="Включает чтение.", verbose_name="Изменение"),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создано")),
                ("updated_at", models.DateTimeField(auto_now=True, verbose_name="Изменено")),
                (
                    "event",
                    models.ForeignKey(
                        blank=True,
                        help_text="Пусто — права на все события владельца.",
                        null=True,
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="delegations",
                        to="events.event",
                        verbose_name="Событие",
                    ),
                ),
                (
                    "manager",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="received_delegations",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Менеджер",
                    ),
                ),
                (
                    "owner",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="granted_delegations",
                        to=settings.AUTH_USER_MODEL,
                        verbose_name="Владелец",
                    ),
                ),
            ],
            options={
                "verbose_name": "Делегирование прав",
                "verbose_name_plural": "Делегирование прав",
                "ordering": ["owner_id", "manager_id", "event_id"],
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(("event__isnull", True)),
                        fields=("owner", "manager"),
                        name="delegation_unique_owner_level",
                    ),
                    models.UniqueConstraint(
                        fields=("owner", "manager", "event"),
                        name="delegation_unique_event_level",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(("owner", models.F("manager")), _negated=True),
                        name="delegation_owner_not_manager",
                    ),
                ],
            },
        ),
        migrations.RunPython(create_profiles, migrations.RunPython.noop),
    ]
