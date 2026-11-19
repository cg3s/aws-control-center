import base64
import hashlib
from unittest.mock import MagicMock
import pytest
import paramiko
from fastapi import HTTPException
from controlplane import ssh
from test_tasks import request, enqueue


def connection():
    return dict(username='root',port=22,address='public',fingerprint='SHA256:'+'A'*43,password='password',sudo=False)


def test_fingerprint_mismatch_blocks_remote_command(control,monkeypatch):
    web,_,_,_=control
    client=MagicMock()
    def connect(*args,**kwargs):
        policy=client.set_missing_host_key_policy.call_args.args[0]
        key=MagicMock();key.asbytes.return_value=b'wrong-host'
        policy.missing_host_key(client,'1.1.1.1',key)
    client.connect.side_effect=connect
    monkeypatch.setattr(paramiko,'SSHClient',lambda:client)
    with pytest.raises(paramiko.SSHException):
        ssh.execute(web,request().items[0].target.model_dump(),connection(),{'body':'echo hi','timeout':1})
    client.exec_command.assert_not_called()
    client.close.assert_called_once()


def test_pinned_ssh_streams_script_and_limits_output(control,monkeypatch):
    web,_,_,_=control
    client=MagicMock(); channel=MagicMock();stdin=MagicMock();stdout=MagicMock();stdout.channel=channel
    client.exec_command.return_value=(stdin,stdout,MagicMock())
    channel.recv_ready.side_effect=[True,False,False]
    channel.recv.return_value=b'hello'
    channel.recv_stderr_ready.return_value=False
    channel.exit_status_ready.return_value=True
    channel.recv_exit_status.return_value=0
    monkeypatch.setattr(paramiko,'SSHClient',lambda:client)
    result=ssh.execute(web,request().items[0].target.model_dump(),connection(),{'body':'echo "$HOME"','timeout':1})
    assert result['exit_code']==0 and result['stdout']=='hello'
    client.exec_command.assert_called_once_with('bash -s',timeout=10)
    stdin.write.assert_called_once_with('echo "$HOME"')
    assert client.connect.call_args.kwargs['allow_agent'] is False


def test_metadata_and_local_addresses_are_never_ssh_targets(control):
    web,_,_,aws=control
    aws.get_instance.return_value['instance']['publicIpAddress']='169.254.169.254'
    with pytest.raises(HTTPException):
        ssh.execute(web,request().items[0].target.model_dump(),connection(),{'body':'echo hi','timeout':1})


def test_script_timeout_is_unknown_and_next_stop_can_run(control,monkeypatch):
    web,c,h,aws=control
    con=c.post('/api/configurations',headers=h,json={'kind':'connection','name':'ssh','payload':connection()}).json()
    script=c.post('/api/configurations',headers=h,json={'kind':'script','name':'prepare-stop','payload':{'body':'echo hi','timeout':1}}).json()
    data=request('script');data.items[0].connection_id=con['id'];data.items[0].script_id=script['id']
    data.items.append(request('stop').items[0])
    task=enqueue(web,data)
    monkeypatch.setattr(ssh,'execute',lambda *args: (_ for _ in ()).throw(TimeoutError()))
    web.control_jobs.run_once()
    assert web.control_jobs.detail(task['id'])['items'][0]['status']=='unknown'
    web.control_jobs.run_once()
    aws.stop_instance.assert_called_once()


def test_instance_bound_connection_and_parameters_are_snapshotted(control):
    web,c,h,_=control
    con=c.post('/api/configurations',headers=h,json={'kind':'connection','name':'bound','payload':connection()|{'resource_key':'aws/123456789012/us-east-1/lightsail/node'}}).json()
    script=c.post('/api/configurations',headers=h,json={'kind':'script','name':'parameters','payload':{'body':'echo "$ARG"','parameters':{'ARG':'default'}}}).json()
    data=request('script');data.items[0].script_id=script['id'];data.items[0].parameters={'ARG':'$(echo injection)'}
    prepared=web.control_jobs.prepare(data.items[0])
    assert prepared['connection_version']==con['version']
    assert "export ARG='$(echo injection)'" in prepared['script']['body']
    assert 'AWSCC_INSTANCE=node' in prepared['script']['body']
