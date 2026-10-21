# © 2026 Одинцов Валентин Николаевич. Все права защищены.
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
