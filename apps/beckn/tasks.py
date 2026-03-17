from celery import shared_task
import requests
import logging

logger = logging.getLogger(__name__)

@shared_task
def beckn_onix_call(callback_url, template):
    response = requests.post(url=callback_url, params={"body": template})
    logger.info(f"Onix Call Reponse : ",response)
    return "Done"