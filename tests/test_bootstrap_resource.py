import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

import boto3
from botocore.stub import Stubber

spec = importlib.util.spec_from_file_location('bootstrap', Path(__file__).resolve().parents[1]/'src/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.event = {'RequestType': 'Create', 'ResourceType': 'Custom::DatabaseBootstrap', 'StackId': 'stack', 'RequestId': 'request',
                      'LogicalResourceId': 'Bootstrap', 'ResponseURL': 'https://response.invalid',
                      'ResourceProperties': {}}
        self.context = Mock()
        self.context.get_remaining_time_in_millis.return_value = 900000

    def invoke(self, work):
        with patch.object(bootstrap, 'bootstrap', work), patch.object(bootstrap.urllib.request, 'urlopen') as response:
            bootstrap.handler(self.event, self.context)
        request = response.call_args.args[0]
        self.assertEqual(request.method, 'PUT')
        self.assertEqual(request.full_url, self.event['ResponseURL'])
        self.assertLessEqual(len(request.data), 4096)
        payload = json.loads(request.data)
        for field in ['StackId', 'RequestId', 'LogicalResourceId']:
            self.assertEqual(payload[field], self.event[field])
        return payload

    def test_failure_is_reported_without_credentials(self):
        payload = self.invoke(Mock(side_effect=RuntimeError('password=do-not-disclose')))
        self.assertEqual(payload['Status'], 'FAILED')
        self.assertNotIn('do-not-disclose', json.dumps(payload))

    def test_delete_does_not_touch_database(self):
        self.event.update(RequestType='Delete', PhysicalResourceId='existing')
        work = Mock()
        payload = self.invoke(work)
        self.assertEqual(payload['Status'], 'SUCCESS')
        self.assertEqual(payload['PhysicalResourceId'], 'existing')
        work.assert_not_called()

    def test_deadline_reserves_time_for_cloudformation_response(self):
        self.context.get_remaining_time_in_millis.return_value = 25000
        work = Mock()
        self.assertEqual(self.invoke(work)['Status'], 'FAILED')
        work.assert_not_called()

    def test_response_delivery_failure_propagates_for_retry(self):
        with patch.object(bootstrap, 'bootstrap'), \
             patch.object(bootstrap.urllib.request, 'urlopen', side_effect=OSError('callback unavailable')):
            with self.assertRaises(OSError):
                bootstrap.handler(self.event, self.context)

    def test_interrupted_work_reports_failure(self):
        import signal
        def interrupted(properties):
            signal.raise_signal(signal.SIGALRM)
        self.assertEqual(self.invoke(interrupted)['Status'], 'FAILED')

    def test_log_retention_handles_service_creation_race_and_reports_denied_access(self):
        client = boto3.client('logs', region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test')
        properties = {'RetentionInDays': '30', 'LogGroups': ['group'], 'CreateLogGroups': ['group']}
        self.event.update(ResourceType='Custom::ServiceLogRetention', ResourceProperties=properties)
        for denied in [False, True]:
            with self.subTest(denied=denied), Stubber(client) as stub, patch.object(bootstrap.boto3, 'client', return_value=client):
                args = {'logGroupName': 'group', 'retentionInDays': 30}
                stub.add_client_error('put_retention_policy', 'ResourceNotFoundException', expected_params=args)
                stub.add_client_error('create_log_group', 'AccessDeniedException' if denied else 'ResourceAlreadyExistsException',
                                      service_message='do-not-disclose', expected_params={'logGroupName': 'group'})
                if not denied:
                    stub.add_response('put_retention_policy', {}, args)
                database_work = Mock()
                payload = self.invoke(database_work)
                self.assertEqual(payload['Status'], 'FAILED' if denied else 'SUCCESS')
                self.assertEqual(payload['PhysicalResourceId'], 'stack/service-log-retention')
                self.assertNotIn('do-not-disclose', json.dumps(payload))
                database_work.assert_not_called()
                stub.assert_no_pending_responses()

    def test_network_check_requires_available_enforcement_for_the_requested_vpc(self):
        client = boto3.client('ec2', region_name='us-east-1', aws_access_key_id='test', aws_secret_access_key='test')
        self.event.update(ResourceType='Custom::NetworkPrerequisites',
                          ResourceProperties={'Mode': 'vpc', 'VpcId': 'vpc-12345678'})
        control = {'VpcId': 'vpc-12345678', 'Mode': 'enforce', 'State': 'available'}
        cases = [('valid', [control]), ('missing', []), ('monitor', [dict(control, Mode='monitor')]),
                 ('transition', [dict(control, State='modify-in-progress')]),
                 ('wrong-vpc', [dict(control, VpcId='vpc-87654321')]), ('denied', None)]
        for name, controls in cases:
            with self.subTest(case=name), Stubber(client) as stub, patch.object(bootstrap.boto3, 'client', return_value=client):
                if controls is None:
                    stub.add_client_error('describe_vpc_encryption_controls', 'UnauthorizedOperation',
                                          expected_params={'VpcIds': ['vpc-12345678']})
                else:
                    stub.add_response('describe_vpc_encryption_controls', {'VpcEncryptionControls': controls},
                                      {'VpcIds': ['vpc-12345678']})
                database_work = Mock()
                payload = self.invoke(database_work)
                self.assertEqual(payload['Status'], 'SUCCESS' if name == 'valid' else 'FAILED')
                self.assertEqual(payload['PhysicalResourceId'], 'stack/network-prerequisites')
                database_work.assert_not_called()
                stub.assert_no_pending_responses()

    def test_tls_mode_needs_no_vpc_api_and_unknown_mode_fails(self):
        self.event['ResourceType'] = 'Custom::NetworkPrerequisites'
        for mode, expected in [('service-connect-tls', 'SUCCESS'), ('none', 'FAILED')]:
            with self.subTest(mode=mode), patch.object(bootstrap.boto3, 'client') as client:
                self.event['ResourceProperties'] = {'Mode': mode}
                database_work = Mock()
                self.assertEqual(self.invoke(database_work)['Status'], expected)
                client.assert_not_called()
                database_work.assert_not_called()

    def test_non_database_resource_deletion_does_not_call_aws(self):
        for resource_type in ['Custom::ServiceLogRetention', 'Custom::NetworkPrerequisites']:
            self.event.update(ResourceType=resource_type, RequestType='Delete', PhysicalResourceId='existing')
            with self.subTest(resource_type=resource_type), patch.object(bootstrap.boto3, 'client') as client:
                payload = self.invoke(Mock())
            self.assertEqual(payload['Status'], 'SUCCESS')
            self.assertEqual(payload['PhysicalResourceId'], 'existing')
            client.assert_not_called()


if __name__ == '__main__':
    unittest.main()
