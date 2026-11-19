import importlib
import sys
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_PASSWORD', 'very-long-test-password-123')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    monkeypatch.setenv('AWS_REGIONS', 'us-east-1,ap-northeast-1')
    sys.modules.pop('app', None)
    import app
    importlib.reload(app)
    return app, TestClient(app.app)


def logged_in(client):
    result = client.post('/api/login', json={'password': 'very-long-test-password-123'})
    assert result.status_code == 200
    return {'X-AWSCC-CSRF': result.json()['csrf']}


def test_auth_and_csrf(setup):
    app, c = setup
    assert c.get('/api/config').status_code == 401
    headers = logged_in(c)
    assert c.get('/api/config').status_code == 200
    assert c.post('/api/tasks', json={}).status_code == 403
    assert c.get('/api/session').json()['csrf'] == headers['X-AWSCC-CSRF']
    assert c.post('/api/logout', headers=headers).status_code == 200
    assert c.get('/api/config').status_code == 401


def test_account_crud(setup, monkeypatch):
    app, c = setup
    monkeypatch.setattr(app, 'base_profiles', lambda: ['default', 'source'])
    h = logged_in(c)
    req = {'alias': 'target', 'source_profile': 'source', 'role_arn': 'arn:aws:iam::123456789012:role/ReadOnly'}
    assert c.post('/api/accounts', json=req, headers=h).status_code == 200
    assert 'target' in c.get('/api/config').json()['profiles']
    assert c.post('/api/accounts', json=req, headers=h).status_code == 409
    assert c.delete('/api/accounts/target', headers=h).status_code == 200
    assert len(c.get('/api/audit').json()['items']) >= 3








def test_tcp_probe_guard(setup, monkeypatch):
    app, c = setup
    h = logged_in(c)
    aws = MagicMock()
    aws.get_instance.return_value = {'instance': {'publicIpAddress': '127.0.0.1'}}
    monkeypatch.setattr(app, 'client', lambda *args: aws)
    body = {'profile': 'default', 'region': 'us-east-1', 'instance': 'demo', 'port': 22}
    assert c.post('/api/ip/check', json=body, headers=h).status_code == 400
    assert c.post('/api/ip/check', json={**body, 'port': 3000}, headers=h).status_code == 400
    aws.get_instance.return_value = {'instance': {'publicIpAddress': '1.1.1.1'}}
    class Sock:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(app.socket, 'create_connection', lambda *args, **kwargs: Sock())
    assert c.post('/api/ip/check', json={**body, 'port': 443}, headers=h).json()['tcp_reachable'] is True
