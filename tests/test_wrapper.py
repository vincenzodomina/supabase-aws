import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from cfnlint.decode import decode_str
from test_deploy import CLI, ROOT

FOUNDATION = {
    'VpcId': 'vpc-0123456789abcdef0',
    'PublicSubnetIds': 'subnet-11111111111111111,subnet-22222222222222222',
    'PrivateSubnetIds': 'subnet-33333333333333333,subnet-44444444444444444',
    'CertificateArn': 'arn:aws:acm:us-east-1:123456789012:certificate/demo',
    'ServiceConnectPrivateCaArn': 'arn:aws:acm-pca:us-east-1:123456789012:certificate-authority/demo',
    'SesSmtpSecretArn': 'arn:aws:secretsmanager:us-east-1:123456789012:secret:smtp-EXAMPLE',
    'ArtifactsBucket': 'demo-artifacts',
}
# AWS's public example secret; never use real credentials in this fixture.
SECRET = {'username': 'AKIAIOSFODNN7EXAMPLE', 'secretAccessKey': 'wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY'}
SMTP_PASSWORD = 'BLBM/9hSUELfq8Gw+rU1YcBjkOxGbhT2XG763xVLGWL9'

WRAPPER_CLI = CLI.replace("if tool == 'python3':", """if tool == 'python3':
    if args[:2] != ['-m', 'pip']:
        os.execv(sys.executable, [sys.executable, *args])
    if args == ['-m', 'pip', '--version']: sys.exit(0)""").replace("command = args[:2]", """command = args[:2]
if command == ['route53', 'get-hosted-zone']:
    if scenario == 'zone-denied': sys.exit('AccessDenied')
    print(json.dumps({'HostedZone': {'Name': 'example.com.', 'Config': {'PrivateZone': scenario == 'private-zone'}}}))
    sys.exit(0)
if command == ['sts', 'get-caller-identity']:
    print('123456789012'); sys.exit(0)
if command == ['ses', 'wait']:
    sys.exit('verification pending' if scenario == 'ses-pending' else 0)
if command == ['secretsmanager', 'get-secret-value']:
    if scenario == 'secret-denied': sys.exit('AccessDenied')
    print((root/'secret').read_text()); sys.exit(0)
if command == ['secretsmanager', 'put-secret-value']:
    if scenario == 'secret-write-denied': sys.exit('AccessDenied')
    value = args[args.index('--secret-string')+1]
    path = pathlib.Path(value.removeprefix('file://'))
    assert path.stat().st_mode & 0o077 == 0
    (root/'secret').write_text(path.read_text()); sys.exit(0)
if command == ['cloudformation', 'deploy'] and args[args.index('--stack-name')+1].endswith('-foundation'):
    if scenario == 'foundation-fails': sys.exit('foundation failed')
    (root/'context').write_text(json.dumps({k: os.environ[k] for k in ['AWS_PROFILE', 'AWS_REGION']}))
    sys.exit(0)
if command == ['cloudformation', 'describe-stacks'] and args[args.index('--stack-name')+1].endswith('-foundation'):
    values = json.loads((root/'foundation').read_text())
    print(json.dumps({'Stacks': [{'Outputs': [{'OutputKey': k, 'OutputValue': v} for k, v in values.items()]}]}))
    sys.exit(0)
""")


