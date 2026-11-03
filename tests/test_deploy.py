import json
import os
import re
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
import urllib.request

import yaml

from cfnlint.decode import decode_str

ROOT = Path(__file__).resolve().parents[1]

CLI = '''
import json, os, pathlib, sys
root = pathlib.Path(os.environ['CLI_FIXTURE'])
args = sys.argv[1:]
tool = pathlib.Path(sys.argv[0]).name
with (root/'calls').open('a') as log:
    log.write(json.dumps([tool, *args])+'\\n')
scenario = os.environ['SCENARIO']
if tool == 'python3':
    if scenario == 'package-fails': sys.exit('wheel download failed')
    target = pathlib.Path(args[args.index('--target')+1])
    target.mkdir()
    (target/'dependency.py').write_text('# dependency fixture')
    sys.exit(0)
command = args[:2]
if command == ['cloudformation', 'describe-stacks']:
    if scenario == 'denied':
        sys.exit('AccessDenied: cannot describe stack')
    if scenario in ['new', 'deploy-fails'] and not (root/'created').exists():
        sys.exit('ValidationError: Stack with id demo does not exist')
    status = scenario.removeprefix('status:') if scenario.startswith('status:') else 'REVIEW_IN_PROGRESS' if scenario == 'review' else 'CREATE_COMPLETE'
    print(json.dumps({'Stacks': [{'StackStatus': status}]}))
elif command == ['cloudformation', 'deploy']:
    if scenario == 'deploy-fails': sys.exit('CloudFormation deployment failed')
    (root/'template').write_text(pathlib.Path(args[args.index('--template-file')+1]).read_text())
    if scenario == 'empty':
        if '--no-fail-on-empty-changeset' not in args: sys.exit('No changes to deploy')
        print('No changes to deploy'); sys.exit(0)
    (root/'created').touch()
elif command == ['cloudformation', 'get-stack-policy']:
    if scenario == 'policy-read-denied': sys.exit('AccessDenied: cannot read policy')
    if scenario == 'no-policy': sys.exit(0)
    if scenario in ['new', 'review', 'null-policy']:
        print('null' if scenario == 'null-policy' else '{}'); sys.exit(0)
    statements = [
        {'Effect': 'Allow', 'Action': 'Update:*', 'Principal': '*', 'Resource': '*'},
        {'Effect': 'Deny', 'Action': 'Update:Delete', 'Principal': '*', 'Resource': 'LogicalResourceId/Bucket'},
        {'Effect': 'Deny', 'Action': ['Update:Replace', 'Update:Delete'], 'Principal': '*', 'Resource': 'LogicalResourceId/Database'}]
    if scenario == 'cached':
        statements.append({'Effect': 'Deny', 'Action': ['Update:Replace', 'Update:Delete'], 'Principal': '*', 'Resource': '*',
                           'Condition': {'StringEquals': {'ResourceType': ['AWS::RDS::DBInstance', 'AWS::S3::Bucket']}}})
    print(json.dumps({'StackPolicyBody': json.dumps({'Statement': statements})}))
elif command == ['cloudformation', 'set-stack-policy']:
    if scenario == 'policy-write-denied': sys.exit('AccessDenied: cannot set policy')
elif command == ['s3api', 'put-object']:
    source = pathlib.Path(args[args.index('--body')+1])
    (root/source.name).write_bytes(source.read_bytes())
    if scenario in ['cached', 'empty']: sys.exit('PreconditionFailed: object exists')
    if scenario == 'upload-denied': sys.exit('AccessDenied: cannot write artifact')
else: sys.exit('Unexpected CLI call: '+repr(args))
'''


def configured(document, overrides):
    parameters = {name: spec.get('Default', name) for name, spec in document['Parameters'].items()} | overrides
    def resolve(value):
        if isinstance(value, list):
            return [resolved for item in value if (resolved := resolve(item)) is not None]
        if not isinstance(value, dict):
            return value
        if 'Ref' in value:
            return None if value['Ref'] == 'AWS::NoValue' else parameters.get(value['Ref'], value)
        if 'Condition' in value and len(value) == 1:
            return resolve(document['Conditions'][value['Condition']])
        if 'Fn::If' in value:
            condition, yes, no = value['Fn::If']
            return resolve(yes if resolve(document['Conditions'][condition]) else no)
        if 'Fn::Equals' in value:
            left, right = value['Fn::Equals']
            return resolve(left) == resolve(right)
        if 'Fn::Not' in value:
            return not resolve(value['Fn::Not'][0])
        if 'Fn::And' in value:
            return all(resolve(item) for item in value['Fn::And'])
        if 'Fn::Or' in value:
            return any(resolve(item) for item in value['Fn::Or'])
        return {key: resolve(item) for key, item in value.items()}
    return {name: resolve(resource) for name, resource in document['Resources'].items()
            if 'Condition' not in resource or resolve(document['Conditions'][resource['Condition']])}


