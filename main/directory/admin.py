import json

from django import forms
from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.urls import reverse
from django.utils.html import format_html

from models_employee.admin import RoleAdminMixin

from .ldap_client import DEFAULT_MAPPING, MIN_QUERY, DirectoryError, rank_entries
from .models import DirectorySource
from .search import get_client


class DirectorySourceForm(forms.ModelForm):
    password = forms.CharField(
        label="Пароль",
        required=False,
        strip=False,
        widget=forms.PasswordInput(render_value=False, attrs={"autocomplete": "new-password"}),
        help_text="Хранится в зашифрованном виде. Оставьте пустым, чтобы не менять сохранённый пароль.",
    )
    clear_password = forms.BooleanField(label="Удалить сохранённый пароль", required=False)
    probe_query = forms.CharField(
        label="Проверочный запрос", required=False, max_length=100,
        widget=forms.TextInput(attrs={"placeholder": "например, Петров"}),
        help_text="Для кнопки «Сохранить и проверить поиск»: что ввёл бы пользователь в адресной книге.",
    )

    class Meta:
        model = DirectorySource
        fields = "__all__"
        widgets = {
            "name": forms.TextInput(attrs={"placeholder": "YG"}),
            "host": forms.TextInput(attrs={"placeholder": "email.yanao.ru", "size": 50}),
            "base_dn": forms.TextInput(attrs={"placeholder": "cn=yg.loc", "size": 50}),
            "bind_dn": forms.TextInput(attrs={"placeholder": "user@yanao.ru", "size": 50, "autocomplete": "off"}),
            "port": forms.NumberInput(attrs={"placeholder": "636"}),
            "search_filter": forms.Textarea(attrs={"rows": 3, "cols": 80}),
            "attribute_map": forms.Textarea(attrs={"rows": 4, "cols": 80}),
        }

    def clean_attribute_map(self):
        value = self.cleaned_data.get("attribute_map") or {}
        if not isinstance(value, dict):
            raise forms.ValidationError('Ожидается объект JSON, например {"department": "departmentNumber"}.')
        allowed = set(DEFAULT_MAPPING["ad"])
        unknown = sorted(set(value) - allowed)
        if unknown:
            raise forms.ValidationError(f"Неизвестные поля: {', '.join(unknown)}. Допустимые: {', '.join(sorted(allowed))}.")
        return value

    def clean_search_filter(self):
        value = (self.cleaned_data.get("search_filter") or "").strip()
        if value and (not value.startswith("(") or value.count("(") != value.count(")")):
            raise forms.ValidationError("Фильтр LDAP должен быть в скобках, например (mail=*).")
        return value

    def save(self, commit=True):
        obj = super().save(commit=False)
        if self.cleaned_data.get("clear_password"):
            obj.password = ""
        elif self.cleaned_data.get("password"):
            obj.password = self.cleaned_data["password"]
        if commit:
            obj.save()
        return obj


@admin.register(DirectorySource)
class DirectorySourceAdmin(RoleAdminMixin, admin.ModelAdmin):
    form = DirectorySourceForm
    list_display = ("name", "kind", "host", "base_dn", "enabled", "last_ok_at", "status")
    list_filter = ("kind", "enabled")
    readonly_fields = ("password_state", "effective_settings", "last_ok_at", "last_error", "last_error_at")
    actions = ["test_connection"]
    fieldsets = (
        ("Основное", {"fields": ("name", "kind", "host", "base_dn", "port", "bind_dn", "password", "password_state", "clear_password", "enabled")}),
        ("Дополнительно", {
            "classes": ("collapse",),
            "fields": ("security", "verify_certificate", "ca_cert_file", "search_filter", "attribute_map", "effective_settings",
                       "max_results", "page_size"),
        }),
        ("Проверка поиска", {"fields": ("probe_query",)}),
        ("Состояние", {"fields": ("last_ok_at", "last_error", "last_error_at")}),
    )

    @admin.display(description="Состояние")
    def status(self, obj):
        if obj.last_error:
            return format_html('<span style="color:#c00">⚠ {}</span>', obj.last_error[:80])
        return "работает" if obj.last_ok_at else "—"

    @admin.display(description="Пароль")
    def password_state(self, obj):
        return "сохранён" if obj and obj.pk and obj.password else "не задан"

    @admin.display(description="Действующие настройки")
    def effective_settings(self, obj):
        cfg = obj.connection_settings() if obj and obj.pk else None
        if cfg is None:
            return "Будут показаны после сохранения."
        return format_html(
            "<div>Фильтр: <code>{}</code></div><div>Атрибуты:</div><pre style='margin:0'>{}</pre>",
            cfg.effective_filter(),
            json.dumps(cfg.effective_mapping(), ensure_ascii=False, indent=2),
        )

    # ---- проверка и синхронизация
    def _test(self, request, obj):
        try:
            info = get_client(obj).test()
        except DirectoryError as exc:
            self.message_user(request, f"{obj}: {exc}", messages.ERROR)
            for line in exc.details:  # сетевая диагностика: DNS и порты
                self.message_user(request, line, messages.WARNING)
            return
        if not info["found"]:
            self.message_user(request, f"{obj}: подключение успешно, но по фильтру ничего не найдено. Проверьте Base DN и фильтр.", messages.WARNING)
            return
        sample = "; ".join(f"{s['name'] or s['dn']}{' <' + s['email'] + '>' if s['email'] else ''}" for s in info["samples"])
        self.message_user(request, f"{obj}: подключение успешно. Примеры: {sample}", messages.SUCCESS)
        self.message_user(request, f"Атрибуты в записях: {', '.join(info['attributes'])}", messages.INFO)

    def _search(self, request, obj):
        query = (request.POST.get("probe_query") or "").strip()
        if len(query) < MIN_QUERY:
            self.message_user(request, f"Введите проверочный запрос (от {MIN_QUERY} символов).", messages.WARNING)
            return
        try:
            entries = get_client(obj).search(query, limit=obj.max_results or 50)
        except DirectoryError as exc:
            self.message_user(request, f"{obj}: {exc}", messages.ERROR)
            return
        ranked = rank_entries(entries, query, obj.connection_settings().effective_mapping(), limit=10)
        if not ranked:
            self.message_user(request, f"{obj}: по запросу «{query}» на сервере ничего не найдено.", messages.WARNING)
            return
        found = "; ".join(f"{c['display_name']}{' <' + c['email'] + '>' if c['email'] else ''}" for _, c in ranked)
        self.message_user(request, f"{obj}: «{query}» — найдено на сервере: {found}", messages.SUCCESS)

    @admin.action(description="Проверить подключение")
    def test_connection(self, request, queryset):
        for obj in queryset:
            self._test(request, obj)

    def _after_save(self, request, obj, default_response):
        for key, handler in (("_test", self._test), ("_search", self._search)):
            if key in request.POST:
                handler(request, obj)
                return HttpResponseRedirect(reverse("admin:directory_directorysource_change", args=[obj.pk]))
        return default_response

    def response_change(self, request, obj):
        return self._after_save(request, obj, super().response_change(request, obj))

    def response_add(self, request, obj, post_url_continue=None):
        return self._after_save(request, obj, super().response_add(request, obj, post_url_continue))
