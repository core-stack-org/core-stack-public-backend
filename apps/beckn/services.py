# beckn_datasets/services.py

import secrets
from .models import DatasetOrder


def generate_download_token(order):

    token = secrets.token_urlsafe(32)

    order.download_token = token
    order.status = "CONFIRMED"
    order.save()

    return token
