#!/usr/bin/env python3
"""Disposable Docker integration checks with Moto; no AWS account or provisioning."""
import base64
import datetime
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import zipfile

import boto3
from botocore.config import Config
import psycopg
from cfnlint.decode import decode_str
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from acceptance import check_storage

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT/'src'))
from bootstrap import generate_keys

template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
assert not errors, errors
DATABASE_IMAGE = 'postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5'
LAMBDA_IMAGE = 'public.ecr.aws/lambda/python:3.13@sha256:4878ebcc1df9c6b55770d66b2f06b7530b068dac32d19befc9fa7e118abc901d'
MOTO_IMAGE = 'motoserver/moto:5.2.5@sha256:59a1965d845249b04def37a0e2565f9cc6c44543cb89b68e90f28f344b3b0d1a'
prefix = 'supabase-check-' + secrets.token_hex(4)
containers = []
ecs_architecture = os.environ.get('ECS_CPU_ARCHITECTURE', template['Parameters']['EcsCpuArchitecture']['Default'])
assert ecs_architecture in template['Parameters']['EcsCpuArchitecture']['AllowedValues'], 'Unsupported ECS_CPU_ARCHITECTURE'
ecs_platform = {'X86_64': 'linux/amd64', 'ARM64': 'linux/arm64'}[ecs_architecture]


def docker(*args, stderr=subprocess.PIPE, **kwargs):
    return subprocess.check_output(['docker', *args], text=True, stderr=stderr, timeout=180, **kwargs).strip()


def start(name, image, environment, port=None, alias=None, mounts=(), entrypoint=None, command=(), network=None,
          platform='linux/amd64', extra_ports=(), hosts=()):
    name = prefix+'-'+name
    args = ['run', '-d', '--platform', platform, '--name', name, '--network', network or prefix]
    for exposed in (*extra_ports, *([port] if port else [])):
        args += ['-p', f'127.0.0.1::{exposed}']
    if alias:
        args += ['--network-alias', alias]
    for host in hosts:
        args += ['--add-host', host]
    for mount in mounts:
        args += ['-v', mount]
    if entrypoint:
        args += ['--entrypoint', entrypoint]
    for key in environment:
        args += ['--env', key]
    containers.append(name)
    try:
        docker(*args, image, *command, env=dict(os.environ, **environment))
    except subprocess.CalledProcessError as error:
        error.add_note(error.stderr or 'Docker returned no startup diagnostics.')
        raise
    if port:
        return int(docker('port', name, str(port)).rsplit(':', 1)[1])


def request(url, method='GET', data=None, headers=None, timeout=5):
    headers = dict(headers or {})
    if data is not None:
        data = json.dumps(data).encode()
        headers['Content-Type'] = 'application/json'
    try:
        response = urllib.request.urlopen(urllib.request.Request(url, data=data, method=method, headers=headers), timeout=timeout)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, response.read()


def wait_http(url):
    for _ in range(90):
        try:
            if request(url)[0] == 200:
                return
        except (OSError, TimeoutError):
            pass
        time.sleep(2)
    raise RuntimeError('Service did not become ready: '+url)


