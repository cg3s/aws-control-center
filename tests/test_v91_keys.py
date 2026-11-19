import importlib
import os
import sys
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv('APP_PASSWORD', 'long-test-pass-12345678')
    monkeypatch.setenv('DATA_DIR', str(tmp_path))
    monkeypatch.setenv('COOKIE_SECURE', 'false')
    monkeypatch.delenv('AWSCC_ENCRYPTION_KEY', raising=False)
    sys.modules.pop('app', None)
    import app
    importlib.reload(app)
    client = TestClient(app.app)
    csrf = client.post('/api/login', json={'password':'long-test-pass-12345678'}).json()['csrf']
    return app, client, {'X-AWSCC-CSRF':csrf}, tmp_path


def stub_sts(app, monkeypatch):
    def mock_session(**kwargs):
        assert kwargs['aws_access_key_id'] == 'AKIA'+'A'*16
        assert kwargs['aws_secret_access_key'] == 'b'*40
        ses = MagicMock()
        ses.client.return_value.get_caller_identity.return_value = {
            'Account':'123456789012','Arn':'arn:aws:iam::123456789012:user/test'
        }
        return ses
    monkeypatch.setattr(app.boto3,'Session',mock_session)


def test_key_crud_activate_and_no_leak(env, monkeypatch):
    app, client, headers, path = env
    monkeypatch.setattr(app, 'base_profiles', lambda:['default'])
    stub_sts(app,monkeypatch)
    data = {'alias':'prod1','access_key_id':'AKIA'+'A'*16,'secret_access_key':'b'*40}
    assert client.post('/api/key-accounts',json=data).status_code==403
    r=client.post('/api/key-accounts',json=data,headers=headers)
    assert r.status_code==200,r.text
    assert 'secret' not in r.text
    assert client.get('/api/config').json()['active_profile']=='default'
    entries=client.get('/api/key-accounts').json()['items']
    assert len(entries)==1 and entries[0]['access_key_tail']=='AAAA'
    assert data['secret_access_key'] not in repr(entries)
    encrypted=(path/'control.db').read_bytes()
    assert data['secret_access_key'].encode() not in encrypted
    assert (path/'credential.key').exists()
    # Windows reports DOS attributes here; POSIX permissions apply on Linux deployment hosts.
    if os.name == 'posix':
        assert (path/'credential.key').stat().st_mode & 0o777 == 0o600
    r=client.post('/api/key-accounts/activate',json={'alias':'prod1'},headers=headers)
    assert r.status_code==200
    assert client.get('/api/config').json()['active_profile']=='prod1'
    assert app.aws_session('prod1')
    assert client.post('/api/key-accounts/prod1/verify',headers=headers).status_code==200
    assert client.delete('/api/key-accounts/prod1',headers=headers).status_code==200
    assert client.get('/api/config').json()['active_profile']=='default'


def test_reject_invalid_and_duplicate(env,monkeypatch):
    app,client,headers,_=env
    monkeypatch.setattr(app, 'base_profiles', lambda:['default'])
    fake=MagicMock()
    fake.client.return_value.get_caller_identity.side_effect=app.ClientError(
        {'Error':{'Code':'InvalidClientTokenId','Message':'no'}},'GetCallerIdentity')
    monkeypatch.setattr(app.boto3,'Session',lambda **kw:fake)
    payload={'alias':'staging','access_key_id':'AKIA'+'A'*16,'secret_access_key':'c'*40}
    assert client.post('/api/key-accounts',json=payload,headers=headers).status_code==400
    assert client.get('/api/key-accounts').json()['items']==[]
    assert 'c'*40 not in client.post('/api/key-accounts',json={**payload,'secret_access_key':'short'},headers=headers).text
    stub_sts(app,monkeypatch)
    payload['secret_access_key']='b'*40
    assert client.post('/api/key-accounts',json=payload,headers=headers).status_code==200
    assert client.post('/api/key-accounts',json=payload,headers=headers).status_code==409
    assert client.post('/api/key-accounts/activate',json={'alias':'unknown'},headers=headers).status_code==400


def test_refuse_removal_with_monitor(env,monkeypatch):
    app,client,headers,_=env
    monkeypatch.setattr(app, 'base_profiles', lambda:['default'])
    stub_sts(app,monkeypatch)
    client.post('/api/key-accounts',json={'alias':'prod1','access_key_id':'AKIA'+'A'*16,'secret_access_key':'b'*40},headers=headers)
    with app.db() as db:
        db.execute("INSERT INTO v2_documents VALUES('test-rule','rule','test',1,?,'now','now')", (app.vault().encrypt(b'{\"targets\":[{\"profile\":\"prod1\"}]}'),))
    assert client.delete('/api/key-accounts/prod1',headers=headers).status_code==409
