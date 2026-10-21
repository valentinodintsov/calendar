# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.contrib import admin
from django.contrib.auth import get_user_model
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.forms.models import BaseInlineFormSet

from .models import Delegation, Employee
from .permissions import display_name, is_admin


class RoleAdminMixin:
    """Доступ к разделу админки — у суперпользователя и у роли «Администратор»."""

    def _allowed(self, request):
        return request.user.is_active and is_admin(request.user)

    def has_module_permission(self, request):
        return self._allowed(request)

    def has_view_permission(self, request, obj=None):
        return self._allowed(request)

    def has_add_permission(self, request, *args, **kwargs):
        return self._allowed(request)

    def has_change_permission(self, request, obj=None):
        return self._allowed(request)

    def has_delete_permission(self, request, obj=None):
        return self._allowed(request)


@admin.register(Employee)
class EmployeeAdmin(RoleAdminMixin, admin.ModelAdmin):
    list_display = ("user", "full_name", "is_owner", "managed_calendars", "is_observer", "is_admin", "position")
    list_editable = ("is_owner", "is_observer", "is_admin")
    list_filter = ("is_owner", "is_observer", "is_admin")
    search_fields = ("user__username", "user__first_name", "user__last_name", "user__email", "position")
    list_select_related = ("user",)
    fields = ("user", "position", "is_owner", "is_observer", "is_admin", "managed_calendars", "ad_guid")
    readonly_fields = ("managed_calendars", "ad_guid")

    @admin.display(description="Имя")
    def full_name(self, obj):
        return obj.user.get_full_name()

    @admin.display(description="Менеджер календарей")
    def managed_calendars(self, obj):
        if not obj.pk:
            return "—"
        owners = sorted(
            {display_name(d.owner) for d in obj.user.received_delegations.select_related("owner")}
        )
        return ", ".join(owners) or "—"


@admin.register(Delegation)
class DelegationAdmin(RoleAdminMixin, admin.ModelAdmin):
    list_display = ("owner", "manager", "scope", "can_create", "can_read", "can_change", "updated_at")
    list_filter = ("can_create", "can_read", "can_change")
    search_fields = ("owner__username", "manager__username", "event__title")
    list_select_related = ("owner", "manager", "event")
    raw_id_fields = ("event",)

    @admin.display(description="Область")
    def scope(self, obj):
        return obj.event.title if obj.event_id else "Все события владельца"


class EmployeeInlineFormSet(BaseInlineFormSet):
    """
    Профиль сотрудника создаётся сигналом сразу при сохранении пользователя,
    раньше, чем админка сохраняет встроенную форму роли. Поэтому вместо создания
    второго профиля (IntegrityError: UNIQUE user_id) обновляем уже созданный.
    """

    def save_new(self, form, commit=True):
        existing = Employee.objects.filter(user=self.instance).first()
        if existing is not None:
            form.instance.pk = existing.pk
            form.instance._state.adding = False
            form.instance._initial_admin = existing.is_admin
        return super().save_new(form, commit=commit)


class EmployeeInline(admin.StackedInline):
    model = Employee
    formset = EmployeeInlineFormSet
    can_delete = False
    max_num = 1
    verbose_name_plural = "Роли в календаре"
    fields = ("position", "is_owner", "is_observer", "is_admin")


User = get_user_model()
admin.site.unregister(User)


@admin.register(User)
class UserAdmin(BaseUserAdmin):
    inlines = [EmployeeInline]
