import importlib
import sys
from unittest.mock import MagicMock
import pytest

@pytest.fixture
def api(tmp_path,monkeypatch):
    monkeypatch.setenv('APP_PASSWORD','test-password-long-enough')
    monkeypatch.setenv('DATA_DIR',str(tmp_path))
    monkeypatch.setenv('AWS_REGIONS','us-east-1,ap-northeast-1')
    sys.modules.pop('app',None)
    import app
    importlib.reload(app)
    return app

def test_cli_inventory(api, monkeypatch,capsys):
    import cli
    monkeypatch.setattr(api,'instances',lambda **k: {'items':[{'name':'node1','region':'us-east-1','state':'running'}], 'errors':[]})
    assert cli.main(['instances','list','--region','us-east-1','--json'],api=api)==0
    assert 'node1' in capsys.readouterr().out

def test_cli_write_confirmation_and_audit(api,monkeypatch,capsys):
    import cli
    fun=MagicMock()
    monkeypatch.setattr(api,'invoke_action',fun)
    cmd=['instances','stop','--service','lightsail','--region','us-east-1','--name','node1']
    monkeypatch.setattr(sys.stdin,'isatty',lambda:False)
    assert cli.main(cmd,api=api)==2
    fun.assert_not_called()
    assert cli.main(cmd+['--yes'],api=api)==0
    fun.assert_called_once()
    assert api.audit_list()['items'][0]['ip']=='local-cli'

def test_cli_reject_unscoped_writes(api, monkeypatch):
    import cli
    fun=MagicMock()
    monkeypatch.setattr(api,'invoke_action',fun)
    assert cli.main(['instances','reboot','--service','ec2','--name','i-12345678','--yes'],api=api)==2
    fun.assert_not_called()

def test_cli_ip_release_guard(api, monkeypatch):
    import cli
    mock=MagicMock()
    mock.get_static_ip.return_value={'staticIp':{'name':'unused','isAttached':True}}
    monkeypatch.setattr(api,'client',lambda *args:mock)
    assert cli.main(['ip','release','--region','us-east-1','--name','unused','--yes'],api=api)==1
    mock.release_static_ip.assert_not_called()
    mock.get_static_ip.return_value={'staticIp':{'name':'unused','isAttached':False}}
    assert cli.main(['ip','release','--region','us-east-1','--name','unused','--yes'],api=api)==0
    mock.release_static_ip.assert_called_once_with(staticIpName='unused')

def test_cli_partial_inventory_exit_code(api,monkeypatch):
    import cli
    monkeypatch.setattr(api,'instances',lambda **kw:{'items':[],'errors':[{'region':'us-east-1','message':'denied'}]})
    assert cli.main(['instances','list'],api=api)==3

def test_cli_account_secret_not_displayed(api,monkeypatch,capsys):
    import cli
    monkeypatch.setattr(api,'base_profiles',lambda:['default'])
    monkeypatch.setattr(api,'accounts',lambda:[{'alias':'dev','external_id':'supersecret','role_arn':'arn:role','source_profile':'default'}])
    assert cli.main(['accounts','list','--json'],api=api)==0
    assert 'supersecret' not in capsys.readouterr().out