class WrapperTest(unittest.TestCase):
    def run_wrapper(self, scenario, secret=None, extra_args=()):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for tool in ('aws', 'python3'):
                path = root/tool
                path.write_text('#!' + sys.executable + '\n' + WRAPPER_CLI)
                path.chmod(0o755)
            (root/'secret').write_text(json.dumps(secret or SECRET))
            (root/'foundation').write_text(json.dumps(FOUNDATION))
            result = subprocess.run(
                ['bash', str(ROOT/'wrapper/deploy.sh'), '--domain', 'supabase.example.com',
                 '--hosted-zone-id', 'Z123456', '--profile', 'demo-profile', *extra_args],
                cwd=directory, capture_output=True, text=True,
                env=dict(os.environ, PATH=directory+os.pathsep+os.environ['PATH'],
                         CLI_FIXTURE=directory, SCENARIO=scenario), timeout=40)
            calls = [json.loads(line) for line in (root/'calls').read_text().splitlines()] if (root/'calls').exists() else []
            context = json.loads((root/'context').read_text()) if (root/'context').exists() else None
            for call in calls:
                if '--secret-string' in call:
                    self.assertFalse(Path(call[call.index('--secret-string')+1].removeprefix('file://')).exists())
            return result, calls, json.loads((root/'secret').read_text()), context

    def test_fresh_demo_passes_foundation_to_real_deployer_without_leaking_credentials(self):
        result, calls, secret, context = self.run_wrapper('new')
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(context, {'AWS_PROFILE': 'demo-profile', 'AWS_REGION': 'us-east-1'})
        self.assertEqual(secret, {'username': SECRET['username'], 'password': SMTP_PASSWORD})
        foundation, errors = decode_str((ROOT/'wrapper/foundation.yaml').read_text())
        self.assertFalse(errors)
        self.assertEqual(FOUNDATION.keys(), foundation['Outputs'].keys())
        deployments = [c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy']]
        self.assertEqual([c[c.index('--stack-name')+1] for c in deployments],
                         ['supabase-demo-foundation', 'supabase-demo-supabase'])
        call = deployments[1]
        self.assertIn('--no-fail-on-empty-changeset', call)
        for key, value in FOUNDATION.items():
            self.assertIn(f'{key}={value}', call)
        self.assertIn('DatabaseSubnetIds='+FOUNDATION['PrivateSubnetIds'], call)
        self.assertIn('InternalTrafficEncryption=service-connect-tls', call)
        self.assertIn('ApiExposure=internet-facing', call)
        self.assertIn('CredentialRevision='+SECRET['username'], call)
        self.assertIn('SiteUrl=https://supabase.example.com', call)
        self.assertIn('SesSenderEmail=noreply@supabase.example.com', call)
        self.assertIn('DatabaseMultiAZ=false', call)
        for service in ('Gateway', 'Auth', 'Rest', 'Storage'):
            self.assertIn(f'{service}MinTasks=1', call)
            self.assertIn(f'{service}MaxTasks=1', call)
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True))
        self.assertFalse(errors)
        params = dict(arg.split('=', 1) for arg in call[call.index('--parameter-overrides')+1:] if '=' in arg)
        required = {key for key, value in template['Parameters'].items() if 'Default' not in value}
        self.assertFalse(required - params.keys())
        self.assertFalse(params.keys() - template['Parameters'].keys())
        logged = result.stdout + result.stderr + json.dumps(calls)
        self.assertNotIn(SECRET['secretAccessKey'], logged)
        self.assertNotIn(SMTP_PASSWORD, logged)
        written = next(i for i, c in enumerate(calls) if c[:3] == ['aws', 'secretsmanager', 'put-secret-value'])
        self.assertLess(written, calls.index(call))

    def test_unchanged_demo_reuses_smtp_credentials(self):
        credentials = {'username': SECRET['username'], 'password': SMTP_PASSWORD}
        result, calls, secret, _ = self.run_wrapper('empty', secret=credentials)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(secret, credentials)
        self.assertFalse(any(c[:3] == ['aws', 'secretsmanager', 'put-secret-value'] for c in calls))

    def test_failure_stops_before_deploying_supabase(self):
        for scenario in ('zone-denied', 'private-zone', 'foundation-fails', 'secret-denied', 'secret-write-denied', 'ses-pending'):
            with self.subTest(scenario=scenario):
                result, calls, _, _ = self.run_wrapper(scenario)
                self.assertNotEqual(result.returncode, 0)
                deployments = [c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy']]
                self.assertFalse(any('supabase-demo-supabase' in c for c in deployments))

    def test_invalid_inputs_do_not_provision_resources(self):
        for args in (['--name', 'UPPERCASE'], ['--hosted-zone-id', '/hostedzone/Z123'],
                     ['--domain', 'unrelated.test'], ['--domain', 'example.com']):
            with self.subTest(args=args):
                result, calls, _, _ = self.run_wrapper('new', extra_args=args)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'deploy'] for c in calls))

    def test_smtp_password_is_region_specific(self):
        result = subprocess.check_output([sys.executable, str(ROOT/'wrapper/smtp-password.py'), 'eu-west-1'],
                                         input=json.dumps(SECRET), text=True)
        self.assertEqual(json.loads(result), {'username': SECRET['username'], 'password': 'BMW5RDrXmmVs0lV7GpI4oLkHXpZ4stDsk6q91z1g38Pk'})