def run(directory, package):
    with zipfile.ZipFile(package) as archive:
        assert sum(entry.file_size for entry in archive.infolist()) < 250*1024*1024
        migration_names = sorted(path.relative_to(ROOT/'src').as_posix() for path in (ROOT/'src/migrations').glob('*.sql'))
        assert migration_names and sorted(name for name in archive.namelist() if name.startswith('migrations/')) == migration_names
        for name in ['bootstrap.py', 'rds-ca.pem', *migration_names]:
            assert archive.read(name) == (ROOT/'src'/name).read_bytes(), 'Stale Lambda package: '+name
        archive.extractall(directory/'lambda')
    runtime = template['Resources']['BootstrapHandler']['Properties']
    assert runtime['Runtime'] == 'python3.13' and runtime['Architectures'] == ['x86_64']
    aws_env = {'AWS_ACCESS_KEY_ID': 'local-test', 'AWS_SECRET_ACCESS_KEY': 'local-test',
               'AWS_DEFAULT_REGION': 'us-east-1', 'AWS_REGION': 'us-east-1', 'AWS_EC2_METADATA_DISABLED': 'true'}
    motoport = start('moto', MOTO_IMAGE, {'S3_IGNORE_SUBDOMAIN_BUCKETNAME': 'true'}, 5000, 'moto')
    wait_http(f'http://127.0.0.1:{motoport}/moto-api/')
    session = boto3.Session(aws_access_key_id='local-test', aws_secret_access_key='local-test', region_name='us-east-1')
    config = Config(connect_timeout=5, read_timeout=10, retries={'total_max_attempts': 1}, s3={'addressing_style': 'path'})
    s3 = session.client('s3', endpoint_url=f'http://127.0.0.1:{motoport}', config=config)
    sm = session.client('secretsmanager', endpoint_url=f'http://127.0.0.1:{motoport}', config=config)
    logs = session.client('logs', endpoint_url=f'http://127.0.0.1:{motoport}', config=config)
    internal_s3 = session.client('s3', endpoint_url='http://moto:5000', config=config)
    for bucket in ['artifacts', 'integration']:
        s3.create_bucket(Bucket=bucket)
    s3.put_bucket_versioning(Bucket='integration', VersioningConfiguration=template['Resources']['Bucket']['Properties']['VersioningConfiguration'])

    password = secrets.token_hex(24)
    fixture_secrets = {'ApiSecret': {'JWT_SECRET': secrets.token_hex(32)},
                       'DatabaseSecret': {'username': 'postgres', 'password': password},
                       'MetaSecret': {'crypto_key': secrets.token_hex(24)},
                       'AuthSecret': {'password': password}, 'RestSecret': {'password': password},
                       'StorageSecret': {'password': password}, 'SesSmtpSecretArn': {'username': '', 'password': ''}}
    for name in ['AuthSecret', 'RestSecret', 'StorageSecret']:
        fixture_secrets[name].update(json.loads(template['Resources'][name]['Properties']['GenerateSecretString']['SecretStringTemplate']))
    properties = {name: sm.create_secret(Name=name, SecretString=json.dumps(value))['ARN']
                  for name, value in fixture_secrets.items()}
    properties.update(DatabaseHost='db', DatabasePort=5432)
    values = {k: v['Default'] for k, v in template['Parameters'].items() if 'Default' in v}
    values.update({'AWS::StackName': 'test', 'AWS::Region': 'us-east-1', 'AWS::URLSuffix': 'amazonaws.com',
                   'Bucket': 'integration', 'ApiDomainName': 'api.example.invalid',
                   'SiteUrl': 'http://localhost:3000', 'SesSenderEmail': 'integration@example.com'})

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, 'db')])
    now = datetime.datetime.now(datetime.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now-datetime.timedelta(minutes=1))
            .not_valid_after(now+datetime.timedelta(days=1))
            .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('db'), x509.DNSName('hook'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    (directory/'rds-ca.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory/'server.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    certmount = str(directory)+':/certs:ro'
    db_parameters = template['Resources']['DbParameters']['Properties']['Parameters']
    tracking_parameter = db_parameters['pg_stat_statements.track']['Ref']
    pgport = start('db', DATABASE_IMAGE, {'POSTGRES_PASSWORD': password}, 5432, 'db', mounts=[certmount],
                   entrypoint='/bin/bash', command=['-ec',
                   'cp /certs/server.key /certs/rds-ca.pem /tmp/; chown postgres:postgres /tmp/server.key /tmp/rds-ca.pem; chmod 0600 /tmp/server.key /tmp/rds-ca.pem; exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/rds-ca.pem -c ssl_key_file=/tmp/server.key "$@"',
                   '--', '-c', 'shared_preload_libraries=pg_stat_statements',
                   '-c', 'pg_stat_statements.track='+values[tracking_parameter],
                   '-c', 'pg_stat_statements.track_utility='+db_parameters['pg_stat_statements.track_utility']])
    database = dict(host='127.0.0.1', port=pgport, user='postgres', password=password, dbname='postgres',
                    sslmode='verify-full', sslrootcert=str(directory/'rds-ca.pem'), connect_timeout=3)
    for _ in range(60):
        try:
            psycopg.connect(**database).close()
            break
        except psycopg.OperationalError:
            time.sleep(1)
    else:
        raise RuntimeError('Postgres startup timed out')

    lambda_env = dict(aws_env, AWS_ENDPOINT_URL_SECRETS_MANAGER='http://moto:5000', AWS_ENDPOINT_URL_CLOUDWATCH_LOGS='http://moto:5000')
    mounts = [str(directory/'lambda')+':/var/task:ro']
    badport = start('lambda-untrusted', LAMBDA_IMAGE, lambda_env, 8080, mounts=mounts, command=[runtime['Handler']])
    lambdaport = start('lambda', LAMBDA_IMAGE, lambda_env, 8080,
                       mounts=mounts+[str(directory/'rds-ca.pem')+':/var/task/rds-ca.pem:ro'], command=[runtime['Handler']])

    def invoke(port, request_type, expected='SUCCESS', physical_id=None,
               resource_type='Custom::DatabaseBootstrap', resource_properties=None):
        for _ in range(60):
            try:
                request(f'http://127.0.0.1:{port}/', timeout=1)
                break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError('Lambda runtime startup timed out')
        request_id = secrets.token_hex(8)
        event = {'RequestType': request_type, 'ResourceType': resource_type, 'StackId': 'stack', 'RequestId': request_id,
                 'LogicalResourceId': {'Custom::ServiceLogRetention': 'ServiceLogRetention',
                                       'Custom::NetworkPrerequisites': 'EncryptionCheck',
                                       'Custom::DatabaseBootstrap': 'Bootstrap'}[resource_type],
                 'ResourceProperties': properties if resource_properties is None else resource_properties,
                 'ResponseURL': internal_s3.generate_presigned_url('put_object', Params={'Bucket': 'artifacts', 'Key': request_id}, ExpiresIn=300)}
        if physical_id:
            event['PhysicalResourceId'] = physical_id
        status, body = request(f'http://127.0.0.1:{port}/2015-03-31/functions/function/invocations', 'POST', event, timeout=120)
        assert status == 200 and json.loads(body) is None, 'Packaged Lambda invocation failed'
        response = s3.get_object(Bucket='artifacts', Key=request_id)['Body'].read()
        assert len(response) <= 4096
        response = json.loads(response)
        assert response['Status'] == expected, resource_type+' returned '+response['Status']
        assert response['RequestId'] == request_id
        return response['PhysicalResourceId']

    network_args = dict(resource_type='Custom::NetworkPrerequisites', resource_properties={'Mode': 'service-connect-tls'})
    network_identity = invoke(lambdaport, 'Create', **network_args)
    assert network_identity == 'stack/network-prerequisites'
    assert invoke(lambdaport, 'Update', physical_id=network_identity, **network_args) == network_identity
    invoke(lambdaport, 'Delete', physical_id=network_identity, **network_args)
    invoke(lambdaport, 'Create', expected='FAILED', resource_type='Custom::NetworkPrerequisites',
           resource_properties={'Mode': 'none'})
    print('PASS: packaged network prerequisite lifecycle and unsupported-mode rejection.', flush=True)

    groups = ['/aws/rds/instance/test/postgresql', '/aws/rds/instance/test/upgrade',
              '/aws/ecs/containerinsights/test/performance']
    logs.create_log_group(logGroupName=groups[0], tags={'owner': 'existing'})
    logs.create_log_stream(logGroupName=groups[0], logStreamName='existing')
    logs.put_log_events(logGroupName=groups[0], logStreamName='existing',
                        logEvents=[{'timestamp': int(time.time()*1000), 'message': 'retained history'}])
    logs.create_log_group(logGroupName='unrelated')
    retention = {'LogGroups': groups, 'CreateLogGroups': groups[:2], 'RetentionInDays': '30'}
    retention_args = dict(resource_type='Custom::ServiceLogRetention', resource_properties=retention)
    log_identity = invoke(lambdaport, 'Create', **retention_args)
    assert log_identity != 'stack/database-bootstrap'
    state = {group['logGroupName']: group.get('retentionInDays') for group in logs.describe_log_groups()['logGroups']}
    assert state == {groups[0]: 30, groups[1]: 30, 'unrelated': None}
    assert logs.list_tags_log_group(logGroupName=groups[0])['tags'] == {'owner': 'existing'}
    assert logs.get_log_events(logGroupName=groups[0], logStreamName='existing')['events'][0]['message'] == 'retained history'
    retention.update(CreateLogGroups=groups, RetentionInDays='14')
    assert invoke(lambdaport, 'Update', physical_id=log_identity, **retention_args) == log_identity
    retention.update(CreateLogGroups=groups[:2], RetentionInDays='7')
    invoke(lambdaport, 'Update', physical_id=log_identity, **retention_args)
    invoke(lambdaport, 'Delete', physical_id=log_identity, **retention_args)
    assert {group['logGroupName']: group.get('retentionInDays') for group in logs.describe_log_groups()['logGroups']} == {
        **dict.fromkeys(groups, 7), 'unrelated': None}
    assert 'JWT_KEYS' not in json.loads(sm.get_secret_value(SecretId=properties['ApiSecret'])['SecretString'])
    print('PASS: service-log retention adopts existing groups, preserves history, handles disabled Insights and retains policies on deletion.', flush=True)

    invoke(badport, 'Create', expected='FAILED')
    assert 'JWT_KEYS' not in json.loads(sm.get_secret_value(SecretId=properties['ApiSecret'])['SecretString'])

    # Faults affect only the unpacked fixture, never committed migrations or the deployment artifact.
    migration = directory/'lambda'/migration_names[-1]
    original = migration.read_bytes()
    migration.write_bytes(original+b'\nCREATE TABLE public.must_rollback (id int); SELECT 1 / 0;\n')
    try:
        invoke(lambdaport, 'Create', expected='FAILED')
        with psycopg.connect(**database) as connection:
            assert connection.execute("SELECT to_regclass('public.must_rollback'), to_regclass('supabase_aws.migrations')").fetchone() == (None, None)
            assert connection.execute("SELECT count(*) FROM pg_roles WHERE rolname = 'authenticator'").fetchone() == (0,)
        assert 'JWT_KEYS' not in json.loads(sm.get_secret_value(SecretId=properties['ApiSecret'])['SecretString'])
    finally:
        migration.write_bytes(original)

    with psycopg.connect(**database) as connection:
        connection.execute('CREATE ROLE anon LOGIN')
    invoke(lambdaport, 'Create', expected='FAILED')
    with psycopg.connect(**database) as connection:
        assert connection.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname = 'anon'").fetchone() == (True,)
        connection.execute('ALTER ROLE anon NOLOGIN; GRANT postgres TO anon;')
    invoke(lambdaport, 'Create', expected='FAILED')
    with psycopg.connect(**database) as connection:
        assert connection.execute("SELECT pg_has_role('anon', 'postgres', 'MEMBER')").fetchone() == (True,)
        connection.execute('DROP ROLE anon')
    upgrade_names = migration_names[migration_names.index('migrations/20261101131622_rds_query_statistics.sql'):]
    upgrade_files = {directory/'lambda'/name: (directory/'lambda'/name).read_bytes() for name in upgrade_names}
    try:
        for path in upgrade_files:
            path.unlink()
        identity = invoke(lambdaport, 'Create')
        with psycopg.connect(**database) as connection:
            previous_history = connection.execute('SELECT * FROM supabase_aws.migrations ORDER BY version').fetchall()
            assert connection.execute("SELECT count(*) FROM pg_extension WHERE extname = 'pg_stat_statements'").fetchone() == (0,)
    finally:
        for path, content in upgrade_files.items():
            path.write_bytes(content)
    with psycopg.connect(**database) as connection:
        connection.execute('CREATE EXTENSION pg_stat_statements WITH SCHEMA public')
    invoke(lambdaport, 'Update', expected='FAILED', physical_id=identity)
    with psycopg.connect(**database) as connection:
        assert connection.execute('SELECT * FROM supabase_aws.migrations ORDER BY version').fetchall() == previous_history
        assert connection.execute("SELECT extnamespace::regnamespace::text FROM pg_extension WHERE extname = 'pg_stat_statements'").fetchone() == ('public',)
        connection.execute('DROP EXTENSION pg_stat_statements')
    assert invoke(lambdaport, 'Update', physical_id=identity) == identity

    def migration_history():
        with psycopg.connect(**database) as connection:
            return connection.execute('SELECT version, checksum, applied_at FROM supabase_aws.migrations ORDER BY version').fetchall()

    history = migration_history()
    assert history[:len(previous_history)] == previous_history, 'Forward migration changed applied history'
    assert [(version, checksum) for version, checksum, _ in history] == [
        (Path(name).stem, hashlib.sha256((ROOT/'src'/name).read_bytes()).hexdigest()) for name in migration_names]
    try:
        migration.write_bytes(original+b'\n-- Edited after application.\n')
        invoke(lambdaport, 'Update', expected='FAILED', physical_id=identity)
        migration.unlink()
        invoke(lambdaport, 'Update', expected='FAILED', physical_id=identity)
    finally:
        migration.write_bytes(original)
    assert invoke(lambdaport, 'Update', physical_id=identity) == identity
    assert migration_history() == history
    with psycopg.connect(**database) as connection:
        for role in ['anon', 'authenticated', 'service_role', 'authenticator', 'supabase_auth_admin', 'supabase_storage_admin']:
            assert not connection.execute("SELECT has_schema_privilege(%s, 'supabase_aws', 'USAGE')", (role,)).fetchone()[0]
            table_access = connection.execute("SELECT has_table_privilege(%s, oid, 'SELECT') FROM pg_class WHERE relnamespace = 'supabase_aws'::regnamespace AND relkind IN ('r', 'v')", (role,)).fetchall()
            assert table_access and not any(allowed for (allowed,) in table_access), 'Diagnostic table exposed to '+role
            function_access = connection.execute("SELECT has_function_privilege(%s, oid, 'EXECUTE') FROM pg_proc WHERE pronamespace = 'supabase_aws'::regnamespace", (role,)).fetchall()
            assert function_access and not any(allowed for (allowed,) in function_access), 'Diagnostic function exposed to '+role
    with psycopg.connect(**database, autocommit=True) as connection:
        assert connection.execute('SHOW pg_stat_statements.track').fetchone() == ('none',)
        assert connection.execute('SHOW pg_stat_statements.track_utility').fetchone() == ('off',)
        connection.execute('SET pg_stat_statements.track = top')
        for _ in range(2):
            connection.execute('SELECT 731 AS supabase_statistics_enabled_probe').fetchone()
        assert connection.execute("SELECT calls FROM supabase_aws.pg_stat_statements WHERE query LIKE %s", ('%supabase_statistics_enabled_probe%',)).fetchone() == (2,)
        connection.execute('CREATE TEMP TABLE supabase_statistics_utility_probe (id int)')
        connection.execute('SET pg_stat_statements.track = none')
        connection.execute('SELECT 913 AS supabase_statistics_disabled_probe').fetchone()
        assert connection.execute("SELECT count(*) FROM supabase_aws.pg_stat_statements WHERE query LIKE %s OR query LIKE %s", ('%supabase_statistics_disabled_probe%', '%supabase_statistics_utility_probe%')).fetchone() == (0,)
        assert invoke(lambdaport, 'Update', physical_id=identity) == identity
        assert connection.execute("SELECT calls FROM supabase_aws.pg_stat_statements WHERE query LIKE %s", ('%supabase_statistics_enabled_probe%',)).fetchone() == (2,)
        connection.execute('SELECT supabase_aws.pg_stat_statements_reset()').fetchone()
        assert connection.execute('SELECT count(*) FROM supabase_aws.pg_stat_statements').fetchone() == (0,)
    print('PASS: query statistics upgrade, opt-in collection, utility exclusion, private access and retry preservation.', flush=True)
    for name in fixture_secrets:
        fixture_secrets[name] = json.loads(sm.get_secret_value(SecretId=properties[name])['SecretString'])
    keys = dict(fixture_secrets['ApiSecret'])
    sensitive_values = [password, *keys.values()]
    print('PASS: packaged Lambda, rollback/recovery, baseline validation, immutable migration history, private ledger and TLS rejection.', flush=True)

    def resolve_value(value):
        if not isinstance(value, dict):
            return value
        if 'Ref' in value:
            return None if value['Ref'] == 'AWS::NoValue' else values[value['Ref']]
        if 'Fn::If' in value:
            condition, yes, no = value['Fn::If']
            return resolve_value(yes if resolve_value(template['Conditions'][condition]) else no)
        if 'Fn::Not' in value:
            return not resolve_value(value['Fn::Not'][0])
        if 'Fn::Equals' in value:
            left, right = value['Fn::Equals']
            return resolve_value(left) == resolve_value(right)
        if value == {'Fn::GetAtt': ['Database', 'Endpoint.Address']}:
            return 'db'
        if 'Fn::Sub' in value:
            return re.sub(r'\$\{([^}]+)\}', lambda m: str(values[m[1]]), value['Fn::Sub'])
        return {key: resolve_value(item) for key, item in value.items()}

    storage = next(c for c in template['Resources']['StorageTask']['Properties']['ContainerDefinitions'] if c['Name'] == 'storage')
    tenant = next(item['Value'] for item in storage['Environment'] if item['Name'] == 'TENANT_ID')
    assert resolve_value(tenant) == values['AWS::StackName'], 'Empty tenant ID must preserve the stack-name prefix'
    values['StorageTenantId'] = 'existing-tenant'
    assert resolve_value(tenant) == 'existing-tenant'

    def start_service(name, overrides=None, network=None, extra_ports=(), hosts=()):
        task_name = 'AdminTask' if name in ('meta', 'studio') else name.title()+'Task'
        task = template['Resources'][task_name]['Properties']
        assert task['RuntimePlatform']['CpuArchitecture'] == {'Ref': 'EcsCpuArchitecture'}
        definition = next(c for c in task['ContainerDefinitions'] if c['Name'] == name)
        environment = {item['Name']: str(item['Value']) for entry in definition.get('Environment', []) if (item := resolve_value(entry)) is not None}
        for entry in definition.get('Secrets', []):
            item = entry
            if 'Fn::If' in entry:
                condition, yes, no = entry['Fn::If']
                item = yes if resolve_value(template['Conditions'][condition]) else no
            if item == {'Ref': 'AWS::NoValue'}:
                continue
            source = re.fullmatch(r'\$\{([^}]+)\}:([^:]+)::', item['ValueFrom']['Fn::Sub'])
            environment[item['Name']] = fixture_secrets[source[1]][source[2]]
        environment.update(overrides or {})
        entrypoint = definition.get('EntryPoint', [])
        port = 8080 if name == 'meta' else definition['PortMappings'][0]['ContainerPort']
        return start(name, definition['Image'], environment, port if not network else None,
                     name+'.test.internal' if not network else None, mounts=[certmount],
                     entrypoint=entrypoint[0] if entrypoint else None,
                     command=entrypoint[1:]+definition.get('Command', []), network=network,
                     platform=ecs_platform, extra_ports=extra_ports, hosts=hosts)

    certificate = template['Resources']['AuthTask']['Properties']['ContainerDefinitions'][0]
    readiness = next(c for c in template['Resources']['RestTask']['Properties']['ContainerDefinitions'] if c['Name'] == 'readiness')
    s3.put_object(Bucket='artifacts', Key='rds-ca.pem',
                  Body=(directory/'rds-ca.pem').read_bytes() + (ROOT/'src/rds-ca.pem').read_bytes())
    command = ['s3://artifacts/rds-ca.pem' if isinstance(v, dict) else v for v in certificate['Command']]
    start('certificates', certificate['Image'], dict(aws_env, AWS_ENDPOINT_URL='http://moto:5000'),
          mounts=[prefix+':/shared'], command=command, platform=ecs_platform)
    assert docker('wait', prefix+'-certificates') == '0', 'Certificate download failed'
    certmount = prefix+':/certs:ro'

    hook_key = base64.b64encode(secrets.token_bytes(32)).decode()
    fixture_secrets['EmailHookSecretArn'] = {'secret': 'v1,whsec_'+hook_key}
    properties['EmailHookSecretArn'] = sm.create_secret(Name='EmailHookSecretArn',
        SecretString=json.dumps(fixture_secrets['EmailHookSecretArn']))['ARN']
    (directory/'hook.py').write_text("""
import base64, hashlib, hmac, http.server, json, os, pathlib, ssl
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args): pass
    def do_POST(self):
        body = self.rfile.read(int(self.headers['Content-Length']))
        message = (self.headers['webhook-id']+'.'+self.headers['webhook-timestamp']+'.').encode()+body
        signature = base64.b64encode(hmac.digest(base64.b64decode(os.environ['HOOK_KEY']), message, hashlib.sha256)).decode()
        valid = any(hmac.compare_digest(value, 'v1,'+signature) for value in self.headers['webhook-signature'].split())
        if valid: pathlib.Path('/fixture/hook-result.json').write_text(json.dumps(json.loads(body)))
        self.send_response(200 if valid else 401)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{}')
server = http.server.HTTPServer(('0.0.0.0', 443), Handler)
context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
context.load_cert_chain('/fixture/rds-ca.pem', '/fixture/server.key')
server.socket = context.wrap_socket(server.socket, server_side=True)
server.serve_forever()
""")
    start('email-hook', LAMBDA_IMAGE, {'HOOK_KEY': hook_key}, alias='hook',
          mounts=[str(directory)+':/fixture'], entrypoint='/var/lang/bin/python', command=['/fixture/hook.py'])
    values.update(EmailMode='hook', EmailHookUri='https://hook/')
    authport = start_service('auth', {'GOTRUE_DISABLE_SIGNUP': 'false', 'SSL_CERT_FILE': '/certs/rds-ca.pem'})
    wait_http(f'http://127.0.0.1:{authport}/health')
    status, body = request(f'http://127.0.0.1:{authport}/signup', 'POST',
                           {'email': 'hook@example.com', 'password': password})
    assert status == 200, 'Hook signup returned HTTP '+str(status)
    assert not json.loads(body).get('access_token'), 'Hook signup bypassed email confirmation'
    assert json.loads((directory/'hook-result.json').read_text())['user']['email'] == 'hook@example.com'
    docker('rm', '-f', prefix+'-auth')
    containers.remove(prefix+'-auth')
    values['EmailMode'] = 'smtp'
    print('PASS: hook-only Auth signup uses verified HTTPS and a valid webhook signature without SMTP credentials.', flush=True)

    def start_applications():
        authport = start_service('auth', {'GOTRUE_DISABLE_SIGNUP': 'false', 'GOTRUE_MAILER_AUTOCONFIRM': 'true'})
        wait_http(f'http://127.0.0.1:{authport}/health')
        restport = start_service('rest')
        wait_http(f'http://127.0.0.1:{restport}/')
        start('readiness', readiness['Image'], {}, network='container:'+prefix+'-rest',
              entrypoint=readiness['EntryPoint'][0], command=readiness['EntryPoint'][1:]+readiness['Command'], platform=ecs_platform)
        docker('exec', prefix+'-readiness', *readiness['HealthCheck']['Command'][1:])
        storageport = start_service('storage', dict(aws_env, STORAGE_S3_ENDPOINT='http://moto:5000', STORAGE_S3_FORCE_PATH_STYLE='true'))
        wait_http(f'http://127.0.0.1:{storageport}/status')
        hosts = []
        for service in ['auth', 'rest', 'storage']:
            address = docker('inspect', '--format', '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}', prefix+'-'+service)
            hosts.append(f'{service}.service-connect.invalid:{address}')
        gwport = start_service('gateway', {'SERVICE_NAMESPACE': 'service-connect.invalid'}, hosts=hosts)
        url = f'http://127.0.0.1:{gwport}'
        wait_http(url+'/healthz')
        return restport, url

    restport, url = start_applications()
    with psycopg.connect(**database) as connection:
        connection.execute('''CREATE TABLE public.integration_secret (owner uuid, value text);
            ALTER TABLE public.integration_secret ENABLE ROW LEVEL SECURITY;
            CREATE ROLE integration_reader NOLOGIN;
            GRANT SELECT ON public.integration_secret TO anon, integration_reader, service_role;
            GRANT integration_reader TO authenticated;
            CREATE POLICY own_rows ON public.integration_secret FOR SELECT TO authenticated USING (owner = auth.uid());
            INSERT INTO public.integration_secret VALUES (NULL, 'private');''')
    wait_http(f'http://127.0.0.1:{restport}/integration_secret')
    with psycopg.connect(**database) as connection:
        connection.execute('ALTER TABLE public.integration_secret ADD COLUMN added_live text')
    wait_http(f'http://127.0.0.1:{restport}/integration_secret?select=added_live')

    def wait_api_schema(name, present):
        for _ in range(60):
            status, body = request(f'http://127.0.0.1:{restport}/')
            if status == 200 and (name in json.loads(body)['definitions']) == present:
                return
            time.sleep(1)
        raise AssertionError('PostgREST did not reload its schema cache')

    with psycopg.connect(**database) as connection:
        connection.execute('CREATE TABLE public.integration_drop (id int); GRANT SELECT ON public.integration_drop TO anon;')
    wait_api_schema('integration_drop', True)
    with psycopg.connect(**database) as connection:
        connection.execute('DROP TABLE public.integration_drop')
    wait_api_schema('integration_drop', False)
    with psycopg.connect(**database, autocommit=True) as listener, psycopg.connect(**database, autocommit=True) as writer:
        writer.execute('CREATE TYPE public.integration_type AS (value text)')
        listener.execute('LISTEN pgrst')
        writer.execute('ALTER TYPE public.integration_type ADD ATTRIBUTE added_live text')
        assert any(notification.payload == 'reload schema' for notification in listener.notifies(timeout=5, stop_after=1)), 'ALTER TYPE did not notify PostgREST'
        writer.execute('DROP TYPE public.integration_type')
    publishable = {'apikey': keys['SUPABASE_PUBLISHABLE_KEY']}
    for path in ['/', '/pg/', '/realtime/v1/', '/functions/v1/', '/graphql/v1', '/mcp']:
        assert request(url+path, headers={'apikey': keys['SUPABASE_SECRET_KEY']})[0] in (401, 403, 404), path
    for key in [None, 'invalid-key']:
        assert request(url+'/rest/v1/integration_secret', headers={'apikey': key} if key else {})[0] in (401, 403)
    status, body = request(url+'/rest/v1/integration_secret?select=value', headers=publishable)
    assert status == 200 and json.loads(body) == []
    status, body = request(url+'/rest/v1/integration_secret?select=value', headers={'apikey': keys['SUPABASE_SECRET_KEY']})
    assert status == 200 and json.loads(body) == [{'value': 'private'}]
    users = []
    for email in ['first@example.com', 'second@example.com']:
        status, body = request(url+'/auth/v1/signup', 'POST', {'email': email, 'password': password}, publishable)
        assert status == 200, f'Signup returned HTTP {status}'
        user = json.loads(body)
        assert user.get('access_token')
        users.append(user)
        with psycopg.connect(**database) as connection:
            connection.execute('INSERT INTO public.integration_secret VALUES (%s, %s)', (user['user']['id'], email))
    user = users[0]
    header = user['access_token'].split('.')[0]
    assert json.loads(base64.urlsafe_b64decode(header+'='*(-len(header)%4)))['alg'] == 'ES256'
    status, body = request(url+'/auth/v1/.well-known/jwks.json')
    assert status == 200 and json.loads(body)['keys']
    assert all('d' not in key and 'k' not in key for key in json.loads(body)['keys'])

    def check_sessions():
        for account in users:
            headers = dict(publishable, Authorization='Bearer '+account['access_token'])
            status, body = request(url+'/rest/v1/integration_secret?select=value', headers=headers)
            assert status == 200 and json.loads(body) == [{'value': account['user']['email']}], 'JWT identity/RLS isolation failed'
            assert request(url+'/auth/v1/user', headers=headers)[0] == 200
        status, body = request(url+'/auth/v1/token?grant_type=refresh_token', 'POST',
                               {'refresh_token': user['refresh_token']}, publishable)
        assert status == 200 and json.loads(body).get('access_token'), 'Session refresh failed after bootstrap retry'
        user['refresh_token'] = json.loads(body)['refresh_token']

    check_sessions()
    parts = user['access_token'].split('.')
    parts[2] = ('A' if parts[2][0] != 'A' else 'B') + parts[2][1:]
    assert request(url+'/rest/v1/integration_secret', headers=dict(publishable, Authorization='Bearer '+'.'.join(parts)))[0] == 401

    def retry_with_existing_data(bucket, payload):
        objects = s3.list_objects_v2(Bucket='integration').get('Contents', [])
        stored = [item for item in objects if bucket in item['Key']]
        assert len(stored) == 1, 'Storage did not persist the object in S3'
        assert stored[0]['Key'].startswith('existing-tenant/'+bucket+'/'), 'Storage ignored the configured tenant prefix'
        assert s3.get_object(Bucket='integration', Key=stored[0]['Key'])['Body'].read() == payload
        def service_state():
            with psycopg.connect(**database) as connection:
                return [connection.execute(query).fetchall() for query in [
                    'SELECT * FROM auth.schema_migrations ORDER BY version',
                    'SELECT * FROM storage.migrations ORDER BY id',
                    "SELECT pg_get_functiondef(oid) FROM pg_proc WHERE oid IN ('auth.uid()'::regprocedure, 'auth.jwt()'::regprocedure) ORDER BY oid",
                    "SELECT relname, relacl FROM pg_class WHERE relnamespace = 'storage'::regnamespace AND relname IN ('buckets', 'objects') ORDER BY relname"]]

        before = service_state()
        assert invoke(lambdaport, 'Update', physical_id=identity) == identity
        assert migration_history() == history
        assert service_state() == before, 'Retry changed service migrations, helpers or grants'
        # An unversioned installation must be adopted without replaying service-owned SQL.
        with psycopg.connect(**database) as connection:
            connection.execute('''DROP SCHEMA supabase_aws CASCADE;
                ALTER ROLE authenticated NOINHERIT;
                GRANT anon, authenticated, service_role TO supabase_storage_admin;
                GRANT supabase_auth_admin, supabase_storage_admin TO postgres WITH ADMIN TRUE, INHERIT TRUE, SET TRUE;
                SET ROLE supabase_storage_admin;
                ALTER DEFAULT PRIVILEGES IN SCHEMA storage GRANT ALL ON TABLES TO postgres, anon, authenticated, service_role;
                GRANT ALL ON storage.migrations TO postgres, anon, authenticated, service_role;
                RESET ROLE;''')
        assert invoke(lambdaport, 'Update', physical_id=identity) == identity
        assert service_state() == before, 'Baseline adoption changed service-owned state'
        with psycopg.connect(**database) as connection:
            for role in ['anon', 'authenticated', 'service_role']:
                assert not connection.execute("SELECT has_table_privilege(%s, 'storage.migrations', 'SELECT')", (role,)).fetchone()[0]
            assert connection.execute("SELECT rolinherit FROM pg_roles WHERE rolname = 'authenticated'").fetchone() == (True,)
        for name, value in fixture_secrets.items():
            assert json.loads(sm.get_secret_value(SecretId=properties[name])['SecretString']) == value, 'Bootstrap rotated existing credentials'
        check_sessions()

    check_storage(url, keys, after_upload=retry_with_existing_data)
    assert not s3.list_objects_v2(Bucket='integration').get('Contents'), 'Storage deletion left an S3 object'
    versions = s3.list_object_versions(Bucket='integration')
    assert versions.get('Versions') and versions.get('DeleteMarkers'), 'Versioned storage must retain history after API deletion'
    for version in versions['Versions'] + versions['DeleteMarkers']:
        s3.delete_object(Bucket='integration', Key=version['Key'], VersionId=version['VersionId'])
    purged = s3.list_object_versions(Bucket='integration')
    assert not purged.get('Versions') and not purged.get('DeleteMarkers'), 'Administrator purge left version history'
    print('PASS: API deletion retains S3 history; explicit version purge removes the disposable fixture history.', flush=True)
    def check_sensitive_logs():
        for name in ['gateway', 'lambda', 'lambda-untrusted']:
            logs = docker('logs', prefix+'-'+name, stderr=subprocess.STDOUT)
            assert all(value not in logs for value in sensitive_values), 'Credentials appeared in container logs'

    def restart_applications():
        check_sensitive_logs()
        for name in ['gateway', 'storage', 'readiness', 'rest', 'auth']:
            container = prefix+'-'+name
            docker('rm', '-f', container)
            containers.remove(container)
        return start_applications()[1]

    before_rotation = migration_history()
    old_passwords = {}
    for name in ['AuthSecret', 'RestSecret', 'StorageSecret']:
        value = dict(fixture_secrets[name])
        old_passwords[value['username']] = value['password']
        value['password'] = secrets.token_urlsafe(36)+':/@'
        sensitive_values.extend([old_passwords[value['username']], value['password']])
        sm.put_secret_value(SecretId=properties[name], SecretString=json.dumps(value))
    old_keys = dict(keys)
    replacement = generate_keys(keys['JWT_SECRET'])
    for name in ['SUPABASE_PUBLISHABLE_KEY', 'SUPABASE_SECRET_KEY']:
        keys[name] = replacement[name]
    sensitive_values.extend(keys.values())
    sm.put_secret_value(SecretId=properties['ApiSecret'], SecretString=json.dumps(keys))
    properties['CredentialRevision'] = 'password-and-opaque-keys'
    for _ in range(2):
        assert invoke(lambdaport, 'Update', physical_id=identity) == identity
    assert migration_history() == before_rotation
    assert json.loads(sm.get_secret_value(SecretId=properties['ApiSecret'])['SecretString']) == keys
    for name in ['AuthSecret', 'RestSecret', 'StorageSecret']:
        value = json.loads(sm.get_secret_value(SecretId=properties[name])['SecretString'])
        fixture_secrets[name] = value
        with psycopg.connect(value['uri'], host=database['host'], port=database['port'],
                             sslrootcert=database['sslrootcert']) as connection:
            assert connection.execute('SELECT current_user').fetchone() == (value['username'],)
        try:
            psycopg.connect(**dict(database, user=value['username'], password=old_passwords[value['username']])).close()
        except psycopg.OperationalError as error:
            assert 'password authentication failed' in str(error)
        else:
            raise AssertionError('Old service password still authenticates')
    fixture_secrets['ApiSecret'] = dict(keys)
    url = restart_applications()
    publishable = {'apikey': keys['SUPABASE_PUBLISHABLE_KEY']}
    check_sessions()
    assert request(url+'/rest/v1/integration_secret', headers={'apikey': old_keys['SUPABASE_PUBLISHABLE_KEY']})[0] in (401, 403)
    assert request(url+'/rest/v1/integration_secret', headers={'apikey': old_keys['SUPABASE_SECRET_KEY']})[0] in (401, 403)
    check_storage(url, keys)
    print('PASS: service passwords and opaque API keys rotate; repaired URIs reconnect, retries preserve signing keys and sessions.', flush=True)

    old_keys = dict(keys)
    keys = generate_keys(secrets.token_urlsafe(36))
    sensitive_values.extend(keys.values())
    sm.put_secret_value(SecretId=properties['ApiSecret'], SecretString=json.dumps(keys))
    properties['CredentialRevision'] = 'signing-bundle'
    assert invoke(lambdaport, 'Update', physical_id=identity) == identity
    assert json.loads(sm.get_secret_value(SecretId=properties['ApiSecret'])['SecretString']) == keys
    fixture_secrets['ApiSecret'] = dict(keys)
    url = restart_applications()
    publishable = {'apikey': keys['SUPABASE_PUBLISHABLE_KEY']}
    for token in [user['access_token'], old_keys['ANON_KEY']]:
        assert request(url+'/rest/v1/integration_secret', headers=dict(publishable, Authorization='Bearer '+token))[0] == 401
    status, body = request(url+'/auth/v1/token?grant_type=refresh_token', 'POST',
                           {'refresh_token': user['refresh_token']}, publishable)
    assert status == 200, 'Signing-key rotation must not be mistaken for session revocation'
    user.update(json.loads(body))
    headers = dict(publishable, Authorization='Bearer '+user['access_token'])
    status, body = request(url+'/rest/v1/integration_secret?select=value', headers=headers)
    assert status == 200 and json.loads(body) == [{'value': user['user']['email']}]
    assert migration_history() == before_rotation
    check_storage(url, keys)
    print('PASS: complete signing-key cutover rejects old access tokens; retained refresh sessions obtain valid new tokens.', flush=True)

    metaport = start_service('meta', extra_ports=(3000,))
    wait_http(f'http://127.0.0.1:{metaport}/health')
    assert request(f'http://127.0.0.1:{metaport}/tables')[0] == 200, 'Meta could not read database metadata'
    start_service('studio', network='container:'+prefix+'-meta')
    studioport = int(docker('port', prefix+'-meta', '3000').rsplit(':', 1)[1])
    wait_http(f'http://127.0.0.1:{studioport}/api/platform/profile')
    print(f'PASS: {ecs_architecture} application/helper containers and paired Studio/meta startup.', flush=True)
    print('PASS: pinned Envoy routes through host-file aliases absent from DNS.', flush=True)
    assert invoke(lambdaport, 'Delete', physical_id=identity) == identity
    assert request(url+'/auth/v1/user', headers=dict(publishable, Authorization='Bearer '+user['access_token']))[0] == 200
    modes = docker('exec', prefix+'-gateway', 'stat', '-c', '%a', '/etc/envoy/lds.yaml')
    assert all(int(mode, 8) & 0o077 == 0 for mode in modes.splitlines()), 'Gateway configuration exposed secrets to other users'
    check_sensitive_logs()
    gateway = template['Resources']['GatewayTask']['Properties']['ContainerDefinitions'][0]
    start('gateway-missing-env', gateway['Image'], {}, entrypoint=gateway['EntryPoint'][0],
          command=gateway['EntryPoint'][1:]+gateway['Command'], platform=ecs_platform)
    assert docker('wait', prefix+'-gateway-missing-env') != '0', 'Gateway accepted missing configuration'
    docker('stop', prefix+'-rest')
    try:
        docker('exec', prefix+'-readiness', *readiness['HealthCheck']['Command'][1:])
    except subprocess.CalledProcessError:
        pass
    else:
        raise AssertionError('Readiness must fail when PostgREST is unavailable')
    print('PASS: fresh install/adoption/retry preserve service data, keys and grants; live DDL/drop reload, inherited RLS, certificate init, readiness and gateway isolation.', flush=True)


def main():
    network = volume = False
    try:
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            package = Path(sys.argv[1]).resolve() if len(sys.argv) > 1 else directory/'bootstrap.zip'
            if len(sys.argv) == 1:
                subprocess.run([str(ROOT/'src/deploy.sh'), '--package', str(package)], check=True, timeout=300)
            docker('network', 'create', prefix)
            network = True
            docker('volume', 'create', prefix)
            volume = True
            run(directory, package)
    except Exception:
        for name in containers:
            try:
                print('Container status:', name, docker('inspect', '--format', '{{.State.Status}}', name))
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                pass
        raise
    finally:
        failures = []
        resources = [('rm', '-f', '-v', name) for name in reversed(containers)]
        if network:
            resources.append(('network', 'rm', prefix))
        if volume:
            resources.append(('volume', 'rm', prefix))
        for args in resources:
            try:
                docker(*args)
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired):
                failures.append(args[-1])
        if failures:
            message = 'Docker cleanup failed: '+', '.join(failures)
            if sys.exception():
                sys.exception().add_note(message)
            else:
                raise RuntimeError(message)


if __name__ == '__main__':
    main()
