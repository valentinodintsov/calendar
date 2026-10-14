import getpass
import os

from django.core.management.base import BaseCommand, CommandError

from directory.ldap_client import diagnose
from models_employee import ad_auth


class Command(BaseCommand):
    help = (
        "Проверка входа через Active Directory: подключение к контроллеру домена, проверка пароля, "
        "данные учётной записи. Пользователь календаря не создаётся и не изменяется."
    )

    def add_arguments(self, parser):
        parser.add_argument("login", nargs="?", help="Логин в домене: ivanov, YANAO\\ivanov или ivanov@yanao.ru")
        parser.add_argument("--diagnose", action="store_true", help="Только DNS и доступность LDAP-портов (без входа)")

    def handle(self, *args, **opts):
        cfg = ad_auth.connection_settings()
        self.stdout.write(
            f"Домен: {ad_auth.domain() or '—'}; серверы: {cfg.host}; шифрование: {cfg.security}; "
            f"проверка сертификата: {'да' if cfg.verify_certificate else 'нет'}; Base DN: {cfg.base_dn}"
        )
        if opts["diagnose"]:
            for line in diagnose(cfg):
                self.stdout.write("    " + line)
            return
        if not opts["login"]:
            raise CommandError("Укажите логин или --diagnose.")
        login = ad_auth.parse_login(opts["login"])
        if login is None:
            raise CommandError("Некорректный логин или чужой домен.")
        # пароль — из переменной окружения AD_AUTH_CHECK_PASSWORD или с клавиатуры (в аргументах его видно в списке процессов)
        password = os.environ.get("AD_AUTH_CHECK_PASSWORD") or getpass.getpass(f"Пароль {login}@{ad_auth.domain()}: ")
        try:
            account = ad_auth.fetch_account(login, password)
        except ad_auth.ADAuthError as exc:
            self.stderr.write(self.style.ERROR(exc.message or "Неверное имя пользователя или пароль."))
            if exc.message == ad_auth.UNAVAILABLE:
                for line in diagnose(cfg):
                    self.stderr.write("    " + line)
            raise CommandError("Вход не выполнен.") from exc
        self.stdout.write(self.style.SUCCESS(f"Вход выполнен: {account.login}"))
        self.stdout.write(f"    DN: {account.dn}")
        self.stdout.write(f"    objectGUID: {account.guid}")
        self.stdout.write(f"    Имя: {account.last_name} {account.first_name}".rstrip())
        self.stdout.write(f"    E-mail: {account.email or '—'}")
        self.stdout.write(f"    Должность: {account.title or '—'}")
        if account.is_admin is not None:
            self.stdout.write(f"    Администратор календаря: {'да' if account.is_admin else 'нет'}")
