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
from django.contrib import admin

from models_employee.admin import RoleAdminMixin

from .models import Attachment, Event


class AttachmentInline(RoleAdminMixin, admin.TabularInline):
    model = Attachment
    extra = 0
    fields = ("file", "name", "size", "uploaded_at")
    readonly_fields = ("size", "uploaded_at")


@admin.register(Event)
class EventAdmin(RoleAdminMixin, admin.ModelAdmin):
    list_display = ("title", "owner", "start", "end", "all_day", "color", "location")
    list_filter = ("all_day", "color", "owner")
    list_select_related = ("owner",)
    search_fields = ("search_text",)
    search_help_text = "Поиск по названию, месту и описанию"
    date_hierarchy = "start"
    filter_horizontal = ("invitees",)
    inlines = [AttachmentInline]

    def get_search_results(self, request, queryset, search_term):
        # search_text хранится в нижнем регистре — приводим и запрос
        return super().get_search_results(request, queryset, Event.normalize(search_term))


@admin.register(Attachment)
class AttachmentAdmin(RoleAdminMixin, admin.ModelAdmin):
    list_display = ("name", "event", "size", "uploaded_at")
    search_fields = ("name", "event__title")
