import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

spec = importlib.util.spec_from_file_location('bootstrap', Path(__file__).resolve().parents[1]/'src/bootstrap.py')
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapLifecycleTest(unittest.TestCase):
    def setUp(self):
        self.event = {'RequestType': 'Create', 'StackId': 'stack', 'RequestId': 'request',
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


if __name__ == '__main__':
    unittest.main()
