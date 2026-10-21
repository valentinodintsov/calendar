# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""Точка входа WSGI: переменная application."""

import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "main.settings")

application = get_wsgi_application()
