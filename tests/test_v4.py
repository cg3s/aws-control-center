import pytest
from unittest.mock import MagicMock
from test_api import setup, logged_in


def test_static_ip_list_and_actions(setup, monkeypatch):
    app, c = setup
    h = logged_in(c)
    aws = MagicMock()
    aws.get_static_ips.return_value = {'staticIps': [{'name':'ip1', 'ipAddress':'1.2.3.4','isAttached':False}]}
    aws.get_static_ip.return_value = {'staticIp': {'isAttached': False}}
    monkeypatch.setattr(app, 'client', lambda *a: aws)
    assert c.get('/api/lightsail/static-ips?profile=default&region=us-east-1').json()['items'][0]['name'] == 'ip1'
    payload = {'profile':'default','region':'us-east-1','name':'ip1','action':'release'}
    assert c.post('/api/lightsail/static-ip/action',headers=h,json=payload).status_code == 400
    assert c.post('/api/lightsail/static-ip/action',headers=h,json={**payload,'confirm':'ip1'}).status_code == 200
    aws.release_static_ip.assert_called_once_with(staticIpName='ip1')
    aws.get_static_ip.return_value = {'staticIp': {'isAttached': True}}
    assert c.post('/api/lightsail/static-ip/action',headers=h,json={**payload,'confirm':'ip1'}).status_code == 409
    assert aws.release_static_ip.call_count == 1
    assert c.post('/api/lightsail/static-ip/action',headers=h,json={**payload,'action':'attach'}).status_code == 400
    assert c.post('/api/lightsail/static-ip/action',headers=h,json={**payload,'action':'allocate'}).status_code == 200
    aws.allocate_static_ip.assert_called_once_with(staticIpName='ip1')
    assert c.post('/api/lightsail/static-ip/action',json=payload).status_code == 403


def test_bundle_metadata(setup, monkeypatch):
    app, c = setup
    logged_in(c)
    aws = MagicMock()
    aws.get_instance.return_value = {'instance': {'bundleId': 'nano_3_0'}}
    aws.get_bundles.return_value = {'bundles': [{'bundleId':'nano_3_0','transferPerMonthInGb':1024,'price':7.0}]}
    monkeypatch.setattr(app,'client',lambda *args:aws)
    response = c.get('/api/lightsail/allowance?profile=default&region=us-east-1&instance=demo')
    assert response.status_code == 200
    assert response.json()['transfer_per_month_gb'] == 1024
    assert 'remaining' not in response.json()


def test_ip_check_history(setup, monkeypatch):
    app, c = setup
    h = logged_in(c)
    aws = MagicMock()
    aws.get_instance.return_value = {'instance': {'publicIpAddress': '1.1.1.1'}}
    monkeypatch.setattr(app,'client',lambda *args:aws)
    class Sock:
        def __enter__(self): return self
        def __exit__(self, *args): pass
    monkeypatch.setattr(app.socket,'create_connection',lambda *args,**kwargs:Sock())
    assert c.post('/api/ip/check', headers=h, json={'profile':'default','region':'us-east-1','instance':'demo','port':443}).status_code == 200
    rows = c.get('/api/ip/history?instance=demo').json()['items']
    assert len(rows) == 1
    assert rows[0]['reachable'] is True
    assert c.get('/api/ip/history?limit=999999').status_code == 400