class DeploymentTest(unittest.TestCase):
    def run_deploy(self, scenario, parameters=None, role_arn=None, extra_args=(), source=ROOT/'src'):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for tool in ['aws', 'python3']:
                path = root/tool
                path.write_text('#!'+sys.executable+'\n'+CLI)
                path.chmod(0o755)
            params = root/'parameters.json'
            params.write_text(json.dumps(parameters if parameters is not None else {'SiteUrl': 'https://example.com/a b'}))
            args = ['bash', str(source/'deploy.sh'), '--stack-name', 'demo', '--region', 'us-east-1',
                    '--parameters', params.name, '--artifacts-bucket', 'artifacts']
            args.extend(extra_args)
            if role_arn is not None:
                args.extend(['--role-arn', role_arn])
            result = subprocess.run(args, capture_output=True, text=True, cwd=directory,
                                    env=dict(os.environ, PATH=directory+os.pathsep+os.environ['PATH'],
                                             CLI_FIXTURE=directory, SCENARIO=scenario), timeout=30)
            calls = [json.loads(line) for line in (root/'calls').read_text().splitlines()] if (root/'calls').exists() else []
            self.template = (root/'template').read_text() if (root/'template').exists() else None
            self.artifacts = {name: (root/name).read_bytes() for name in ['bootstrap.zip', 'rds-ca.pem'] if (root/name).exists()}
            for call in calls:
                if call[:3] == ['aws', 'cloudformation', 'set-stack-policy']:
                    statements = json.loads(call[call.index('--stack-policy-body')+1])['Statement']
                    protection = {'Effect': 'Deny', 'Action': ['Update:Replace', 'Update:Delete'], 'Principal': '*', 'Resource': '*',
                                  'Condition': {'StringEquals': {'ResourceType': ['AWS::RDS::DBInstance', 'AWS::S3::Bucket']}}}
                    self.assertEqual(statements.count(protection), 1)
            return result, calls

    def test_default_deployment_uploads_artifacts_and_protects_created_data(self):
        role = 'arn:aws:iam::123456789012:role/platform/cloudformation'
        for scenario in ['new', 'review']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario, role_arn=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual([c[:3] for c in calls if c[0] == 'aws'], [
                    ['aws', 'cloudformation', 'describe-stacks'], ['aws', 's3api', 'put-object'],
                    ['aws', 's3api', 'put-object'], ['aws', 'cloudformation', 'deploy'],
                    ['aws', 'cloudformation', 'get-stack-policy'], ['aws', 'cloudformation', 'set-stack-policy']])
                deployment = next(c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy'])
                self.assertNotIn('--no-execute-changeset', deployment)
                self.assertIn('--no-fail-on-empty-changeset', deployment)
                self.assertEqual(deployment[deployment.index('--role-arn')+1], role)
                self.assertFalse(any('--role-arn' in c for c in calls if c != deployment))
                self.assertIn('ArtifactsBucket=artifacts', deployment)
                self.assertTrue(any(c.startswith('BootstrapKey=supabase-aws/bootstrap/') for c in deployment))
                self.assertTrue(any(c.startswith('CertificateKey=supabase-aws/certificates/') for c in deployment))
                self.assertIn('SiteUrl=https://example.com/a b', deployment)
                self.assertIn('--s3-bucket', deployment)
                self.assertIn('Stack deployment complete', result.stdout)
                template, errors = decode_str(self.template)
                self.assertFalse(errors)
                gateway = template['Resources']['GatewayTask']['Properties']['ContainerDefinitions'][0]
                self.assertTrue(gateway['Image'].startswith('envoyproxy/envoy:'))
                self.assertEqual(gateway['EntryPoint'], ['/bin/sh', '-ec'])
                self.assertEqual(gateway['Command'], [(ROOT/'src/gateway-entrypoint.sh').read_text()])

    def test_updates_and_empty_change_sets_preserve_protection_and_reuse_artifacts(self):
        for scenario in ['cached', 'empty']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.count('Reusing artifact:'), 2)
                self.assertIn('Stack deployment complete', result.stdout)
                policy_call = next(c for c in calls if c[:3] == ['aws', 'cloudformation', 'set-stack-policy'])
                policy = json.loads(policy_call[policy_call.index('--stack-policy-body')+1])
                denies = {s['Resource']: s['Action'] for s in policy['Statement'] if s['Effect'] == 'Deny'}
                self.assertEqual(denies['LogicalResourceId/Database'], ['Update:Replace', 'Update:Delete'])
                self.assertEqual(denies['LogicalResourceId/Bucket'], 'Update:Delete')
                deployment = next(c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy'])
                self.assertNotIn('--role-arn', deployment)
                self.assertNotIn('--no-execute-changeset', deployment)
                self.assertIn('--no-fail-on-empty-changeset', deployment)
                self.assertLess(calls.index(policy_call), calls.index(deployment))
                if scenario == 'empty':
                    self.assertIn('No changes to deploy', result.stdout)

    def test_invalid_inputs_and_stack_states_stop_before_uploads(self):
        result, calls = self.run_deploy('new', parameters={'SiteURL': 'https://example.com'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Unknown template parameter', result.stderr)
        self.assertFalse(calls)
        for status in ['UPDATE_IN_PROGRESS', 'DELETE_FAILED', 'ROLLBACK_COMPLETE']:
            result, calls = self.run_deploy('status:'+status)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(status, result.stderr)
            self.assertEqual([c[:3] for c in calls], [['aws', 'cloudformation', 'describe-stacks']])

    def test_missing_policy_still_installs_data_protection(self):
        for scenario in ['no-policy', 'null-policy']:
            result, calls = self.run_deploy(scenario)
            self.assertEqual(result.returncode, 0, result.stderr)
            policy = next(i for i, c in enumerate(calls) if c[:3] == ['aws', 'cloudformation', 'set-stack-policy'])
            deploy = next(i for i, c in enumerate(calls) if c[:3] == ['aws', 'cloudformation', 'deploy'])
            self.assertLess(policy, deploy)

    def test_access_error_is_not_treated_as_a_new_stack(self):
        result, calls = self.run_deploy('denied')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AccessDenied', result.stderr)
        self.assertEqual(len(calls), 1)

    def test_failed_package_or_upload_does_not_deploy(self):
        for scenario in ['package-fails', 'upload-denied']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'deploy'] for c in calls))
                self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'set-stack-policy'] for c in calls))

    def test_policy_failure_prevents_existing_stack_update(self):
        for scenario in ['policy-read-denied', 'policy-write-denied']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'deploy'] for c in calls))

    def test_failed_creation_does_not_report_success_or_install_policy(self):
        result, calls = self.run_deploy('deploy-fails')
        self.assertNotEqual(result.returncode, 0)
        self.assertNotIn('Stack deployment complete', result.stdout)
        self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'set-stack-policy'] for c in calls))

    def test_invalid_parameters_fail_before_external_calls(self):
        for parameters in [{'ArtifactsBucket': 'override'}, {'BootstrapKey': 'override'},
                           {'CertificateKey': 'override'}, {'SiteUrl': 'line\nbreak'},
                           {'SiteUrl': {'nested': 'value'}}, []]:
            with self.subTest(parameters=parameters):
                result, calls = self.run_deploy('new', parameters=parameters)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_obsolete_modes_fail_before_external_calls(self):
        for args in [('--apply',), ('--change-set', 'saved-plan')]:
            with self.subTest(args=args):
                result, calls = self.run_deploy('new', extra_args=args)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(calls, [])

    def test_copied_source_preserves_rendering_and_artifact_content(self):
        with tempfile.TemporaryDirectory() as directory:
            copied = Path(directory)/'embedded infra'/'supabase'
            shutil.copytree(ROOT/'src', copied)
            expected = subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'])
            rendered = subprocess.check_output([str(copied/'deploy.sh'), '--template'], cwd=directory)
            self.assertEqual(rendered, expected)
            result, calls = self.run_deploy('new')
            self.assertEqual(result.returncode, 0, result.stderr)
            deployment = next(c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy'])
            key = next(c for c in deployment if c.startswith('BootstrapKey='))
            artifacts = self.artifacts
            self.assertEqual(artifacts['rds-ca.pem'], (ROOT/'src/rds-ca.pem').read_bytes())
            result, calls = self.run_deploy('new', source=copied)
            self.assertEqual(result.returncode, 0, result.stderr)
            deployment = next(c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy'])
            self.assertIn(key, deployment)
            self.assertEqual(self.artifacts, artifacts)
            self.assertEqual(self.template, expected.decode())


class ArtifactTest(unittest.TestCase):
    def test_storage_history_expiry_is_opt_in_and_preserves_current_objects(self):
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
        self.assertFalse(errors)
        parameter = 'StorageNoncurrentVersionRetentionDays'
        self.assertEqual(template['Parameters'][parameter]['Default'], 0)
        bucket = template['Resources']['Bucket']
        self.assertEqual(bucket['Properties']['VersioningConfiguration']['Status'], 'Enabled')
        self.assertEqual(bucket['DeletionPolicy'], 'Retain')
        self.assertEqual(bucket['UpdateReplacePolicy'], 'Retain')
        rules = bucket['Properties']['LifecycleConfiguration']['Rules']
        self.assertEqual(next(r for r in rules if r.get('Id') == 'AbortIncompleteUploads')['AbortIncompleteMultipartUpload'],
                         {'DaysAfterInitiation': 7})
        condition, rule, absent = next(r['Fn::If'] for r in rules if 'Fn::If' in r)
        self.assertEqual(template['Conditions'][condition], {'Fn::Not': [{'Fn::Equals': [{'Ref': parameter}, 0]}]})
        self.assertEqual(absent, {'Ref': 'AWS::NoValue'})
        self.assertEqual(rule['NoncurrentVersionExpiration'], {'NoncurrentDays': {'Ref': parameter}})
        self.assertEqual(rule['Status'], 'Enabled')
        for candidate in [rule, *(r for r in rules if 'Fn::If' not in r)]:
            self.assertFalse({'ExpirationInDays', 'ExpirationDate', 'ExpiredObjectDeleteMarker'} & candidate.keys())
        statements = template['Resources']['StorageRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        self.assertNotIn('s3:DeleteObjectVersion', str(statements))

    def test_service_log_retention_is_opt_in_and_cannot_delete_log_groups(self):
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
        self.assertFalse(errors)
        self.assertEqual(template['Parameters']['ServiceLogRetentionDays']['Default'], 0)
        resource = template['Resources']['ServiceLogRetention']
        self.assertEqual(template['Conditions'][resource['Condition']],
                         {'Fn::Not': [{'Fn::Equals': [{'Ref': 'ServiceLogRetentionDays'}, 0]}]})
        statements = template['Resources']['BootstrapRole']['Properties']['Policies'][0]['PolicyDocument']['Statement']
        condition, policy, absent = next(s['Fn::If'] for s in statements if s.get('Fn::If', [None])[0] == resource['Condition'])
        self.assertEqual(condition, resource['Condition'])
        self.assertEqual(absent, {'Ref': 'AWS::NoValue'})
        self.assertEqual(set(policy['Action']), {'logs:CreateLogGroup', 'logs:PutRetentionPolicy'})
        self.assertEqual({r['Fn::Sub'] for r in policy['Resource']}, {
            'arn:${AWS::Partition}:logs:${AWS::Region}:${AWS::AccountId}:log-group:'+name+':*'
            for name in ['/aws/rds/instance/${Database}/postgresql', '/aws/rds/instance/${Database}/upgrade',
                         '/aws/ecs/containerinsights/${Cluster}/performance']})

    def test_private_api_requires_explicit_clients_and_gates_public_ingress(self):
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
        self.assertFalse(errors)
        resources = template['Resources']
        self.assertEqual(template['Parameters']['ApiExposure']['Default'], 'internal')
        self.assertEqual(template['Parameters']['ManageApiIngress']['Default'], 'true')
        self.assertEqual(template['Parameters']['ManageApiIngress']['AllowedValues'], ['true', 'false'])
        self.assertEqual(resources['ApiAlb']['Properties']['Scheme'], {'Ref': 'ApiExposure'})
        public_condition = resources['PublicHttps']['Condition']
        self.assertEqual(template['Conditions'][public_condition], {'Fn::Equals': [{'Ref': 'ApiExposure'}, 'internet-facing']})
        self.assertEqual(resources['ApiAlb']['Properties']['Subnets'],
                         {'Fn::If': [public_condition, {'Ref': 'PublicSubnetIds'}, {'Ref': 'PrivateSubnetIds'}]})
        private = resources['PrivateApiHttps']
        self.assertEqual(template['Conditions'][private['Condition']], {'Fn::And': [
            {'Condition': 'HasPrivateApi'},
            {'Fn::Equals': [{'Ref': 'ManageApiIngress'}, 'true']}]})
        self.assertEqual(private['Properties']['SourceSecurityGroupId'], {'Ref': 'ApiClientSecurityGroupId'})
        self.assertNotIn('CidrIp', private['Properties'])
        self.assertEqual((private['Properties']['FromPort'], private['Properties']['ToPort']), (443, 443))
        self.assertNotIn('SecurityGroupIngress', resources['ApiAlbSG']['Properties'])
        api_ingress = {name for name, resource in resources.items() if resource['Type'] == 'AWS::EC2::SecurityGroupIngress'
                       and resource['Properties']['GroupId'] == {'Ref': 'ApiAlbSG'}}
        self.assertEqual(api_ingress, {'PublicHttps'})
        rule = template['Rules']['PrivateApiRequiresClients']
        self.assertEqual(rule['RuleCondition']['Fn::And'][0], {'Fn::Or': [
            {'Fn::Equals': [{'Ref': 'ApiExposure'}, 'internal']},
            {'Fn::Equals': [{'Ref': 'EnablePrivateApi'}, 'true']}]})
        self.assertEqual(rule['Assertions'][0]['Assert'], {'Fn::Not': [{'Fn::Equals': [{'Ref': 'ApiClientSecurityGroupId'}, '']}]})
        public_rule = template['Rules']['PublicApiRequiresSubnets']
        self.assertEqual(public_rule['RuleCondition'], template['Conditions'][public_condition])
        self.assertEqual(public_rule['Assertions'][0]['Assert'], {'Fn::Not': [{'Fn::Contains': [{'Ref': 'PublicSubnetIds'}, '']}]})
        outputs = template['Outputs']
        for name, resource in [('ApiSecurityGroupId', 'ApiAlbSG'), ('GatewaySecurityGroupId', 'GatewaySG'),
                               ('DatabaseSecurityGroupId', 'DatabaseSG')]:
            self.assertEqual(outputs[name]['Value'], {'Ref': resource})
        self.assertEqual(outputs['DatabasePort']['Value'], {'Fn::GetAtt': ['Database', 'Endpoint.Port']})
        self.assertEqual(outputs['ServiceConnectNamespaceArn'],
                         {'Value': {'Fn::GetAtt': ['TlsNamespace', 'Arn']}, 'Condition': 'UseServiceConnectTls'})
        self.assertFalse(any('Export' in output for output in outputs.values()))

    def test_optional_private_api_preserves_single_endpoint_defaults(self):
        document, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True))
        self.assertFalse(errors)
        self.assertEqual(document['Parameters']['PublicApiEnabled']['Default'], 'true')
        for exposure, additional in [('internal', 'false'), ('internet-facing', 'false'), ('internet-facing', 'true')]:
            for encryption in ['vpc', 'service-connect-tls']:
                for managed, enabled in [('true', 'true'), ('true', 'false'), ('false', 'true'), ('false', 'false')]:
                    resources = configured(document, {'ApiExposure': exposure, 'EnablePrivateApi': additional,
                        'PublicApiEnabled': enabled,
                        'InternalTrafficEncryption': encryption, 'ManageApiIngress': managed,
                        'ApiClientSecurityGroupId': 'sg-client', 'PrivateHostedZoneId': 'private-zone' if additional == 'true' else ''})
                    dual = additional == 'true'
                    self.assertEqual('PrivateApiAlb' in resources, dual)
                    self.assertEqual('PrivateApiDns' in resources, dual)
                    self.assertEqual('PrivateApiErrorsAlarm' in resources, dual)
                    self.assertEqual('PublicHttps' in resources, exposure == 'internet-facing')
                    self.assertEqual('PrivateApiHttps' in resources, managed == 'true' and (dual or exposure == 'internal'))
                    gateway = resources['GatewayService']['Properties']
                    forwarding = exposure == 'internal' or enabled == 'true'
                    action = resources['ApiListener']['Properties']['DefaultActions'][0]
                    self.assertEqual(action['Type'], 'forward' if forwarding else 'fixed-response')
                    if not forwarding:
                        self.assertEqual(action['FixedResponseConfig']['StatusCode'], '503')
                        self.assertNotIn('TargetGroupArn', action)
                    targets = [item['TargetGroupArn'] for item in gateway['LoadBalancers']]
                    self.assertEqual(targets, ([{'Ref': 'GatewayTargets'}] if forwarding else []) +
                                     ([{'Ref': 'PrivateGatewayTargets'}] if dual else []))
                    if dual:
                        self.assertEqual(resources['PrivateApiListener']['Properties']['DefaultActions'],
                                         [{'Type': 'forward', 'TargetGroupArn': {'Ref': 'PrivateGatewayTargets'}}])
                        self.assertIn({'Key': 'PrivateApiListener', 'Value': {'Ref': 'PrivateApiListener'}}, gateway['Tags'])
                        self.assertEqual(resources['PrivateApiAlb']['Properties']['Scheme'], 'internal')
                        self.assertEqual(resources['PrivateApiListener']['Properties']['Port'], 443)
                        self.assertEqual(resources['PrivateGatewayTargets']['Properties']['Protocol'], 'HTTPS' if encryption == 'service-connect-tls' else 'HTTP')
                        if managed == 'true':
                            ingress = resources['PrivateApiHttps']['Properties']
                            self.assertEqual(ingress['GroupId'], {'Ref': 'PrivateApiAlbSG'})
                            self.assertEqual(ingress['SourceSecurityGroupId'], 'sg-client')
                            self.assertNotIn('CidrIp', ingress)
        self.assertNotIn('PrivateApiListener', document['Resources']['GatewayService']['DependsOn'])

    def test_auth_delivery_modes_keep_secrets_and_permissions_separate(self):
        document, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True))
        self.assertFalse(errors)
        for mode in ['smtp', 'hook']:
            with self.subTest(mode=mode):
                resources = configured(document, {'EmailMode': mode, 'SesSmtpSecretArn': 'smtp-secret',
                    'EmailHookSecretArn': 'hook-secret', 'SesSmtpKmsKeyArn': 'smtp-key', 'EmailHookKmsKeyArn': 'hook-key'})
                auth = next(c for c in resources['AuthTask']['Properties']['ContainerDefinitions'] if c['Name'] == 'auth')
                env = {item['Name']: item['Value'] for item in auth['Environment']}
                secrets = {item['Name']: item['ValueFrom'] for item in auth['Secrets']}
                self.assertEqual(env['GOTRUE_HOOK_SEND_EMAIL_ENABLED'], 'true' if mode == 'hook' else 'false')
                self.assertEqual('GOTRUE_SMTP_USER' in secrets, mode == 'smtp')
                self.assertEqual('GOTRUE_HOOK_SEND_EMAIL_SECRETS' in secrets, mode == 'hook')
                self.assertEqual('AuthSmtp' in resources, mode == 'smtp')
                permissions = json.dumps(resources['AuthExecutionRole'])
                self.assertIn(mode+'-secret', permissions)
                self.assertIn(mode+'-key', permissions)
                self.assertNotIn(('hook' if mode == 'smtp' else 'smtp')+'-secret', permissions)
                for key in ['GOTRUE_EXTERNAL_PHONE_ENABLED', 'GOTRUE_SMS_AUTOCONFIRM', 'GOTRUE_MAILER_AUTOCONFIRM']:
                    self.assertEqual(env[key], 'false')
        resources = configured(document, dict.fromkeys(['EnablePhoneSignup', 'EnablePhoneAutoconfirm', 'EnableEmailAutoconfirm'], 'true'))
        auth = next(c for c in resources['AuthTask']['Properties']['ContainerDefinitions'] if c['Name'] == 'auth')
        env = {item['Name']: item['Value'] for item in auth['Environment']}
        for key in ['GOTRUE_EXTERNAL_PHONE_ENABLED', 'GOTRUE_SMS_AUTOCONFIRM', 'GOTRUE_MAILER_AUTOCONFIRM']:
            self.assertEqual(env[key], 'true')

    def test_internal_encryption_has_two_protected_modes_and_gates_all_services(self):
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
        self.assertFalse(errors)
        parameters, resources = template['Parameters'], template['Resources']
        self.assertEqual(parameters['InternalTrafficEncryption']['Default'], 'vpc')
        self.assertEqual(parameters['InternalTrafficEncryption']['AllowedValues'], ['vpc', 'service-connect-tls'])
        for condition, mode in [('UseVpcEncryption', 'vpc'), ('UseServiceConnectTls', 'service-connect-tls')]:
            self.assertEqual(template['Conditions'][condition], {'Fn::Equals': [{'Ref': 'InternalTrafficEncryption'}, mode]})
        self.assertEqual(resources['EncryptionCheck']['Properties']['Mode'], {'Ref': 'InternalTrafficEncryption'})
        self.assertEqual(resources['EncryptionCheck']['Properties']['VpcId'], {'Ref': 'VpcId'})
        self.assertIn('EncryptionCheck', resources['Bootstrap']['DependsOn'])
        self.assertIn('ServiceConnectRequiresPrivateCa', template['Rules'])
        self.assertEqual(resources['ServiceConnectRole']['Condition'], 'UseServiceConnectTls')
        role = resources['ServiceConnectRole']['Properties']
        self.assertEqual(role['AssumeRolePolicyDocument']['Statement'][0]['Principal'], {'Service': 'ecs.amazonaws.com'})
        self.assertEqual(role['Policies']['Fn::If'][0], 'HasServiceConnectKmsKey')
        for name, resource in resources.items():
            if resource['Type'] != 'AWS::ECS::Service':
                continue
            with self.subTest(service=name):
                self.assertIn('EncryptionCheck', resource['DependsOn'])
                mode, config, disabled = resource['Properties']['ServiceConnectConfiguration']['Fn::If']
                self.assertEqual(mode, 'UseServiceConnectTls')
                self.assertEqual(disabled, {'Enabled': False})
                self.assertTrue(config['Enabled'])
                self.assertEqual(config['Namespace'], {'Fn::GetAtt': ['TlsNamespace', 'Arn']})
                endpoint, = config['Services']
                task = resources[name.replace('Service', 'Task')]['Properties']
                port, = [p for c in task['ContainerDefinitions'] for p in c.get('PortMappings', []) if p['Name'] == endpoint['PortName']]
                self.assertEqual(endpoint['ClientAliases'][0]['Port'], port['ContainerPort'])
                self.assertEqual(port['AppProtocol'], 'http')
                self.assertNotIn('IngressPortOverride', endpoint)
                self.assertEqual(endpoint['Timeout'], {'IdleTimeoutSeconds': 0, 'PerRequestTimeoutSeconds': 0})
                self.assertEqual(endpoint['Tls']['IssuerCertificateAuthority'], {'AwsPcaAuthorityArn': {'Ref': 'ServiceConnectPrivateCaArn'}})
                self.assertEqual(endpoint['Tls']['RoleArn'], {'Fn::GetAtt': ['ServiceConnectRole', 'Arn']})
                if name != 'AdminService':
                    self.assertEqual(resource['Properties']['ServiceRegistries']['Fn::If'][2], [])
                    self.assertEqual(resources[name.replace('Service', 'Discovery')]['Condition'], 'UseVpcEncryption')
        for name in ['GatewayTargets', 'AdminTargets']:
            for field in ['Protocol', 'HealthCheckProtocol']:
                self.assertEqual(resources[name]['Properties'][field], {'Fn::If': ['UseServiceConnectTls', 'HTTPS', 'HTTP']})
            self.assertEqual(resources[name]['Properties']['HealthCheckPort'], 'traffic-port')

    def test_existing_credentials_are_optional_and_source_permissions_are_read_only(self):
        document, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True))
        self.assertFalse(errors)
        for supplied in [False, True]:
            overrides = {'EnableStudio': 'true'}
            if supplied:
                overrides.update(ExistingDatabaseSecretArn='arn:aws:secretsmanager:us-east-1:123456789012:secret:db',
                    ExistingDatabaseSecretKmsKeyArn='arn:aws:kms:us-east-1:123456789012:key/db',
                    LegacyApiKeysSecretArn='arn:aws:secretsmanager:us-east-1:123456789012:secret:legacy',
                    LegacyApiKeysKmsKeyArn='arn:aws:kms:us-east-1:123456789012:key/legacy')
            resources = configured(document, overrides)
            self.assertEqual('DatabaseSecret' in resources, not supplied)
            expected = overrides.get('ExistingDatabaseSecretArn', {'Ref': 'DatabaseSecret'})
            self.assertEqual(resources['Bootstrap']['Properties']['DatabaseSecret'], expected)
            self.assertEqual(resources['Database']['Properties']['MasterUserPassword']['Fn::Sub'][1]['SecretArn'], expected)
            self.assertEqual(resources['Database']['Properties']['MasterUsername'], 'postgres')
            for container in resources['AdminTask']['Properties']['ContainerDefinitions']:
                for secret in container.get('Secrets', []):
                    if secret['Name'] in ['PG_META_DB_PASSWORD', 'POSTGRES_PASSWORD']:
                        self.assertEqual(secret['ValueFrom']['Fn::Sub'][1]['SecretArn'], expected)
            for role in ['BootstrapRole', 'AdminExecutionRole']:
                statements = resources[role]['Properties']['Policies'][0]['PolicyDocument']['Statement']
                decrypt = [entry for entry in statements if entry['Action'] == 'kms:Decrypt']
                self.assertEqual(len(decrypt), (2 if role == 'BootstrapRole' else 1) if supplied else 0)
                if supplied:
                    for statement in statements:
                        if 'secretsmanager:PutSecretValue' in statement['Action']:
                            self.assertNotIn(overrides['ExistingDatabaseSecretArn'], statement['Resource'])
                            self.assertNotIn(overrides['LegacyApiKeysSecretArn'], statement['Resource'])
                    for statement in decrypt:
                        self.assertIn(statement['Condition']['StringEquals']['kms:EncryptionContext:SecretARN'],
                            [overrides['ExistingDatabaseSecretArn'], overrides['LegacyApiKeysSecretArn']])

    def test_credential_refresh_reapplies_bootstrap_before_replacing_tasks(self):
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
        self.assertFalse(errors)
        resources = template['Resources']
        revision = resources['Bootstrap']['Properties']['CredentialRevision']
        self.assertEqual(template['Parameters'][revision['Ref']]['Default'], 'initial')
        for name, resource in resources.items():
            if resource['Type'] == 'AWS::ECS::Service':
                with self.subTest(service=name):
                    self.assertIn('Bootstrap', resource['DependsOn'])
                    self.assertEqual(resource['Properties']['ForceNewDeployment'], {
                        'EnableForceNewDeployment': True, 'ForceNewDeploymentNonce': revision})
        self.assertNotIn(revision['Ref'], json.dumps(resources['Database']))

    def test_rendered_template_and_all_tasks_fit_size_budgets(self):
        rendered = subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], timeout=10)
        self.assertLess(len(rendered), 1024*1024)
        self.assertNotIn(b'@include', rendered)
        template, errors = decode_str(rendered.decode())
        self.assertFalse(errors)
        self.assertEqual(template['Parameters']['EcsCpuArchitecture']['Default'], 'X86_64')
        self.assertEqual(template['Parameters']['EcsCpuArchitecture']['AllowedValues'], ['X86_64', 'ARM64'])

        def sized(value):
            # Estimate resolved names/ARNs with 1 KiB per substitution, plus 4 KiB task headroom.
            if isinstance(value, list):
                return [sized(item) for item in value]
            if isinstance(value, dict):
                if 'Ref' in value or 'Fn::GetAtt' in value:
                    return 'x'*1024
                if 'Fn::Sub' in value:
                    return re.sub(r'\$\{[^}]+\}', 'x'*1024, value['Fn::Sub'][0] if isinstance(value['Fn::Sub'], list) else value['Fn::Sub'])
                return {key: sized(item) for key, item in value.items()}
            return value

        for name, resource in template['Resources'].items():
            if resource['Type'] != 'AWS::ECS::TaskDefinition':
                continue
            task = resource['Properties']
            with self.subTest(task=name):
                self.assertEqual(task['RuntimePlatform']['CpuArchitecture'], {'Ref': 'EcsCpuArchitecture'})
                self.assertLess(len(json.dumps(sized(task)).encode()), 60*1024)
                for container in task['ContainerDefinitions']:
                    self.assertRegex(container['Image'], r'^[^@]+:[^:@]+@sha256:[a-f0-9]{64}$')
                    self.assertNotIn(':latest@', container['Image'])


