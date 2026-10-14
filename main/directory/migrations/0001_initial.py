import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="DirectorySource",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(help_text="Например: YG", max_length=100, verbose_name="Название")),
                ("kind", models.CharField(choices=[("ad", "Active Directory"), ("ldap", "LDAP (OpenLDAP, 389 DS, FreeIPA, почтовый сервер)")], default="ldap", max_length=8, verbose_name="Тип каталога")),
                ("host", models.CharField(help_text="Например: email.yanao.ru. Несколько серверов (резерв) — через запятую; можно ldaps://имя:порт.", max_length=500, verbose_name="Имя сервера")),
                ("port", models.PositiveIntegerField(blank=True, help_text="636 — LDAPS, 389 — LDAP/StartTLS, 3269 — глобальный каталог AD по SSL.", null=True, verbose_name="Порт")),
                ("base_dn", models.CharField(help_text="Например: cn=yg.loc или DC=yg,DC=loc", max_length=500, verbose_name="Корневой элемент (Base DN)")),
                ("bind_dn", models.CharField(blank=True, help_text="Например: user@yanao.ru, DOMAIN\\user или cn=reader,dc=example,dc=com. Пусто — анонимный вход.", max_length=500, verbose_name="Имя пользователя (Bind DN)")),
                ("secret", models.TextField(blank=True, editable=False, verbose_name="Пароль (зашифрован)")),
                ("security", models.CharField(choices=[("auto", "Автоматически (порт 636 — LDAPS)"), ("ssl", "LDAPS (SSL/TLS)"), ("starttls", "StartTLS"), ("none", "Без шифрования")], default="auto", max_length=10, verbose_name="Шифрование")),
                ("verify_certificate", models.BooleanField(default=True, verbose_name="Проверять сертификат сервера")),
                ("ca_cert_file", models.CharField(blank=True, help_text="Путь к .pem на сервере календаря — для сертификатов внутреннего центра сертификации.", max_length=500, verbose_name="Файл корневого сертификата (CA)")),
                ("search_filter", models.CharField(blank=True, help_text="Пусто — фильтр по умолчанию для выбранного типа каталога.", max_length=1000, verbose_name="Фильтр поиска")),
                ("attribute_map", models.JSONField(blank=True, default=dict, help_text='Только отличия от стандартного, например {"department": "departmentNumber", "id": "nsUniqueId"}.', verbose_name="Сопоставление атрибутов")),
                ("page_size", models.PositiveIntegerField(default=500, verbose_name="Размер страницы")),
                ("enabled", models.BooleanField(default=True, verbose_name="Включён")),
                ("last_sync_at", models.DateTimeField(blank=True, null=True, verbose_name="Последняя синхронизация")),
                ("last_error", models.TextField(blank=True, verbose_name="Последняя ошибка")),
                ("last_summary", models.CharField(blank=True, max_length=255, verbose_name="Итог последней синхронизации")),
                ("contacts_count", models.PositiveIntegerField(default=0, verbose_name="Контактов")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="Создан")),
            ],
            options={
                "verbose_name": "Каталог (AD / LDAP)",
                "verbose_name_plural": "Каталоги (AD / LDAP)",
                "ordering": ["name"],
            },
        ),
        migrations.CreateModel(
            name="Contact",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("external_id", models.CharField(max_length=255, verbose_name="Идентификатор в каталоге")),
                ("dn", models.CharField(blank=True, max_length=1000, verbose_name="DN")),
                ("display_name", models.CharField(max_length=255, verbose_name="Имя")),
                ("last_name", models.CharField(blank=True, max_length=150, verbose_name="Фамилия")),
                ("first_name", models.CharField(blank=True, max_length=150, verbose_name="Имя (имя)")),
                ("middle_name", models.CharField(blank=True, max_length=150, verbose_name="Отчество")),
                ("email", models.CharField(blank=True, max_length=254, verbose_name="E-mail")),
                ("phone", models.CharField(blank=True, max_length=64, verbose_name="Телефон")),
                ("mobile", models.CharField(blank=True, max_length=64, verbose_name="Мобильный")),
                ("title", models.CharField(blank=True, max_length=255, verbose_name="Должность")),
                ("department", models.CharField(blank=True, max_length=255, verbose_name="Подразделение")),
                ("company", models.CharField(blank=True, max_length=255, verbose_name="Организация")),
                ("office", models.CharField(blank=True, max_length=255, verbose_name="Кабинет / офис")),
                ("search_text", models.TextField(blank=True, editable=False, verbose_name="Текст для поиска")),
                ("synced_at", models.DateTimeField(auto_now=True, verbose_name="Обновлён")),
                ("source", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="contacts", to="directory.directorysource", verbose_name="Каталог")),
                ("user", models.ForeignKey(blank=True, help_text="Определяется по совпадению e-mail.", null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="directory_contacts", to=settings.AUTH_USER_MODEL, verbose_name="Пользователь календаря")),
            ],
            options={
                "verbose_name": "Контакт",
                "verbose_name_plural": "Адресная книга",
                "ordering": ["display_name", "id"],
                "constraints": [models.UniqueConstraint(fields=("source", "external_id"), name="contact_unique_external_id")],
                "indexes": [
                    models.Index(fields=["display_name"], name="contact_name_idx"),
                    models.Index(fields=["email"], name="contact_email_idx"),
                ],
            },
        ),
    ]
