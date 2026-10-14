from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html

from models_employee.admin import RoleAdminMixin

from . import mailer
from .models import ExternalCalendar, GuestDelivery, MailServer, SyncedEvent
from .sync import sync_calendar


@admin.register(ExternalCalendar)
class ExternalCalendarAdmin(RoleAdminMixin, admin.ModelAdmin):
    list_display = ("user", "provider", "calendar_name", "direction", "enabled", "last_sync_at", "last_summary", "last_error")
    list_filter = ("provider", "direction", "enabled")
    search_fields = ("user__username", "calendar_name", "username")
    list_select_related = ("user",)
    readonly_fields = ("last_sync_at", "last_error", "last_summary", "created_at")
    actions = ["sync_now"]

    @admin.action(description="Синхронизировать сейчас")
    def sync_now(self, request, queryset):
        for cal in queryset:
            res = sync_calendar(cal)
            level = messages.ERROR if res.errors else messages.SUCCESS
            self.message_user(request, f"{cal}: {res.summary()}", level)


@admin.register(SyncedEvent)
class SyncedEventAdmin(RoleAdminMixin, admin.ModelAdmin):
    list_display = ("calendar", "event", "origin", "read_only", "synced_at")
    list_filter = ("origin", "read_only", "calendar__provider")
    search_fields = ("uid", "event__title")
    list_select_related = ("calendar", "event")
    readonly_fields = [f.name for f in SyncedEvent._meta.fields]

    def has_add_permission(self, request, *args, **kwargs):
        return False


@admin.register(GuestDelivery)
class GuestDeliveryAdmin(RoleAdminMixin, admin.ModelAdmin):
    list_display = ("event", "email", "channel", "sent_at")
    list_filter = ("channel",)
    search_fields = ("email", "event__title")
    list_select_related = ("event",)
    readonly_fields = [f.name for f in GuestDelivery._meta.fields]

    def has_add_permission(self, request, *args, **kwargs):
        return False


class MailServerForm(forms.ModelForm):
    password = forms.CharField(
        label="Пароль", required=False, strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Хранится в зашифрованном виде. Оставьте пустым, чтобы не менять сохранённый пароль.",
    )
    test_to = forms.EmailField(
        label="Куда отправить тестовое письмо", required=False,
        help_text="Для кнопки «Сохранить и отправить тестовое письмо». Пусто — на ваш адрес.",
    )

    class Meta:
        model = MailServer
        fields = "__all__"
        widgets = {
            "host": forms.TextInput(attrs={"placeholder": "email.yanao.ru", "size": 40}),
            "username": forms.TextInput(attrs={"placeholder": "calendar@yanao.ru", "size": 40, "autocomplete": "off"}),
            "from_email": forms.TextInput(attrs={"placeholder": "calendar@yanao.ru", "size": 40}),
        }

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("password"):
            obj.password = self.cleaned_data["password"]
        if commit:
            obj.save()
        return obj


@admin.register(MailServer)
class MailServerAdmin(RoleAdminMixin, admin.ModelAdmin):
    form = MailServerForm
    change_form_template = "admin/integrations/mailserver/change_form.html"
    list_display = ("name", "host", "port", "security", "from_email", "enabled", "status")
    readonly_fields = ("password_state", "last_ok_at", "last_error", "last_error_at")
    fieldsets = (
        ("Сервер", {"fields": ("name", "host", "port", "security", "verify_certificate", "timeout", "enabled")}),
        ("Вход и отправитель", {"fields": ("username", "password", "password_state", "from_email")}),
        ("Проверка", {"fields": ("test_to",)}),
        ("Состояние", {"fields": ("last_ok_at", "last_error", "last_error_at")}),
    )

    @admin.display(description="Пароль")
    def password_state(self, obj):
        return "сохранён" if obj and obj.pk and obj.password else "не задан"

    @admin.display(description="Состояние")
    def status(self, obj):
        if obj.last_error:
            return format_html('<span style="color:#c00">⚠ {}</span>', obj.last_error[:80])
        return "работает" if obj.last_ok_at else "—"

    def _after_save(self, request, obj, default_response):
        if "_send_test" not in request.POST:
            return default_response
        to = (request.POST.get("test_to") or "").strip() or request.user.email
        if not to:
            self.message_user(request, "Укажите, куда отправить тестовое письмо (у вашей учётной записи нет e-mail).", messages.WARNING)
        else:
            try:
                mailer.send_test(obj, to)
            except RuntimeError as exc:
                self.message_user(request, f"Письмо не отправлено: {exc}.", messages.ERROR)
            else:
                self.message_user(request, f"Тестовое письмо отправлено на {to}. Проверьте почту (и папку «Нежелательная почта»).", messages.SUCCESS)
        return HttpResponseRedirect(reverse("admin:integrations_mailserver_change", args=[obj.pk]))

    def response_change(self, request, obj):
        return self._after_save(request, obj, super().response_change(request, obj))

    def response_add(self, request, obj, post_url_continue=None):
        return self._after_save(request, obj, super().response_add(request, obj, post_url_continue))
