from django.urls import path

from . import views

app_name = "directory"

urlpatterns = [
    path("api/contacts/", views.contacts, name="contacts"),
    path("api/directory/sources/", views.sources, name="sources"),
    path("api/directory/sources/<int:pk>/test/", views.source_test, name="source-test"),
]
