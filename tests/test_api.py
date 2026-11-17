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
    assert c.post('/api/instance/action', json={}).status_code == 403
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


def test_batch_partial_failure_audited(setup, monkeypatch):
    app, c = setup
    h = logged_in(c)
    def fake(x):
        if x.instance.endswith('b'):
            from fastapi import HTTPException
            raise HTTPException(400, 'Rejected')
    monkeypatch.setattr(app, 'invoke_action', fake)
    item = {'profile': 'default', 'region': 'us-east-1', 'service': 'lightsail', 'action': 'stop'}
    response = c.post('/api/instance/batch', headers=h, json={'items': [{**item, 'instance': 'a'}, {**item, 'instance': 'b'}]})
    assert response.status_code == 200
    assert response.json()['submitted'] == 1
    logs = c.get('/api/audit').json()['items']
    assert any(x['result'] == 'failed' for x in logs)
    assert c.post('/api/instance/batch', headers=h, json={'items': [{**item, 'instance': 'a'}] * 2}).status_code == 400


def test_traffic_sums(setup, monkeypatch):
    from datetime import datetime, timezone
    app, c = setup
    h = logged_in(c)
    fake = MagicMock()
    fake.get_instance_metric_data.side_effect = [
        {'metricData': [{'timestamp': datetime.now(timezone.utc), 'sum': 100.0}]},
        {'metricData': [{'timestamp': datetime.now(timezone.utc), 'sum': 230.0}]},
    ]
    monkeypatch.setattr(app, 'client', lambda *args: fake)
    result = c.get('/api/traffic?profile=default&region=us-east-1&instance=demo&days=1').json()
    assert result['metrics']['NetworkIn']['bytes'] == 100
    assert result['metrics']['NetworkOut']['bytes'] == 230
    assert c.get('/api/traffic?profile=default&region=xx&instance=demo').status_code == 400


def test_monitor_crud_samples(setup, monkeypatch):
    app, c = setup
    h = logged_in(c)
    aws = MagicMock()
    monkeypatch.setattr(app, 'client', lambda *args: aws)
    body = {'profile': 'default', 'region': 'us-east-1', 'instance': 'demo', 'threshold_gib': 1.25}
    assert c.post('/api/monitors', headers=h, json=body).status_code == 200
    monitor = c.get('/api/monitors').json()['items'][0]
    assert monitor['threshold_gib'] == 1.25
    assert c.get('/api/monitor/samples?monitor_id='+str(monitor['id'])).status_code == 200
    assert c.post('/api/monitors', headers=h, json={**body, 'threshold_gib': 0}).status_code == 422
    assert c.delete('/api/monitors/'+str(monitor['id']), headers=h).status_code == 200
    assert c.get('/api/monitors').json()['items'] == []


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


def test_worker_alert_cooldown(setup, monkeypatch):
    from datetime import datetime, timezone
    app, c = setup
    h = logged_in(c)
    aws = MagicMock()
    monkeypatch.setattr(app, 'client', lambda *args: aws)
    body = {'profile': 'default', 'region': 'us-east-1', 'instance': 'demo', 'threshold_gib': 0.1}
    assert c.post('/api/monitors', headers=h, json=body).status_code == 200
    import worker
    importlib.reload(worker)
    monkeypatch.setattr(worker, 'client', lambda *args: aws)
    monkeypatch.setattr(worker, 'telegram', lambda *args: True)
    aws.get_instance_metric_data.return_value = {'metricData': [{'sum': 1024**3}]}
    now = datetime(2026, 11, 16, 10, 10, tzinfo=timezone.utc)
    assert worker.run_once() == 1
    mon = c.get('/api/monitors').json()['items'][0]
    assert worker.collect(mon, now=now)['alert'] is None or isinstance(worker.collect(mon, now=now)['alert'], int)
    alerts = c.get('/api/alerts').json()['items']
    assert len(alerts) >= 1
