"""Convert the wrapper's IAM secret JSON on stdin into regional SES SMTP credentials."""

import base64
import hashlib
import hmac
import json
import sys


def smtp_credentials(secret, region):
    # SES uses this fixed signing date and message, not the current date.
    key = ('AWS4' + secret['secretAccessKey']).encode()
    for value in ('11111111', region, 'ses', 'aws4_request', 'SendRawEmail'):
        key = hmac.new(key, value.encode(), hashlib.sha256).digest()
    return {'username': secret['username'], 'password': base64.b64encode(b'\x04' + key).decode()}


if __name__ == '__main__':
    json.dump(smtp_credentials(json.load(sys.stdin), sys.argv[1]), sys.stdout)
