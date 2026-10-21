# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.contrib.auth.forms import AuthenticationForm
from django.core.exceptions import ValidationError


class LoginForm(AuthenticationForm):
    """Форма входа: показывает причину отказа, которую сообщил домен (пароль истёк, запись заблокирована, …)."""

    def get_invalid_login_error(self):
        reason = getattr(self.request, "ad_auth_error", "")
        if reason:
            return ValidationError(reason, code="invalid_login")
        return ValidationError("Неверное имя пользователя или пароль.", code="invalid_login")
