# © 2026 Одинцов Валентин Николаевич. Все права защищены.
from django.urls import path

from . import views

app_name = "models_employee"

urlpatterns = [
    path("api/delegations/", views.delegations, name="delegations"),
    path("api/delegations/candidates/", views.delegation_candidates, name="delegation-candidates"),
    path("api/me/settings/", views.my_settings, name="my-settings"),
]