class ReleaseTest(unittest.TestCase):
    @unittest.skipUnless(os.environ.get('CHECK_SUPABASE_RELEASE') == '1', 'upstream check runs on the weekly schedule')
    def test_images_match_latest_stable_self_hosted_release(self):
        def fetch(url):
            with urllib.request.urlopen(url, timeout=30) as response:
                return response.read()

        refs = json.loads(fetch('https://api.github.com/repos/supabase/supabase/git/matching-refs/tags/self-hosted/v'))
        releases = [match[1] for ref in refs if (match := re.fullmatch(r'refs/tags/(self-hosted/v\d+\.\d+\.\d+)', ref['ref']))]
        latest = max(releases, key=lambda tag: tuple(map(int, tag.split('/v')[1].split('.'))))
        selected = re.search(r'self-hosted/v\d+\.\d+\.\d+', (ROOT/'NOTICE').read_text())[0]
        self.assertEqual(selected, latest, 'New self-hosted release requires review; do not update images independently')
        compose = yaml.safe_load(fetch(f'https://raw.githubusercontent.com/supabase/supabase/{selected}/docker/docker-compose.yml'))
        upstream = {service['image'].split(':')[0]: service['image'] for service in compose['services'].values()}
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
        self.assertFalse(errors)
        checked = set()
        for resource in template['Resources'].values():
            if resource['Type'] != 'AWS::ECS::TaskDefinition':
                continue
            for container in resource['Properties']['ContainerDefinitions']:
                tag = container['Image'].split('@')[0]
                repository = tag.split(':')[0]
                if repository in upstream:
                    self.assertEqual(tag, upstream[repository])
                    checked.add(repository)
        self.assertEqual(checked, {'envoyproxy/envoy', 'supabase/gotrue', 'postgrest/postgrest',
                                   'supabase/storage-api', 'supabase/postgres-meta', 'supabase/studio'})


if __name__ == '__main__':
    unittest.main()
