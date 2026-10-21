# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.conf import settings
from django.db import models

from integrations import crypto

from .ldap_client import AD, LDAP, ConnectionSettings


class DirectorySource(models.Model):
    """
    Каталог (Active Directory / LDAP), в котором ищутся контакты адресной книги. Настраивает администратор.
    Контакты не копируются в базу календаря: каждый поиск выполняется на сервере каталога.
    """

    class Kind(models.TextChoices):
        AD = AD, "Active Directory"
        LDAP = LDAP, "LDAP (OpenLDAP, 389 DS, FreeIPA, почтовый сервер)"

    class Security(models.TextChoices):
        AUTO = "auto", "Автоматически (порт 636 — LDAPS)"
        SSL = "ssl", "LDAPS (SSL/TLS)"
        STARTTLS = "starttls", "StartTLS"
        NONE = "none", "Без шифрования"

    # ---- Основное
    name = models.CharField("Название", max_length=100, help_text="Например: YG")
    kind = models.CharField("Тип каталога", max_length=8, choices=Kind.choices, default=Kind.LDAP)
    host = models.CharField(
        "Имя сервера", max_length=500,
        help_text="Например: email.yanao.ru. Несколько серверов (резерв) — через запятую; можно ldaps://имя:порт.",
    )
    port = models.PositiveIntegerField("Порт", null=True, blank=True, help_text="636 — LDAPS, 389 — LDAP/StartTLS, 3269 — глобальный каталог AD по SSL.")
    base_dn = models.CharField("Корневой элемент (Base DN)", max_length=500, help_text="Например: cn=yg.loc или DC=yg,DC=loc")
    bind_dn = models.CharField(
        "Имя пользователя (Bind DN)", max_length=500, blank=True,
        help_text="Например: user@yanao.ru, DOMAIN\\user или cn=reader,dc=example,dc=com. Пусто — анонимный вход.",
    )
    secret = models.TextField("Пароль (зашифрован)", blank=True, editable=False)

    # ---- Дополнительно
    security = models.CharField("Шифрование", max_length=10, choices=Security.choices, default=Security.AUTO)
    verify_certificate = models.BooleanField("Проверять сертификат сервера", default=True)
    ca_cert_file = models.CharField(
        "Файл корневого сертификата (CA)", max_length=500, blank=True,
        help_text="Путь к .pem на сервере календаря — для сертификатов внутреннего центра сертификации.",
    )
    search_filter = models.CharField(
        "Фильтр поиска", max_length=1000, blank=True,
        help_text="Пусто — фильтр по умолчанию для выбранного типа каталога.",
    )
    attribute_map = models.JSONField(
        "Сопоставление атрибутов", default=dict, blank=True,
        help_text='Только отличия от стандартного, например {"department": "departmentNumber", "id": "nsUniqueId"}.',
    )
    page_size = models.PositiveIntegerField("Размер страницы", default=500, help_text="Для проверки подключения.")
    max_results = models.PositiveIntegerField("Результатов на запрос", default=50, help_text="Не больше стольких записей за один поиск.")
    enabled = models.BooleanField("Включён", default=True)

    # ---- Состояние
    last_error = models.TextField("Последняя ошибка", blank=True)
    last_error_at = models.DateTimeField("Время последней ошибки", null=True, blank=True)
    last_ok_at = models.DateTimeField("Последний успешный поиск", null=True, blank=True)
    created_at = models.DateTimeField("Создан", auto_now_add=True)

    class Meta:
        ordering = ["name"]
        verbose_name = "Каталог (AD / LDAP)"
        verbose_name_plural = "Каталоги (AD / LDAP)"

    def __str__(self):
        return self.name

    @property
    def password(self) -> str:
        return crypto.decrypt(self.secret).get("password", "")

    @password.setter
    def password(self, value: str):
        self.secret = crypto.encrypt({"password": value or ""})

    def connection_settings(self) -> ConnectionSettings:
        return ConnectionSettings(
            kind=self.kind,
            host=self.host,
            port=self.port,
            security=self.security,
            verify_certificate=self.verify_certificate,
            ca_cert_file=self.ca_cert_file,
            bind_dn=self.bind_dn,
            password=self.password,
            base_dn=self.base_dn,
            search_filter=self.search_filter,
            mapping=self.attribute_map if isinstance(self.attribute_map, dict) else {},
            page_size=self.page_size or 500,
            timeout=getattr(settings, "DIRECTORY_TIMEOUT", 15),
            max_entries=getattr(settings, "DIRECTORY_MAX_ENTRIES", 50000),
        )

    def status_dict(self, admin=False):
        data = {
            "id": self.pk,
            "name": self.name,
            "kind": self.kind,
            "kind_label": self.get_kind_display(),
            "enabled": self.enabled,
        }
        if admin:
            data.update(
                host=self.host, port=self.port, base_dn=self.base_dn, bind_dn=self.bind_dn,
                last_error=self.last_error,
                last_error_at=self.last_error_at.isoformat() if self.last_error_at else None,
                last_ok_at=self.last_ok_at.isoformat() if self.last_ok_at else None,
            )
        return data
