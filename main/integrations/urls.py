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

app_name = "integrations"

urlpatterns = [
    path("api/integrations/", views.calendars, name="calendars"),
    path("api/integrations/discover/", views.discover, name="discover"),
    path("api/integrations/sync/", views.sync_all, name="sync-all"),
    path("api/integrations/<int:pk>/", views.calendar_detail, name="calendar-detail"),
    path("api/integrations/<int:pk>/sync/", views.sync_one, name="sync-one"),
    path("api/events/<int:pk>/invite/", views.invite, name="invite"),
    path("integrations/google/connect/", views.google_connect, name="google-connect"),
    path("integrations/google/callback/", views.google_callback, name="google-callback"),
]
