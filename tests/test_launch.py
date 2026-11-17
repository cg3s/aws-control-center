"""Launch configuration and traffic regression tests; AWS calls are mocked."""
import base64
import json
import sys
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest
from botocore.exceptions import ClientError
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.testclient import TestClient
from passlib.hash import sha512_crypt


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_PASSWORD', 'launch-test-password-1234')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('AWS_REGIONS', 'ap-northeast-1')
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    monkeypatch.delenv('AWSCC_ENCRYPTION_KEY', raising=False)
    for module in ('app', 'taskqueue'):
        sys.modules.pop(module, None)
    import app
    import taskqueue as q
    aws = Mock()
    aws.get_bundles.return_value = {'bundles': [{'bundleId': 'micro_3_0', 'price': 7}]}
    aws.get_blueprints.return_value = {'blueprints': [{'blueprintId': 'debian_13', 'platform': 'LINUX_UNIX'}]}
    aws.get_instance.side_effect = ClientError({'Error': {'Code': 'NotFoundException'},
        'ResponseMetadata': {'HTTPStatusCode': 404}}, 'GetInstance')
    monkeypatch.setattr(q, 'valid_profile', lambda p: ('base', p))
    monkeypatch.setattr(q, 'client', lambda *args: aws)
    c = TestClient(app.app)
    csrf = c.post('/api/login', json={'password': 'launch-test-password-1234'}).json()['csrf']
    return app, q, aws, c, {'X-AWSCC-CSRF': csrf}


def payload(**extras):
    return dict(profile='default', region='ap-northeast-1', names=['jpa04'],
        bundle_id='micro_3_0', blueprint_id='debian_13', **extras)


def test_http_preview_then_confirm_and_worker(env):
    app, q, aws, c, headers = env
    url = '/api/tasks/lightsail/create'
    assert c.post(url, json=payload(dry_run=True), headers=headers).json()['dry_run']
    assert q.list_tasks()['items'] == []
    assert c.post(url, json=payload(dry_run=True, confirm='CREATE'), headers=headers).status_code == 400
    response = c.post(url, json=payload(dry_run=False, confirm='CREATE'), headers=headers)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result['id'] and result['status'] == 'queued' and 'dry_run' not in result
    assert not aws.create_instances.called
    assert q.run_once()
    assert aws.create_instances.call_args.kwargs['instanceNames'] == ['jpa04']
    assert q.detail(result['id'])['status'] == 'completed'


def test_encrypted_bootstrap_reaches_aws_and_detail_is_redacted(env):
    app, q, aws, c, headers = env
    public = Ed25519PrivateKey.generate().public_key().public_bytes(
        serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()
    password = 'root-password-123456!'
    script = 'echo custom-script-secret > /root/setup-result\r\n'
    result = q.submit(q.CreateBatch(**payload(dry_run=False, confirm='CREATE',
        root_password=password, enable_root_ssh=True, ssh_public_key=public,
        user_data=script, key_pair_name='my-regional-key')))
    detail = c.get('/api/tasks/' + result['id']).text
    assert 'launch_configured' in detail
    for secret in (password, 'custom-script-secret', public, 'launch_encrypted'):
        assert secret not in detail
    with app.db() as conn:
        raw = conn.execute('SELECT payload FROM tasks').fetchone()[0]
    assert password not in raw and 'custom-script-secret' not in raw and public not in raw
    assert q.run_once()
    kwargs = aws.create_instances.call_args.kwargs
    assert kwargs['keyPairName'] == 'my-regional-key'
    config = json.loads(kwargs['userData'].split('\n', 1)[1])
    root = config['users'][1]
    assert sha512_crypt.verify(password, root['hashed_passwd'])
    assert password not in kwargs['userData']
    assert root['ssh_authorized_keys'] == [public]
    assert config['disable_root'] is False and config['ssh_pwauth'] is True
    assert 'PermitRootLogin yes' in config['runcmd'][0][2]
    assert 'PasswordAuthentication yes' in config['runcmd'][0][2]
    assert base64.b64encode(script.replace('\r\n', '\n').encode()).decode() in config['runcmd'][1][2]


@pytest.mark.parametrize('extra', [dict(root_password='short'), dict(ssh_public_key='PRIVATE KEY'),
    dict(enable_root_ssh=True), dict(user_data='中' * 40000, root_password='a' * 16, enable_root_ssh=True)])
def test_invalid_launch_configuration(env, extra):
    _, q, aws, _, _ = env
    with pytest.raises(HTTPException):
        q.submit(q.CreateBatch(**payload(**extra)))
    assert not aws.create_instances.called
    assert q.list_tasks()['items'] == []


def test_windows_rejected_for_root_options(env):
    _, q, aws, _, _ = env
    aws.get_blueprints.return_value = {'blueprints': [{'blueprintId': 'debian_13', 'platform': 'WINDOWS'}]}
    with pytest.raises(HTTPException):
        q.submit(q.CreateBatch(**payload(root_password='long-password-1234')))


def test_validation_does_not_echo_password_or_script(env):
    _, _, _, c, headers = env
    password = 'do-not-echo-password-' * 20
    script = 'private-script-' * 4000
    response = c.post('/api/tasks/lightsail/create',
        json=payload(root_password=password, user_data=script), headers=headers)
    assert response.status_code == 422
    assert password not in response.text and script not in response.text


@pytest.mark.parametrize('service', ['ec2', 'lightsail'])
def test_traffic_metrics(env, monkeypatch, service):
    app, _, aws, c, _ = env
    stamp = datetime(2026, 11, 17, tzinfo=timezone.utc)
    aws.get_metric_statistics.return_value = {'Datapoints': [{'Timestamp': stamp, 'Sum': 1024}]}
    aws.get_instance_metric_data.return_value = {'metricData': [{'timestamp': stamp, 'sum': 2048}]}
    def client(profile, api_service, region):
        assert api_service == ('cloudwatch' if service == 'ec2' else 'lightsail')
        return aws
    monkeypatch.setattr(app, 'client', client)
    result = c.get('/api/traffic', params=dict(profile='default', region='ap-northeast-1',
        instance='demo-01', service=service, days=30))
    assert result.status_code == 200, result.text
    assert result.json()['metrics']['NetworkIn']['bytes'] == (1024 if service == 'ec2' else 2048)
    if service == 'ec2':
        assert aws.get_metric_statistics.call_args.kwargs['Dimensions'] == [{'Name': 'InstanceId', 'Value': 'demo-01'}]


def test_validation_finds_blueprint_on_later_page(env):
    _, q, aws, _, _ = env
    aws.get_blueprints.side_effect = [
        {'blueprints': [], 'nextPageToken': 'next'},
        {'blueprints': [{'blueprintId': 'debian_13'}]}]
    assert q.submit(q.CreateBatch(**payload()))['dry_run']
    assert aws.get_blueprints.call_args.kwargs['pageToken'] == 'next'
