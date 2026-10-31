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
import socket
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
template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
assert not errors, errors
DATABASE_IMAGE = 'postgres:17-bookworm@sha256:839fbb5a4942a7758dd93e18b42ea5649807491a0545b5eeeb8f0b8bb11857a5'
LAMBDA_IMAGE = 'public.ecr.aws/lambda/python:3.13@sha256:4878ebcc1df9c6b55770d66b2f06b7530b068dac32d19befc9fa7e118abc901d'
MOTO_IMAGE = 'motoserver/moto:5.2.5@sha256:59a1965d845249b04def37a0e2565f9cc6c44543cb89b68e90f28f344b3b0d1a'
prefix = 'supabase-check-' + secrets.token_hex(4)
containers = []


def docker(*args, stderr=subprocess.PIPE, **kwargs):
    return subprocess.check_output(['docker', *args], text=True, stderr=stderr, timeout=180, **kwargs).strip()


def start(name, image, environment, port=None, alias=None, mounts=(), entrypoint=None, command=(), network=None):
    name = prefix+'-'+name
    args = ['run', '-d', '--platform', 'linux/amd64', '--name', name, '--network', network or prefix]
    if port:
        args += ['-p', f'127.0.0.1::{port}']
    if alias:
        args += ['--network-alias', alias]
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
    internal_s3 = session.client('s3', endpoint_url='http://moto:5000', config=config)
    for bucket in ['artifacts', 'integration']:
        s3.create_bucket(Bucket=bucket)

    password = secrets.token_hex(24)
    fixture_secrets = {'ApiSecret': {'JWT_SECRET': secrets.token_hex(32)},
                       'DatabaseSecret': {'username': 'postgres', 'password': password},
                       'AuthSecret': {'password': password}, 'RestSecret': {'password': password},
                       'StorageSecret': {'password': password}, 'SesSmtpSecretArn': {'username': '', 'password': ''}}
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
            .add_extension(x509.SubjectAlternativeName([x509.DNSName('db'), x509.IPAddress(ipaddress.ip_address('127.0.0.1'))]), critical=False)
            .sign(key, hashes.SHA256()))
    (directory/'rds-ca.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (directory/'server.key').write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    certmount = str(directory)+':/certs:ro'
    pgport = start('db', DATABASE_IMAGE, {'POSTGRES_PASSWORD': password}, 5432, 'db', mounts=[certmount],
                   entrypoint='/bin/bash', command=['-ec',
                   'cp /certs/server.key /certs/rds-ca.pem /tmp/; chown postgres:postgres /tmp/server.key /tmp/rds-ca.pem; chmod 0600 /tmp/server.key /tmp/rds-ca.pem; exec docker-entrypoint.sh postgres -c ssl=on -c ssl_cert_file=/tmp/rds-ca.pem -c ssl_key_file=/tmp/server.key'])
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

    lambda_env = dict(aws_env, AWS_ENDPOINT_URL_SECRETS_MANAGER='http://moto:5000')
    mounts = [str(directory/'lambda')+':/var/task:ro']
    badport = start('lambda-untrusted', LAMBDA_IMAGE, lambda_env, 8080, mounts=mounts, command=[runtime['Handler']])
    lambdaport = start('lambda', LAMBDA_IMAGE, lambda_env, 8080,
                       mounts=mounts+[str(directory/'rds-ca.pem')+':/var/task/rds-ca.pem:ro'], command=[runtime['Handler']])

    def invoke(port, request_type, expected='SUCCESS', physical_id=None):
        for _ in range(60):
            try:
                with socket.create_connection(('127.0.0.1', port), timeout=1):
                    break
            except OSError:
                time.sleep(1)
        else:
            raise RuntimeError('Lambda runtime startup timed out')
        request_id = secrets.token_hex(8)
        event = {'RequestType': request_type, 'StackId': 'stack', 'RequestId': request_id,
                 'LogicalResourceId': 'Bootstrap', 'ResourceProperties': properties,
                 'ResponseURL': internal_s3.generate_presigned_url('put_object', Params={'Bucket': 'artifacts', 'Key': request_id}, ExpiresIn=300)}
        if physical_id:
            event['PhysicalResourceId'] = physical_id
        status, body = request(f'http://127.0.0.1:{port}/2015-03-31/functions/function/invocations', 'POST', event, timeout=120)
        assert status == 200 and json.loads(body) is None, 'Packaged Lambda invocation failed'
        response = s3.get_object(Bucket='artifacts', Key=request_id)['Body'].read()
        assert len(response) <= 4096
        response = json.loads(response)
        assert response['Status'] == expected, 'Bootstrap returned '+response['Status']
        assert response['RequestId'] == request_id
        return response['PhysicalResourceId']

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
    identity = invoke(lambdaport, 'Create')

    def migration_history():
        with psycopg.connect(**database) as connection:
            return connection.execute('SELECT version, checksum, applied_at FROM supabase_aws.migrations ORDER BY version').fetchall()

    history = migration_history()
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
    for name in fixture_secrets:
        fixture_secrets[name] = json.loads(sm.get_secret_value(SecretId=properties[name])['SecretString'])
    keys = dict(fixture_secrets['ApiSecret'])
    print('PASS: packaged Lambda, rollback/recovery, baseline validation, immutable migration history, private ledger and TLS rejection.', flush=True)

    def start_service(name, overrides=None):
        task = template['Resources'][name.title()+'Task']['Properties']
        definition = next(c for c in task['ContainerDefinitions'] if c['Name'] == name)
        environment = {}
        for item in definition.get('Environment', []):
            value = item['Value']
            if isinstance(value, dict):
                if 'Ref' in value:
                    value = values[value['Ref']]
                else:
                    value = re.sub(r'\$\{([^}]+)\}', lambda m: str(values[m[1]]), value['Fn::Sub'])
            environment[item['Name']] = str(value)
        for item in definition.get('Secrets', []):
            source = re.fullmatch(r'\$\{([^}]+)\}:([^:]+)::', item['ValueFrom']['Fn::Sub'])
            environment[item['Name']] = fixture_secrets[source[1]][source[2]]
        environment.update(overrides or {})
        entrypoint = definition.get('EntryPoint', [])
        return start(name, definition['Image'], environment, definition['PortMappings'][0]['ContainerPort'],
                     name+'.test.internal', mounts=[certmount], entrypoint=entrypoint[0] if entrypoint else None,
                     command=entrypoint[1:]+definition.get('Command', []))

    certificate = template['Resources']['AuthTask']['Properties']['ContainerDefinitions'][0]
    readiness = next(c for c in template['Resources']['RestTask']['Properties']['ContainerDefinitions'] if c['Name'] == 'readiness')
    s3.put_object(Bucket='artifacts', Key='rds-ca.pem', Body=(directory/'rds-ca.pem').read_bytes())
    command = ['s3://artifacts/rds-ca.pem' if isinstance(v, dict) else v for v in certificate['Command']]
    start('certificates', certificate['Image'], dict(aws_env, AWS_ENDPOINT_URL='http://moto:5000'),
          mounts=[prefix+':/shared'], command=command)
    assert docker('wait', prefix+'-certificates') == '0', 'Certificate download failed'
    certmount = prefix+':/certs:ro'
    authport = start_service('auth', {'GOTRUE_DISABLE_SIGNUP': 'false', 'GOTRUE_MAILER_AUTOCONFIRM': 'true'})
    wait_http(f'http://127.0.0.1:{authport}/health')
    restport = start_service('rest')
    wait_http(f'http://127.0.0.1:{restport}/')
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
    start('readiness', readiness['Image'], {}, network='container:'+prefix+'-rest',
          entrypoint=readiness['EntryPoint'][0], command=readiness['EntryPoint'][1:]+readiness['Command'])
    docker('exec', prefix+'-readiness', *readiness['HealthCheck']['Command'][1:])
    storageport = start_service('storage', dict(aws_env, STORAGE_S3_ENDPOINT='http://moto:5000', STORAGE_S3_FORCE_PATH_STYLE='true'))
    wait_http(f'http://127.0.0.1:{storageport}/status')
    gwport = start_service('gateway')
    url = f'http://127.0.0.1:{gwport}'
    wait_http(url+'/healthz')
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
    assert invoke(lambdaport, 'Delete', physical_id=identity) == identity
    assert request(url+'/auth/v1/user', headers=dict(publishable, Authorization='Bearer '+user['access_token']))[0] == 200
    modes = docker('exec', prefix+'-gateway', 'stat', '-c', '%a', '/etc/envoy/lds.yaml')
    assert all(int(mode, 8) & 0o077 == 0 for mode in modes.splitlines()), 'Gateway configuration exposed secrets to other users'
    for name in ['gateway', 'lambda', 'lambda-untrusted']:
        logs = docker('logs', prefix+'-'+name, stderr=subprocess.STDOUT)
        assert all(value not in logs for value in [password, *keys.values()]), 'Credentials appeared in container logs'
    gateway = template['Resources']['GatewayTask']['Properties']['ContainerDefinitions'][0]
    start('gateway-missing-env', gateway['Image'], {}, entrypoint=gateway['EntryPoint'][0],
          command=gateway['EntryPoint'][1:]+gateway['Command'])
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
