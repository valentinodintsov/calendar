# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.apps import AppConfig


class IntegrationsConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "integrations"
    verbose_name = "Внешние календари"

    def ready(self):
        from . import push  # noqa: F401  мгновенная отправка событий (сигналы)
