"""V6 queue tests: AWS calls are mocked, never create billable resources."""
import importlib
import sys
from unittest.mock import Mock

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient


@pytest.fixture
def v6(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_PASSWORD', 'strong-v6-test-password-1234')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('AWS_REGIONS', 'ap-northeast-1')
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    for name in ('taskqueue','app'):
        sys.modules.pop(name, None)
    import app
    import taskqueue
    return app, taskqueue


def mock_aws(v6, monkeypatch):
    app,q=v6
    aws=Mock()
    aws.get_bundles.return_value={'bundles':[{'bundleId':'nano_3_0','price':7,'isActive':True}]}
    aws.get_blueprints.return_value={'blueprints':[{'blueprintId':'ubuntu_22_04','isActive':True}]}
    from botocore.exceptions import ClientError
    aws.get_instance.side_effect=ClientError({'Error':{'Code':'NotFoundException'},'ResponseMetadata':{'HTTPStatusCode':404}},'GetInstance')
    monkeypatch.setattr(q,'valid_profile',lambda profile: ('base',profile))
    monkeypatch.setattr(q,'client',lambda profile, service, region:aws)
    return aws


def batch(q, **kwargs):
    data={'profile':'default','region':'ap-northeast-1','names':['demo-01','demo-02'],
          'bundle_id':'nano_3_0','blueprint_id':'ubuntu_22_04'}
    data.update(kwargs)
    return q.CreateBatch(**data)


def test_preview_and_submit(v6,monkeypatch):
    app,q=v6; aws=mock_aws(v6,monkeypatch)
    result=q.submit(batch(q))
    assert result['dry_run'] and result['estimate_usd_per_month']==14
    assert q.list_tasks()['items']==[]
    with pytest.raises(HTTPException):q.submit(batch(q,dry_run=False))
    created=q.submit(batch(q,dry_run=False,confirm='CREATE'))
    assert created['status']=='queued'
    assert len(q.detail(created['id'])['items'])==2
    assert not aws.create_instances.called


def test_worker_progress_and_cancel(v6,monkeypatch):
    app,q=v6;aws=mock_aws(v6,monkeypatch)
    task=q.submit(batch(q,dry_run=False,confirm='CREATE'))
    assert q.run_once() is True
    assert aws.create_instances.call_count==1
    assert q.detail(task['id'])['items'][0]['status']=='submitted'
    q.cancel(task['id'])
    d=q.detail(task['id'])
    assert d['status']=='running' or d['status']=='canceled' or d['status']=='partial'
    assert d['items'][1]['status']=='canceled'
    assert q.run_once() is False
    assert aws.create_instances.call_count==1


def test_failed_call_is_not_retried(v6,monkeypatch):
    app,q=v6;aws=mock_aws(v6,monkeypatch)
    aws.create_instances.side_effect=TimeoutError('ambiguous result')
    task=q.submit(batch(q,names=['only-01'],dry_run=False,confirm='CREATE'))
    assert q.run_once()
    assert q.detail(task['id'])['status']=='failed'
    assert q.run_once() is False
    assert aws.create_instances.call_count==1


def test_duplicate_names_are_rejected(v6,monkeypatch):
    app,q=v6;mock_aws(v6,monkeypatch)
    with pytest.raises(HTTPException) as exc:
        q.submit(batch(q,names=['same-01','same-01']))
    assert exc.value.status_code==400


def test_unauthenticated_task_api(v6):
    app,q=v6
    client=TestClient(app.app)
    assert client.get('/api/tasks').status_code==401
    assert client.post('/api/tasks/lightsail/create',json={}).status_code==401
