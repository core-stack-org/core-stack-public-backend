from celery import shared_task
import requests
import logging

logger = logging.getLogger(__name__)

@shared_task
def beckn_onix_call(callback_url, template):
    headers = {
    'Content-Type': 'application/json'
    }
    #response = requests.post(url=callback_url, )
    response = requests.request("POST", callback_url, headers=headers, data=template)
    logger.info(f"Onix Call Reponse : ",response)
    return "Done"