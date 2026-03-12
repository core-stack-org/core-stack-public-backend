from django.urls import path
from .api import get_current_weather, get_forecast_5day, get_forecast_15day, get_historic_forecast

urlpatterns = [
    path("current/", get_current_weather, name="current-weather"),
    path("forecast/5-day/", get_forecast_5day, name="forecast-5day-weather"),
    path("forecast/15-day/", get_forecast_15day, name="forecast-15day-weather"),
    path("historic_forecast/", get_historic_forecast, name="historic-forecast-weather"),
]