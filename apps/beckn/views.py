import os
import json
import uuid
from datetime import datetime, timezone

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings

import boto3

from .models import Dataset, DatasetOrder
from .services import generate_download_token

S3_BUCKET = "corestack-weather-data"
TEMPLATES_DIR = os.path.join(
    settings.BASE_DIR,
    "apps",
    "beckn",
    "templates",
)


def load_json_template(filename: str) -> dict:
    filepath = os.path.join(TEMPLATES_DIR, filename)
    with open(filepath) as f:
        return json.load(f)


def inject_dynamic_context(data: dict) -> dict:
    iso_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    guid = str(uuid.uuid4())

    data["context"]["timestamp"] = iso_timestamp
    data["context"]["message_id"] = guid

    return data


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
        on_select = load_json_template("on_select.json")
        on_select = inject_dynamic_context(on_select)
        return Response(on_select)


class InitAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        on_init = load_json_template("on_init.json")
        on_init = inject_dynamic_context(on_init)
        return Response(on_init)


class ConfirmAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):

        on_confirm = load_json_template("on_confirm.json")
        on_confirm = inject_dynamic_context(on_confirm)

        try:
            access_url = on_confirm["message"]["order"]["beckn:fulfillment"]["beckn:deliveryAttributes"]["fulfillment:accessUrl"]

            path = access_url.split(".amazonaws.com/")[1]
            s3_key = path.split("?")[0]

            s3_client = boto3.client(
                "s3",
                aws_access_key_id=settings.AWS_ACCESS_KEY_ID,
                aws_secret_access_key=settings.AWS_SECRET_ACCESS_KEY,
                region_name=settings.AWS_REGION_NAME,
                config=boto3.session.Config(signature_version="s3v4"),
            )

            head = s3_client.head_object(Bucket=S3_BUCKET, Key=s3_key)
            file_size = head["ContentLength"]

            on_confirm["message"]["order"]["beckn:fulfillment"]["beckn:deliveryAttributes"]["fulfillment:fileSizeBytes"] = file_size

        except Exception as e:
            return Response(
                {"error": f"Failed to fetch file size from S3: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        return Response(on_confirm)