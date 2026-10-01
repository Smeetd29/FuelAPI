from django.urls import re_path
from .views import RouteOptimizeView, RouteMapView

urlpatterns = [
    re_path(r'^route/?(?:\r|\n|\s)*$', RouteOptimizeView.as_view(), name='route-optimize'),
    re_path(r'^route/map/?(?:\r|\n|\s)*$', RouteMapView.as_view(), name='route-map'),
]

