import os
import json
import threading
from datetime import datetime, timezone

import requests
import boto3

from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.conf import settings

from .models import Dataset, DatasetOrder
from .services import generate_download_token
from .tasks import beckn_onix_call

BPP_URI = "http://api.core-stack.org:8082/"
LOCAL_URL = "https://api.core-stack.org/"
S3_BUCKET = "corestack-weather-data"
TEMPLATES_DIR = os.path.join(
    settings.BASE_DIR,
    "apps",
    "beckn",
    "templates",
)


def load_json_template(filename: str) -> dict:
    filepath = os.path.join(TEMPLATES_DIR, filename)
    with open(filepath, encoding="utf-8") as f:
        return json.load(f)


def inject_dynamic_context(data: dict) -> dict:
    iso_timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    data["context"]["timestamp"] = iso_timestamp

    return data


def fire_callback(url: str, payload: dict):
    try:
        requests.post(url, json=payload, timeout=10)
    except Exception as e:
        pass


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
        context = request.data.get("context", {})
        bap_uri = context.get("bap_uri")
        message_id = context.get("message_id")

        on_select = load_json_template("on_select.json")
        on_select = inject_dynamic_context(on_select)
        on_select["context"]["bap_uri"] = bap_uri
        on_select["context"]["message_id"] = message_id

        print("Reached here Before The Celery")

        beckn_onix_call.apply_async(
            args=[f"{BPP_URI}/bpp/caller/on_select", on_select],
            queue='beckn'
        )

        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        })


class InitAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        context = request.data.get("context", {})
        bap_uri = context.get("bap_uri")
        message_id = context.get("message_id")

        on_init = load_json_template("on_init.json")
        on_init = inject_dynamic_context(on_init)
        on_init["context"]["bap_uri"] = bap_uri
        on_init["context"]["message_id"] = message_id

        beckn_onix_call.apply_async(
            args=[f"{BPP_URI}/bpp/caller/on_init", on_init],
            queue='beckn'
        )

        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        })


class ConfirmAPI(APIView):
    authentication_classes = []
    permission_classes = []

    def post(self, request):
        context = request.data.get("context", {})
        bap_uri = context.get("bap_uri")
        message_id = context.get("message_id")

        on_confirm = load_json_template("on_confirm.json")
        on_confirm = inject_dynamic_context(on_confirm)
        on_confirm["context"]["bap_uri"] = bap_uri
        on_confirm["context"]["message_id"] = message_id

        try:
            #* Call forecast download API
            forecast_response = requests.get(
                f"{LOCAL_URL}/api/v1/weather/download_forecast/",
                params={"lat": 28.62, "lon": 77.43},
                timeout=30
            )
            forecast_data = forecast_response.json()
            access_url = forecast_data.get("url")

            if not access_url:
                return Response(
                    {"error": "Forecast API did not return a valid URL"},
                    status=status.HTTP_500_INTERNAL_SERVER_ERROR
                )

            #* Get file size from S3
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

            #* Update on_confirm with new URL and file size
            on_confirm["message"]["order"]["beckn:fulfillment"]["beckn:deliveryAttributes"]["fulfillment:accessUrl"] = access_url
            on_confirm["message"]["order"]["beckn:fulfillment"]["beckn:deliveryAttributes"]["fulfillment:fileSizeBytes"] = file_size

        except Exception as e:
            return Response(
                {"error": f"Failed to process confirm: {str(e)}"},
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )

        beckn_onix_call.apply_async(
            args=[f"{BPP_URI}/bpp/caller/on_confirm", on_confirm],
            queue='beckn'
        )
        return Response({
            "context": context,
            "message": {
                "ack": {
                    "status": "ACK"
                }
            }
        })