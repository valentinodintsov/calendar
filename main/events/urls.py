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
from django.urls import path

from . import views

app_name = "events"

urlpatterns = [
    path("", views.calendar_view, name="calendar"),
    path("api/events/", views.events_collection, name="events-collection"),
    path("api/events/report/", views.events_report, name="events-report"),
    path("api/events/<int:pk>/", views.event_detail, name="event-detail"),
    path("api/reminders/", views.reminders, name="reminders"),
    path("api/events/<int:pk>/attachments/", views.attachment_upload, name="attachment-upload"),
    path("api/attachments/<int:pk>/", views.attachment_delete, name="attachment-delete"),
    path("api/attachments/<int:pk>/download/", views.attachment_download, name="attachment-download"),
]
