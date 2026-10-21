# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.apps import AppConfig


class DirectoryConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "directory"
    verbose_name = "Адресная книга (AD / LDAP)"
