from django.urls import path
from .api import dummy_api

urlpatterns = [
    path("on_select", dummy_api, name="dummy-api")
]