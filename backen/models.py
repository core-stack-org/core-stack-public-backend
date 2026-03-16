# beckn_datasets/models.py

import uuid
from django.db import models


class Dataset(models.Model):

    dataset_id = models.CharField(max_length=200, unique=True)

    name = models.CharField(max_length=300)

    description = models.TextField(blank=True)

    format = models.CharField(max_length=50, default="parquet")

    download_url = models.URLField()

    is_active = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.name


class DatasetOrder(models.Model):

    STATUS = [
        ("SELECTED", "Selected"),
        ("INIT", "Initialized"),
        ("CONFIRMED", "Confirmed"),
    ]

    order_id = models.UUIDField(default=uuid.uuid4, editable=False)

    dataset = models.ForeignKey(Dataset, on_delete=models.CASCADE)

    status = models.CharField(max_length=20, choices=STATUS, default="SELECTED")

    download_token = models.CharField(max_length=200, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
