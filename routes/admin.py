from django.contrib import admin
from .models import FuelStation


@admin.register(FuelStation)
class FuelStationAdmin(admin.ModelAdmin):
    list_display = ('opis_id', 'name', 'city', 'state', 'price', 'latitude', 'longitude')
    list_filter = ('state',)
    search_fields = ('opis_id', 'name', 'city', 'address')
