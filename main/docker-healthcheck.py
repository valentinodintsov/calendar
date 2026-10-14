"""Проверка состояния контейнера: сервер отвечает. Ответ 4xx тоже годится — так бывает, когда 127.0.0.1 нет в DJANGO_ALLOWED_HOSTS."""

import sys
import urllib.error
import urllib.request

try:
    urllib.request.urlopen("http://127.0.0.1:8000/accounts/login/", timeout=4)
except urllib.error.HTTPError as exc:
    sys.exit(1 if exc.code >= 500 else 0)
except Exception:  # noqa: BLE001
    sys.exit(1)
