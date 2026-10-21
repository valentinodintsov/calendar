# © 2026 Одинцов Валентин Николаевич. Все права защищены.
"""
Шифрование паролей приложений и OAuth-токенов в базе (Fernet, библиотека cryptography).

Ключ — настройка INTEGRATIONS_ENCRYPTION_KEY (base64, 32 байта: Fernet.generate_key()).
Если она не задана, ключ выводится из SECRET_KEY. При смене ключа сохранённые
пароли и токены перестанут расшифровываться — календари придётся подключить заново.
"""

import base64
import hashlib
import json

from cryptography.fernet import Fernet, InvalidToken
from django.conf import settings


def _fernet():
    key = getattr(settings, "INTEGRATIONS_ENCRYPTION_KEY", "") or ""
    if not key:
        key = base64.urlsafe_b64encode(hashlib.sha256(("integrations:" + settings.SECRET_KEY).encode()).digest())
    return Fernet(key)


def encrypt(data: dict) -> str:
    return _fernet().encrypt(json.dumps(data).encode("utf-8")).decode("ascii")


def decrypt(token: str) -> dict:
    if not token:
        return {}
    try:
        return json.loads(_fernet().decrypt(token.encode("ascii")).decode("utf-8"))
    except (InvalidToken, ValueError):
        return {}
