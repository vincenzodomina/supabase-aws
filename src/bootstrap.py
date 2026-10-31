"""Verify network prerequisites, provision RDS, and manage optional log retention."""
import base64
import hashlib
import hmac
import json
from pathlib import Path
import secrets
import signal
import time
import urllib.request
import uuid
from urllib.parse import quote

import boto3
from botocore.config import Config
import psycopg
from psycopg import sql
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

CA_BUNDLE = Path(__file__).with_name('rds-ca.pem')


def b64(value):
    return base64.urlsafe_b64encode(value).rstrip(b'=').decode()


def token(header, claims, signer):
    message = '.'.join(b64(json.dumps(v, separators=(',', ':')).encode()) for v in (header, claims))
    return message + '.' + b64(signer(message.encode()))


def generate_keys(legacy_secret):
    private = ec.generate_private_key(ec.SECP256R1())
    numbers = private.private_numbers()
    kid = str(uuid.uuid4())
    public = {'kty': 'EC', 'crv': 'P-256', 'alg': 'ES256', 'use': 'sig', 'kid': kid,
              'key_ops': ['verify'], 'ext': True,
              'x': b64(numbers.public_numbers.x.to_bytes(32, 'big')),
              'y': b64(numbers.public_numbers.y.to_bytes(32, 'big'))}
    private_jwk = dict(public, d=b64(numbers.private_value.to_bytes(32, 'big')), key_ops=['sign', 'verify'])
    symmetric = {'kty': 'oct', 'alg': 'HS256', 'k': b64(legacy_secret.encode())}

    def es256(message):
        r, s = decode_dss_signature(private.sign(message, ec.ECDSA(hashes.SHA256())))
        return r.to_bytes(32, 'big') + s.to_bytes(32, 'big')

    def opaque_key(prefix):
        key = prefix + b64(secrets.token_bytes(17))[:22]
        checksum = b64(hashlib.sha256(('supabase-self-hosted|' + key).encode()).digest())[:8]
        return key + '_' + checksum

    now = int(time.time())
    result = {'JWT_SECRET': legacy_secret,
              'JWT_KEYS': json.dumps([private_jwk, symmetric]),
              'JWT_JWKS': json.dumps({'keys': [public, symmetric]}),
              'SUPABASE_PUBLISHABLE_KEY': opaque_key('sb_publishable_'),
              'SUPABASE_SECRET_KEY': opaque_key('sb_secret_')}
    for role, name in [('anon', 'ANON_KEY'), ('service_role', 'SERVICE_ROLE_KEY')]:
        claims = {'role': role, 'iss': 'supabase', 'iat': now, 'exp': now + 5 * 365 * 86400}
        result[name] = token({'alg': 'HS256', 'typ': 'JWT'}, claims,
                             lambda data: hmac.digest(legacy_secret.encode(), data, 'sha256'))
        result[name + '_ASYMMETRIC'] = token({'alg': 'ES256', 'typ': 'JWT', 'kid': kid}, claims, es256)
    return result


def initialize(connection, passwords):
    migrations = [(path.stem, path.read_bytes()) for path in sorted(Path(__file__).with_name('migrations').glob('*.sql'))]
    if not migrations:
        raise RuntimeError('The bootstrap package contains no migrations')
    versions = [(version, hashlib.sha256(content).hexdigest()) for version, content in migrations]
    # Serialize repeated CloudFormation deliveries without rotating existing credentials.
    with connection.transaction():
        connection.execute("SELECT pg_advisory_xact_lock(781452369)")
        connection.execute('''CREATE SCHEMA IF NOT EXISTS supabase_aws;
            REVOKE ALL ON SCHEMA supabase_aws FROM PUBLIC;
            CREATE TABLE IF NOT EXISTS supabase_aws.migrations (
                version text PRIMARY KEY, checksum text NOT NULL,
                applied_at timestamptz NOT NULL DEFAULT now());
            REVOKE ALL ON supabase_aws.migrations FROM PUBLIC;''')
        applied = connection.execute('SELECT version, checksum FROM supabase_aws.migrations ORDER BY version').fetchall()
        if applied != versions[:len(applied)]:
            raise RuntimeError('Applied migrations differ from the bootstrap package')
        for (version, content), (_, checksum) in zip(migrations[len(applied):], versions[len(applied):]):
            print(f'Applying database migration {version}')
            connection.execute(content.decode('utf-8'))
            connection.execute('INSERT INTO supabase_aws.migrations (version, checksum) VALUES (%s, %s)', (version, checksum))
        for name, password in passwords.items():
            connection.execute(sql.SQL('ALTER ROLE {} PASSWORD {}').format(sql.Identifier(name), sql.Literal(password)))


