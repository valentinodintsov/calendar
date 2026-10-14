from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("integrations", "0003_guestdelivery"),
    ]

    operations = [
        migrations.CreateModel(
            name="MailServer",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("name", models.CharField(default="Почта", max_length=100, verbose_name="Название")),
                ("host", models.CharField(help_text="Например, email.yanao.ru", max_length=255, verbose_name="Сервер SMTP")),
                ("port", models.PositiveIntegerField(default=587, verbose_name="Порт")),
                (
                    "security",
                    models.CharField(
                        choices=[
                            ("starttls", "STARTTLS (обычно порт 587)"),
                            ("ssl", "SSL/TLS (обычно порт 465)"),
                            ("none", "Без шифрования (порт 25)"),
                        ],
                        default="starttls",
                        max_length=8,
                        verbose_name="Шифрование",
                    ),
                ),
                (
                    "username",
                    models.CharField(
                        blank=True,
                        help_text="Учётная запись для отправки (например, calendar@yanao.ru). Пусто — без входа (если сервер разрешает).",
                        max_length=255,
                        verbose_name="Логин",
                    ),
                ),
                ("secret", models.TextField(blank=True, editable=False, verbose_name="Пароль (зашифрован)")),
                (
                    "from_email",
                    models.CharField(
                        help_text="Обычно совпадает с логином: Exchange не даёт отправлять от чужого адреса без права «Отправить как».",
                        max_length=255,
                        verbose_name="Адрес отправителя",
                    ),
                ),
                ("verify_certificate", models.BooleanField(default=True, verbose_name="Проверять сертификат сервера")),
                ("timeout", models.PositiveIntegerField(default=15, verbose_name="Тайм-аут, сек")),
                ("enabled", models.BooleanField(default=True, verbose_name="Включён")),
                ("last_ok_at", models.DateTimeField(blank=True, null=True, verbose_name="Последняя успешная отправка")),
                ("last_error", models.TextField(blank=True, verbose_name="Последняя ошибка")),
                ("last_error_at", models.DateTimeField(blank=True, null=True, verbose_name="Время последней ошибки")),
            ],
            options={
                "verbose_name": "Почтовый сервер",
                "verbose_name_plural": "Почтовый сервер (отправка приглашений)",
                "ordering": ["-enabled", "pk"],
            },
        ),
    ]
