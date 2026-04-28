import json
import os
import logging
import hashlib
import hmac
import urllib.request
import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

ssm = boto3.client('ssm')

WEBHOOK_URL = None
WEBHOOK_SECRET = None

def get_parameters():
    global WEBHOOK_URL, WEBHOOK_SECRET

    if WEBHOOK_URL and WEBHOOK_SECRET:
        return WEBHOOK_URL, WEBHOOK_SECRET

    url_param = os.environ['WEBHOOK_URL_PARAM']
    secret_param = os.environ['WEBHOOK_SECRET_PARAM']

    WEBHOOK_URL = ssm.get_parameter(Name=url_param, WithDecryption=True)['Parameter']['Value']
    WEBHOOK_SECRET = ssm.get_parameter(Name=secret_param, WithDecryption=True)['Parameter']['Value']

    return WEBHOOK_URL, WEBHOOK_SECRET


def handler(event, context):
    try:
        message = json.loads(event['Records'][0]['Sns']['Message'])

        webhook_url, secret = get_parameters()

        payload = {
            "source": "cloudwatch-alarm",
            "title": f"{message.get('AlarmName')} triggered",
            "description": message.get('NewStateReason'),
            "severity": "HIGH",
            "timestamp": message.get('StateChangeTime'),
            "metadata": {
                "alarmName": message.get('AlarmName'),
                "newState": message.get('NewStateValue'),
                "reason": message.get('NewStateReason'),
                "region": message.get('Region'),
                "accountId": message.get('AWSAccountId')
            }
        }

        body = json.dumps(payload).encode('utf-8')

        signature = hmac.new(
            secret.encode('utf-8'),
            body,
            hashlib.sha256
        ).hexdigest()

        req = urllib.request.Request(
            webhook_url,
            data=body,
            headers={
                'Content-Type': 'application/json',
                'X-Signature': f'sha256={signature}'
            }
        )

        with urllib.request.urlopen(req) as response:
            logger.info(f"Webhook sent: {response.status}")

    except Exception as e:
        logger.error(f"Error: {str(e)}")

    return {"status": "done"}