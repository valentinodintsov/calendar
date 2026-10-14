from django.apps import AppConfig


class ModelsEmployeeConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "models_employee"
    verbose_name = "Сотрудники и роли"

    def ready(self):
        from . import signals  # noqa: F401  регистрация обработчиков сигналов
