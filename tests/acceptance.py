#!/usr/bin/env python3
"""Exercise private Storage and API deletion; S3 history follows the bucket retention policy."""
import argparse
import json
import secrets
import urllib.error
import urllib.request

import boto3


def check_storage(api_url, keys, after_upload=None):
    base = api_url.rstrip('/') + '/storage/v1'
    bucket = 'acceptance-' + secrets.token_hex(8)
    payload = b'Supabase AWS acceptance test\n'

    def request(path, method='GET', data=None, authorized=True):
        headers = {'apikey': keys['SUPABASE_SECRET_KEY'] if authorized else keys['SUPABASE_PUBLISHABLE_KEY']}
        if authorized:
            headers['Authorization'] = 'Bearer ' + keys['SERVICE_ROLE_KEY']
        if isinstance(data, dict):
            data = json.dumps(data).encode()
            headers['Content-Type'] = 'application/json'
        elif data is not None:
            headers['Content-Type'] = 'text/plain'
        try:
            with urllib.request.urlopen(urllib.request.Request(base+path, data=data, method=method, headers=headers), timeout=30) as response:
                return response.status, response.read()
        except urllib.error.HTTPError as response:
            return response.code, response.read()

    created = False
    try:
        status, _ = request('/bucket', 'POST', {'id': bucket, 'name': bucket, 'public': False})
        if status != 200:
            raise RuntimeError(f'Bucket creation failed: HTTP {status}')
        created = True
        status, _ = request('/object/'+bucket+'/probe.txt', 'POST', payload)
        if status not in (200, 201):
            raise RuntimeError(f'S3-backed upload failed: HTTP {status}')
        if after_upload:
            after_upload(bucket, payload)
        status, body = request('/object/'+bucket+'/probe.txt')
        if status != 200 or body != payload:
            raise RuntimeError('S3-backed download did not match upload')
        status, _ = request('/object/'+bucket+'/probe.txt', authorized=False)
        if status not in (400, 401, 403, 404):
            raise RuntimeError(f'Expected private-object denial, received HTTP {status}')
        status, body = request('/object/sign/'+bucket+'/probe.txt', 'POST', {'expiresIn': 60})
        if status != 200:
            raise RuntimeError(f'Signed URL creation failed: HTTP {status}')
        signed = json.loads(body)['signedURL']
        url = signed if signed.startswith(('https://', 'http://')) else base+signed
        with urllib.request.urlopen(url, timeout=30) as response:
            if response.read() != payload:
                raise RuntimeError('Signed download did not match upload')
    finally:
        if created:
            status, _ = request('/object/'+bucket, 'DELETE', {'prefixes': ['probe.txt']})
            if status not in (200, 204):
                raise RuntimeError(f'Object cleanup failed in {bucket}: HTTP {status}')
            status, _ = request('/object/'+bucket+'/probe.txt')
            if status not in (400, 404):
                raise RuntimeError(f'Deleted object remained accessible: HTTP {status}')
            status, _ = request('/bucket/'+bucket, 'DELETE')
            if status not in (200, 204):
                raise RuntimeError(f'Bucket cleanup failed for {bucket}: HTTP {status}')
    print('PASS: S3-backed upload/download, private access, signed URL and deletion.', flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stack-name', required=True)
    parser.add_argument('--region', required=True)
    parser.add_argument('--profile')
    args = parser.parse_args()
    session = boto3.Session(profile_name=args.profile, region_name=args.region)
    outputs = {v['OutputKey']: v['OutputValue'] for v in session.client('cloudformation').describe_stacks(StackName=args.stack_name)['Stacks'][0]['Outputs']}
    keys = json.loads(session.client('secretsmanager').get_secret_value(SecretId=outputs['ApiSecretArn'])['SecretString'])
    check_storage(outputs['ApiUrl'], keys)


if __name__ == '__main__':
    main()
