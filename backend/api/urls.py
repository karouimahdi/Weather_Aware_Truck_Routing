from django.urls import path

from .views import AnalyzeRoutesView, GeocodeReverseView, GeocodeSuggestView

app_name = "api"

urlpatterns = [
    path("routes/analyze/", AnalyzeRoutesView.as_view(), name="analyze-routes"),
    path("geocode/suggest/", GeocodeSuggestView.as_view(), name="geocode-suggest"),
    path("geocode/reverse/", GeocodeReverseView.as_view(), name="geocode-reverse"),
]
