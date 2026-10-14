from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("events", "0005_event_guest_emails"),
    ]

    operations = [
        migrations.AddField(
            model_name="event",
            name="directory_guests",
            field=models.TextField(
                blank=True,
                default="",
                help_text="Адреса гостей, выбранных из каталога AD/LDAP (в нижнем регистре, вида ,a@x,b@y,). "
                "Событие автоматически появляется в их календаре.",
                verbose_name="Гости из адресной книги",
            ),
        ),
    ]
