# © 2026 Одинцов Валентин Николаевич. Все права защищены.
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
