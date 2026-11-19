import sys
from unittest.mock import MagicMock
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def control(tmp_path, monkeypatch):
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('APP_PASSWORD', 'control-test-password-1234')
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    monkeypatch.setenv('AWS_REGIONS', 'us-east-1')
    monkeypatch.setenv('AWS_EC2_METADATA_DISABLED', 'true')
    monkeypatch.delenv('AWSCC_ENCRYPTION_KEY', raising=False)
    sys.modules.pop('app', None)
    import app
    aws = MagicMock()
    aws.get_caller_identity.return_value = {'Account': '123456789012', 'Arn': 'arn:aws:iam::123456789012:user/test'}
    aws.get_instance.return_value = {'instance': {'name': 'node', 'arn': 'arn:node', 'createdAt': '2026-11-01',
          'state': {'name': 'running'}, 'publicIpAddress': '1.1.1.1', 'privateIpAddress': '10.0.0.1'}}
    aws.describe_instances.return_value = {'Reservations': [{'Instances': [{'InstanceId': 'i-0123456789abcdef0', 'State': {'Name': 'running'}, 'ImageId': 'ami-1'}]}]}
    aws.describe_instance_attribute.return_value = {'DisableApiTermination': {'Value': False}}
    aws.create_instances.return_value = {'ResponseMetadata': {'RequestId': 'create-request'}}
    monkeypatch.setattr(app, 'base_profiles', lambda: ['default'])
    monkeypatch.setattr(app, 'client', lambda *args: aws)
    monkeypatch.setattr(app, 'mutation_client', lambda *args: aws)
    c = TestClient(app.app)
    token = c.post('/api/login', json={'password': 'control-test-password-1234'}).json()['csrf']
    return app, c, {'X-AWSCC-CSRF': token}, aws
