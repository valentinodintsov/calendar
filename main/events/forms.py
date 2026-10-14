import re

from django import forms
from django.contrib.auth import get_user_model
from django.core.validators import validate_email
from django.db.models import Q

from .models import Event


class EventForm(forms.ModelForm):
    url = forms.URLField(
        label="Ссылка",
        required=False,
        max_length=500,
        assume_scheme="https",
        error_messages={"invalid": "Введите корректный адрес, например https://example.com"},
    )
    invitees = forms.ModelMultipleChoiceField(
        label="Приглашённые",
        queryset=get_user_model().objects.filter(is_active=True),
        required=False,
        error_messages={"invalid_choice": "Пользователь %(value)s не найден."},
    )

    class Meta:
        model = Event
        fields = [
            "title",
            "description",
            "location",
            "url",
            "guest_emails",
            "directory_guests",
            "reminder_minutes",
            "start",
            "end",
            "all_day",
            "color",
            "invitees",
        ]

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields["color"].required = False

    def clean_color(self):
        return self.cleaned_data.get("color") or Event.Color.BLUE

    def clean_guest_emails(self):
        """Принимает строку (через запятую, пробел, перевод строки) или список; хранит через запятую."""
        raw = self.data.get("guest_emails", "")
        items = raw if isinstance(raw, list) else re.split(r"[\s,;]+", raw or "")
        emails, bad = [], []
        for item in (str(x).strip() for x in items):
            if not item:
                continue
            try:
                validate_email(item)
            except forms.ValidationError:
                bad.append(item)
                continue
            if item.lower() not in (e.lower() for e in emails):
                emails.append(item)
        if bad:
            raise forms.ValidationError(f"Некорректные адреса: {', '.join(bad)}")
        if len(emails) > 100:
            raise forms.ValidationError("Не больше 100 участников.")
        return ", ".join(emails)

    def clean_reminder_minutes(self):
        value = self.cleaned_data.get("reminder_minutes")
        if "reminder_minutes" not in self.data and self.instance.pk:
            return self.instance.reminder_minutes  # старые клиенты API поле не передают — не сбрасываем
        return value

    def clean(self):
        cleaned = super().clean()
        guests = [e.strip() for e in (cleaned.get("guest_emails") or "").split(",") if e.strip()]
        if "invitees" not in self.data:
            # Форма «Участники»: сотрудники, у которых есть учётная запись в календаре, становятся приглашёнными
            # (видят событие у себя) — по совпадению e-mail
            q = Q()
            for e in guests:
                q |= Q(email__iexact=e)
            cleaned["invitees"] = list(get_user_model().objects.filter(q, is_active=True)) if guests else []
        # имена участников (как в адресной книге) — только для тех, кто есть в списке
        raw_names = self.data.get("participant_names")
        if isinstance(raw_names, dict):
            keep = {e.lower() for e in guests}
            self.instance.participant_names = {
                str(k).strip().lower(): str(v).strip()[:200]
                for k, v in raw_names.items()
                if str(k).strip().lower() in keep and str(v).strip()
            }
        elif "participant_names" not in self.data and isinstance(self.instance.participant_names, dict):
            keep = {e.lower() for e in guests}
            self.instance.participant_names = {k: v for k, v in self.instance.participant_names.items() if k in keep}
        # Какие участники выбраны из адресной книги AD/LDAP (список адресов или строка через запятую).
        # Учитываются только адреса из списка гостей и адреса приглашённых сотрудников.
        # Если поле не передано — сохраняется прежняя пометка.
        if "directory_guests" in self.data:
            raw = self.data.get("directory_guests")
            items = raw if isinstance(raw, list) else re.split(r"[\s,;]+", raw or "")
            marked = {str(x).strip().lower() for x in items}
        else:
            marked = {e for e in (self.instance.directory_guests or "").split(",") if e}
        allowed = {e.strip().lower() for e in (cleaned.get("guest_emails") or "").split(",") if e.strip()}
        allowed |= {(u.email or "").strip().lower() for u in (cleaned.get("invitees") or []) if u.email}
        cleaned["directory_guests"] = Event.pack_directory_guests(marked & allowed)
        return cleaned

    def clean_title(self):
        title = self.cleaned_data["title"].strip()
        if not title:
            raise forms.ValidationError("Введите тему.")
        return title
