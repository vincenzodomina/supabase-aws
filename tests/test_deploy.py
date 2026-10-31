import json
import os
import re
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
    status = 'REVIEW_IN_PROGRESS' if scenario == 'review' else 'CREATE_COMPLETE'
    print(json.dumps({'Stacks': [{'StackStatus': status}]}))
elif command == ['cloudformation', 'deploy']:
    if scenario == 'deploy-fails': sys.exit('CloudFormation deployment failed')
    (root/'template').write_text(pathlib.Path(args[args.index('--template-file')+1]).read_text())
    if '--no-execute-changeset' not in args: (root/'created').touch()
elif command == ['cloudformation', 'get-stack-policy']:
    if scenario == 'policy-read-denied': sys.exit('AccessDenied: cannot read policy')
    if scenario in ['new', 'review']:
        print('{}'); sys.exit(0)
    print(json.dumps({'StackPolicyBody': json.dumps({'Statement': [
        {'Effect': 'Allow', 'Action': 'Update:*', 'Principal': '*', 'Resource': '*'},
        {'Effect': 'Deny', 'Action': 'Update:Delete', 'Principal': '*', 'Resource': 'LogicalResourceId/Bucket'}]})}))
elif command == ['cloudformation', 'set-stack-policy']:
    if scenario == 'policy-write-denied': sys.exit('AccessDenied: cannot set policy')
elif command == ['s3api', 'put-object']:
    if scenario == 'cached': sys.exit('PreconditionFailed: object exists')
    if scenario == 'upload-denied': sys.exit('AccessDenied: cannot write artifact')
