"""V8 summary endpoint tests, no real AWS interactions."""
import sys
from fastapi.testclient import TestClient
import pytest

@pytest.fixture
def context(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_PASSWORD','v8-testing-secure-password-123456')
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('COOKIE_SECURE','false')
    monkeypatch.setenv('AWS_REGIONS','us-east-1')
    sys.modules.pop('app',None)
    import app
    return app

def test_requires_login(context):
    c=TestClient(context.app)
    assert c.get('/api/dashboard/summary').status_code==401
    assert c.get('/legacy').status_code==200

def test_aggregation_and_partial_failures(context,monkeypatch):
    app=context
    monkeypatch.setattr(app,'instances',lambda **kwargs: {'items':[
        {'service':'lightsail','region':'us-east-1','state':'running'},
        {'service':'ec2','region':'us-east-1','state':'stopped'},
        {'service':'ec2','region':'us-east-1','state':'running'}],
        'errors':[{'region':'us-west-2','service':'ec2','message':'denied'}]})
    c=TestClient(app.app)
    csrf=c.post('/api/login',json={'password':'v8-testing-secure-password-123456'}).json()['csrf']
    assert csrf
    r=c.get('/api/dashboard/summary?service=all')
    assert r.status_code==200
    j=r.json()
    assert j['total']==3 and j['running']==2 and j['stopped']==1
    assert j['by_service']=={'lightsail':1,'ec2':2}
    assert len(j['errors'])==1
    assert j['task_counts']=={}

def test_asset_path_is_restricted(context):
    c=TestClient(context.app)
    assert c.get('/v8/assets/../../app.py').status_code==404
    assert c.get('/v8/assets/nonexistent.js').status_code==404
