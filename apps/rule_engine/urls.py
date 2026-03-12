from django.urls import path
from .api import crop_rule_engine

urlpatterns = [
    path("", crop_rule_engine, name="crop_rule_engine")
]