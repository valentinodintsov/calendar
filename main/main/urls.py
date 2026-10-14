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
