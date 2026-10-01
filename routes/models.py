from django.db import models


class FuelStation(models.Model):
    opis_id = models.BigIntegerField(unique=True, db_index=True)
    name = models.CharField(max_length=255)
    address = models.CharField(max_length=255)
    city = models.CharField(max_length=100)
    state = models.CharField(max_length=10, db_index=True)
    rack_id = models.BigIntegerField(null=True, blank=True)
    price = models.DecimalField(max_digits=8, decimal_places=4)
    latitude = models.FloatField(null=True, blank=True)
    longitude = models.FloatField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(fields=['state']),
            models.Index(fields=['latitude', 'longitude']),
        ]
        verbose_name = 'Fuel Station'
        verbose_name_plural = 'Fuel Stations'

    def __str__(self):
        return f"{self.name} ({self.city}, {self.state}) - ${self.price}"