def bootstrap(properties):
    client = boto3.client('secretsmanager', config=Config(
        connect_timeout=5, read_timeout=10, retries={'mode': 'standard', 'total_max_attempts': 3}))

    def read(arn):
        return json.loads(client.get_secret_value(SecretId=arn)['SecretString'])

    master = read(properties['DatabaseSecret'])
    credentials = {name: (properties[key], read(properties[key])) for name, key in [
        ('supabase_auth_admin', 'AuthSecret'), ('authenticator', 'RestSecret'),
        ('supabase_storage_admin', 'StorageSecret')]}
    host, port = properties['DatabaseHost'], properties['DatabasePort']
    with psycopg.connect(host=host, port=port, dbname='postgres', user=master['username'],
                         password=master['password'], sslmode='verify-full',
                         sslrootcert=str(CA_BUNDLE), connect_timeout=15,
                         options='-c statement_timeout=30000 -c lock_timeout=10000') as connection:
        initialize(connection, {name: entry[1]['password'] for name, entry in credentials.items()})
    for name, (arn, value) in credentials.items():
        value['uri'] = (f"postgresql://{name}:{quote(value['password'], safe='')}@{host}:{port}/postgres"
                        '?sslmode=verify-full&sslrootcert=/certs/rds-ca.pem')
        client.put_secret_value(SecretId=arn, SecretString=json.dumps(value))
    arn = properties['ApiSecret']
    value = read(arn)
    if 'JWT_KEYS' not in value:
        value = generate_keys(value['JWT_SECRET'])
        client.put_secret_value(SecretId=arn, SecretString=json.dumps(value))
    print('Database prerequisites and service credentials are ready.')


def verify_network(properties):
    if properties['Mode'] == 'service-connect-tls':
        return
    if properties['Mode'] != 'vpc':
        raise ValueError('Unsupported internal encryption mode')
    client = boto3.client('ec2', config=Config(
        connect_timeout=5, read_timeout=10, retries={'mode': 'standard', 'total_max_attempts': 3}))
    response = client.describe_vpc_encryption_controls(VpcIds=[properties['VpcId']])
    controls = response['VpcEncryptionControls']
    if len(controls) != 1 or controls[0].get('VpcId') != properties['VpcId'] or \
       controls[0].get('Mode') != 'enforce' or controls[0].get('State') != 'available':
        raise RuntimeError('The existing VPC must have available encryption enforcement')


def retain_service_logs(properties):
    client = boto3.client('logs', config=Config(
        connect_timeout=5, read_timeout=10, retries={'mode': 'standard', 'total_max_attempts': 3}))
    days = int(properties['RetentionInDays'])
    for name in properties['LogGroups']:
        try:
            client.put_retention_policy(logGroupName=name, retentionInDays=days)
        except client.exceptions.ResourceNotFoundException:
            if name not in properties['CreateLogGroups']:
                continue
            try:
                client.create_log_group(logGroupName=name)
            except client.exceptions.ResourceAlreadyExistsException:
                pass  # The AWS service can create the group between these calls.
            client.put_retention_policy(logGroupName=name, retentionInDays=days)


def handler(event, context):
    retention = event['ResourceType'] == 'Custom::ServiceLogRetention'
    network = event['ResourceType'] == 'Custom::NetworkPrerequisites'
    if network:
        operation, suffix = 'Network prerequisites', '/network-prerequisites'
    elif retention:
        operation, suffix = 'Service log retention', '/service-log-retention'
    else:
        operation, suffix = 'Database bootstrap', '/database-bootstrap'
    physical_id = event.get('PhysicalResourceId', event['StackId'] + suffix)
    status, reason = 'SUCCESS', operation + ' completed'

    def timeout(signum, frame):
        raise TimeoutError('Custom resource deadline reached')

    previous = signal.signal(signal.SIGALRM, timeout)
    try:
        if event['RequestType'] != 'Delete':
            if event['ResourceType'] not in ('Custom::DatabaseBootstrap', 'Custom::ServiceLogRetention', 'Custom::NetworkPrerequisites'):
                raise ValueError('Unsupported custom resource type')
            seconds = context.get_remaining_time_in_millis() / 1000 - 30
            if seconds <= 0:
                raise TimeoutError('Insufficient time to handle the custom resource')
            # Leave time to report failure before Lambda terminates the invocation.
            signal.setitimer(signal.ITIMER_REAL, seconds)
            if network:
                verify_network(event['ResourceProperties'])
            elif retention:
                retain_service_logs(event['ResourceProperties'])
            else:
                bootstrap(event['ResourceProperties'])
    except Exception as error:
        print(f'{operation} failed: {type(error).__name__}; request {event["RequestId"]}')
        status, reason = 'FAILED', operation + ' failed; inspect the Lambda log stream.'
        if network:
            reason = 'Network prerequisites failed; check the VPC enforcement state and EC2 API access.'
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)
    payload = json.dumps({'Status': status, 'Reason': reason, 'PhysicalResourceId': physical_id,
                          'StackId': event['StackId'], 'RequestId': event['RequestId'],
                          'LogicalResourceId': event['LogicalResourceId'], 'Data': {}}).encode()
    request = urllib.request.Request(event['ResponseURL'], data=payload, method='PUT',
                                     headers={'Content-Type': '', 'Content-Length': str(len(payload))})
    with urllib.request.urlopen(request, timeout=20):
        pass
