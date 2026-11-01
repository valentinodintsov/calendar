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
"""Адреса проекта: админка, вход и выход, адреса приложений."""

from django.conf import settings
from django.contrib import admin
from django.contrib.auth import views as auth_views
from django.urls import include, path

from models_employee.forms import LoginForm

urlpatterns = [
    path("admin/", admin.site.urls),
    # Вход — по учётной записи Active Directory; смены и сброса пароля в календаре нет
    path(
        "accounts/login/",
        auth_views.LoginView.as_view(authentication_form=LoginForm, extra_context={"ad_domain": settings.AD_AUTH_DOMAIN}),
        name="login",
    ),
    path("accounts/logout/", auth_views.LogoutView.as_view(), name="logout"),
    path("", include("models_employee.urls")),
    path("", include("integrations.urls")),
    path("", include("directory.urls")),
    path("", include("events.urls")),
]
