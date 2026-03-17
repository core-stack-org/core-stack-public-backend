from django.urls import path

from .api import DiscoverAPI, SelectAPI, InitAPI, ConfirmAPI

urlpatterns = [
    path("discover", DiscoverAPI.as_view()),
    path("select", SelectAPI.as_view()),
    path("init", InitAPI.as_view()),
    path("confirm", ConfirmAPI.as_view()),
]
