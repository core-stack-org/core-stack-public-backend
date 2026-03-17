import requests
import logging
from celery import shared_task

logger = logging.getLogger(__name__)

@shared_task
def beckn_onix_call(callback_url, template):
    try:
        response = requests.post(
            url=callback_url,
            headers={"Content-Type": "application/json"},
            json=template,
            timeout=10
        )

        logger.info(f"Status Code: {response.status_code}")
        logger.info(f"Response: {response.text}")

        response.raise_for_status()

        return response.json()

    except requests.exceptions.SSLError as e:
        logger.error(f"SSL Error: {e}")
    except requests.exceptions.ConnectionError as e:
        logger.error(f"Connection Error: {e}")
    except requests.exceptions.Timeout:
        logger.error("Request timed out")
    except requests.exceptions.RequestException as e:
        logger.error(f"Request failed: {e}")

    return None