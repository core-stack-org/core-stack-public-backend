# beckn_datasets/serializers.py

from rest_framework import serializers
from .models import Dataset, DatasetOrder


class DatasetSerializer(serializers.ModelSerializer):

    class Meta:
        model = Dataset
        fields = "__all__"


class OrderSerializer(serializers.ModelSerializer):

    class Meta:
        model = DatasetOrder
        fields = "__all__"