else: sys.exit('Unexpected CLI call: '+repr(args))
'''


class DeploymentTest(unittest.TestCase):
    def run_deploy(self, scenario, apply=False, parameters=None, role_arn=None):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for tool in ['aws', 'python3']:
                path = root/tool
                path.write_text('#!'+sys.executable+'\n'+CLI)
                path.chmod(0o755)
            params = root/'parameters.json'
            params.write_text(json.dumps(parameters if parameters is not None else {'SiteUrl': 'https://example.com/a b'}))
            args = ['bash', str(ROOT/'src/deploy.sh'), '--stack-name', 'demo', '--region', 'us-east-1',
                    '--parameters', params.name, '--artifacts-bucket', 'artifacts']
            if apply:
                args.append('--apply')
            if role_arn is not None:
                args.extend(['--role-arn', role_arn])
            result = subprocess.run(args, capture_output=True, text=True, cwd=directory,
                                    env=dict(os.environ, PATH=directory+os.pathsep+os.environ['PATH'],
                                             CLI_FIXTURE=directory, SCENARIO=scenario), timeout=30)
            calls = [json.loads(line) for line in (root/'calls').read_text().splitlines()] if (root/'calls').exists() else []
            self.template = (root/'template').read_text() if (root/'template').exists() else None
            return result, calls

    def test_plan_uploads_artifacts_without_executing_or_changing_policy(self):
        role = 'arn:aws:iam::123456789012:role/platform/cloudformation'
        result, calls = self.run_deploy('new', role_arn=role)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([c[:3] for c in calls if c[0] == 'aws'], [
            ['aws', 'cloudformation', 'describe-stacks'], ['aws', 's3api', 'put-object'],
            ['aws', 's3api', 'put-object'], ['aws', 'cloudformation', 'deploy']])
        self.assertIn('--no-execute-changeset', calls[-1])
        self.assertEqual(calls[-1][calls[-1].index('--role-arn')+1], role)
        self.assertFalse(any('--role-arn' in c for c in calls[:-1]))
        self.assertIn('ArtifactsBucket=artifacts', calls[-1])
        self.assertTrue(any(c.startswith('BootstrapKey=supabase-aws/bootstrap/') for c in calls[-1]))
        self.assertTrue(any(c.startswith('CertificateKey=supabase-aws/certificates/') for c in calls[-1]))
        self.assertIn('SiteUrl=https://example.com/a b', calls[-1])
        template, errors = decode_str(self.template)
        self.assertFalse(errors)
        task = template['Resources']['GatewayTask']['Properties']
        gateway = task['ContainerDefinitions'][0]
        self.assertTrue(gateway['Image'].startswith('envoyproxy/envoy:'))
        self.assertEqual(gateway['EntryPoint'], ['/bin/sh', '-ec'])
        self.assertEqual(gateway['Command'], [(ROOT/'src/gateway-entrypoint.sh').read_text()])
        self.assertIn('--s3-bucket', calls[-1])

    def test_first_apply_deploys_once_then_protects_database(self):
        role = 'arn:aws:iam::123456789012:role/platform/cloudformation'
        for scenario in ['new', 'review']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario, apply=True, role_arn=role)
                self.assertEqual(result.returncode, 0, result.stderr)
                deployments = [c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy']]
                self.assertEqual(len(deployments), 1)
                self.assertNotIn('--no-execute-changeset', deployments[0])
                self.assertEqual(deployments[0][deployments[0].index('--role-arn')+1], role)
                self.assertFalse(any('--role-arn' in c for c in calls if c != deployments[0]))
                uploads = [i for i, c in enumerate(calls) if c[:3] == ['aws', 's3api', 'put-object']]
                policy_index = next(i for i, c in enumerate(calls) if c[:3] == ['aws', 'cloudformation', 'set-stack-policy'])
                self.assertLess(max(uploads), calls.index(deployments[0]))
                self.assertLess(calls.index(deployments[0]), policy_index)

    def test_update_reuses_artifacts_and_preserves_existing_protection(self):
        result, calls = self.run_deploy('cached', apply=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count('Reusing artifact:'), 2)
        policy_call = next(c for c in calls if c[:3] == ['aws', 'cloudformation', 'set-stack-policy'])
        policy = json.loads(policy_call[policy_call.index('--stack-policy-body')+1])
        denies = {s['Resource']: s['Action'] for s in policy['Statement'] if s['Effect'] == 'Deny'}
        self.assertEqual(denies['LogicalResourceId/Database'], ['Update:Replace', 'Update:Delete'])
        self.assertEqual(denies['LogicalResourceId/Bucket'], 'Update:Delete')
        deployment = next(c for c in calls if c[:3] == ['aws', 'cloudformation', 'deploy'])
        self.assertNotIn('--role-arn', deployment)
        self.assertLess(calls.index(policy_call), calls.index(deployment))

    def test_access_error_is_not_treated_as_a_new_stack(self):
        result, calls = self.run_deploy('denied')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('AccessDenied', result.stderr)
        self.assertEqual(len(calls), 1)

    def test_failed_package_or_upload_does_not_deploy(self):
        for scenario in ['package-fails', 'upload-denied']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario, apply=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'deploy'] for c in calls))
                self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'set-stack-policy'] for c in calls))

    def test_policy_failure_prevents_existing_stack_update(self):
        for scenario in ['policy-read-denied', 'policy-write-denied']:
            with self.subTest(scenario=scenario):
                result, calls = self.run_deploy(scenario, apply=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertFalse(any(c[:3] == ['aws', 'cloudformation', 'deploy'] for c in calls))

    def test_failed_creation_does_not_report_success_or_install_policy(self):
        result, calls = self.run_deploy('deploy-fails', apply=True)
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

    def test_unchanged_package_reuses_content_address(self):
        keys = []
        for _ in range(2):
            result, calls = self.run_deploy('new')
            self.assertEqual(result.returncode, 0, result.stderr)
            keys.append(next(c for c in calls[-1] if c.startswith('BootstrapKey=')))
        self.assertEqual(keys[0], keys[1])


class ArtifactTest(unittest.TestCase):
    def test_private_api_requires_explicit_clients_and_gates_public_ingress(self):
        template, errors = decode_str(subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], text=True, timeout=10))
        self.assertFalse(errors)
        resources = template['Resources']
        self.assertEqual(template['Parameters']['ApiExposure']['Default'], 'internal')
        self.assertEqual(resources['ApiAlb']['Properties']['Scheme'], {'Ref': 'ApiExposure'})
        public_condition = resources['PublicHttps']['Condition']
        self.assertEqual(template['Conditions'][public_condition], {'Fn::Equals': [{'Ref': 'ApiExposure'}, 'internet-facing']})
        self.assertEqual(resources['ApiAlb']['Properties']['Subnets'],
                         {'Fn::If': [public_condition, {'Ref': 'PublicSubnetIds'}, {'Ref': 'PrivateSubnetIds'}]})
        private = resources['PrivateApiHttps']
        self.assertEqual(template['Conditions'][private['Condition']], {'Fn::Equals': [{'Ref': 'ApiExposure'}, 'internal']})
        self.assertEqual(private['Properties']['SourceSecurityGroupId'], {'Ref': 'ApiClientSecurityGroupId'})
        self.assertNotIn('CidrIp', private['Properties'])
        rule = template['Rules']['PrivateApiRequiresClients']
        self.assertEqual(rule['RuleCondition'], template['Conditions'][private['Condition']])
        self.assertEqual(rule['Assertions'][0]['Assert'], {'Fn::Not': [{'Fn::Equals': [{'Ref': 'ApiClientSecurityGroupId'}, '']}]})
        public_rule = template['Rules']['PublicApiRequiresSubnets']
        self.assertEqual(public_rule['RuleCondition'], template['Conditions'][public_condition])
        self.assertEqual(public_rule['Assertions'][0]['Assert'], {'Fn::Not': [{'Fn::Contains': [{'Ref': 'PublicSubnetIds'}, '']}]})

    def test_rendered_template_and_all_tasks_fit_size_budgets(self):
        rendered = subprocess.check_output([str(ROOT/'src/deploy.sh'), '--template'], timeout=10)
        self.assertLess(len(rendered), 1024*1024)
        self.assertNotIn(b'@include', rendered)
        template, errors = decode_str(rendered.decode())
        self.assertFalse(errors)

        def sized(value):
            # Estimate resolved names/ARNs with 1 KiB per substitution, plus 4 KiB task headroom.
            if isinstance(value, list):
                return [sized(item) for item in value]
            if isinstance(value, dict):
                if 'Ref' in value or 'Fn::GetAtt' in value:
                    return 'x'*1024
                if 'Fn::Sub' in value:
                    return re.sub(r'\$\{[^}]+\}', 'x'*1024, value['Fn::Sub'])
                return {key: sized(item) for key, item in value.items()}
            return value

        for name, resource in template['Resources'].items():
            if resource['Type'] != 'AWS::ECS::TaskDefinition':
                continue
            task = resource['Properties']
            with self.subTest(task=name):
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
