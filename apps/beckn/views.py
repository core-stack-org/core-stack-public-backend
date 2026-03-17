import os
import json
from rest_framework.views import APIView
from rest_framework.response import Response
from django.shortcuts import get_object_or_404
from django.conf import settings


from .models import Dataset, DatasetOrder
from .services import generate_download_token

TEMPLATES_DIR = os.path.join(
    settings.BASE_DIR,
    "apps",
    "beckn",
    "templates",
)

class DiscoverAPI(APIView):

    def post(self, request):

        datasets = Dataset.objects.filter(is_active=True)

        items = []

        for d in datasets:
            items.append(
                {"id": d.dataset_id, "name": d.name, "description": d.description}
            )

        return Response({"message": {"catalog": {"items": items}}})


class SelectAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):


        return Response({"message": {"ack": {"status": "ACK"}}})


class InitAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        return Response({"message": {"ack": {"status": "ACK"}}})


class ConfirmAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        return Response({"message": {"ack": {"status": "ACK"}}})
