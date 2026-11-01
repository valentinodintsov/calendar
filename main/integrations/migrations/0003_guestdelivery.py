#################################################
#
#   Программа для ЭВМ «Календарь»
#   (агрегатор событий в календарях пользователей)
#
#   © 2026 Одинцов Валентин Николаевич
#   Все права защищены. All rights reserved.
#
#   Автор и правообладатель:
#       Одинцов Валентин Николаевич
#       e-mail: valentinka_santa@mail.ru
#
#   Год создания и первого выпуска: 2026
#
#   Программа охраняется законодательством
#   Российской Федерации об авторском праве
#   (часть четвёртая ГК РФ, ст. 1225, 1259, 1261).
#   Знак охраны авторского права проставлен
#   в соответствии со ст. 1271 ГК РФ.
#
#   Копирование, распространение, модификация,
#   декомпиляция и иное использование программы
#   или её частей без письменного разрешения
#   правообладателя запрещены, если иное
#   не предусмотрено законом.
#
#################################################
import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0006_event_directory_guests"),
        ("integrations", "0002_exchange"),
    ]

    operations = [
        migrations.CreateModel(
            name="GuestDelivery",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("email", models.CharField(max_length=254, verbose_name="E-mail участника")),
                (
                    "channel",
                    models.CharField(
                        choices=[("exchange", "Собрание Exchange"), ("email", "Письмо с .ics")],
                        max_length=8,
                        verbose_name="Способ",
                    ),
                ),
                ("content_key", models.CharField(blank=True, max_length=64, verbose_name="Отпечаток отправленного события")),
                ("sent_at", models.DateTimeField(auto_now=True, verbose_name="Отправлено")),
                (
                    "event",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="guest_deliveries",
                        to="events.event",
                        verbose_name="Событие",
                    ),
                ),
            ],
            options={
                "verbose_name": "Доставка участнику",
                "verbose_name_plural": "Доставка участникам",
                "constraints": [models.UniqueConstraint(fields=("event", "email"), name="guest_delivery_unique")],
            },
        ),
    ]
