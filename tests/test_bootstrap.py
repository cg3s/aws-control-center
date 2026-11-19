import json
import pytest
from fastapi import HTTPException
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives import serialization
from controlplane.models import Launch
from controlplane.bootstrap import render


def public_key():
    return Ed25519PrivateKey.generate().public_key().public_bytes(serialization.Encoding.OpenSSH, serialization.PublicFormat.OpenSSH).decode()


def test_password_is_hashed_and_ssh_options_are_independent():
    source = render(Launch(root_password='a-strong-password-1234'), 'ec2')
    assert 'a-strong-password-1234' not in source
    config = json.loads(source.split('\n', 1)[1])
    assert config['users'][1]['hashed_passwd'].startswith('$6$')
    assert config['ssh_pwauth'] is False
    assert 'PermitRootLogin no' in source


@pytest.mark.parametrize('launch', [dict(root_password='short'), dict(ssh_public_key='private-key'), dict(root_login='key'),
    dict(root_login='password',root_password='a-strong-password-1234'), dict(root_login='password', password_auth=True),
    dict(root_password='a-strong-password-1234', password_auth=True)])
def test_invalid_initialization_rejected(launch):
    with pytest.raises(HTTPException): render(Launch(**launch), 'ec2')


def test_default_and_regular_user_public_keys():
    key = public_key()
    data = json.loads(render(Launch(ssh_public_key=key), 'lightsail').split('\n',1)[1])
    assert data['ssh_authorized_keys'] == [key]
    data = json.loads(render(Launch(ssh_public_key=key,username='operator'), 'ec2').split('\n',1)[1])
    assert data['users'][1]['name'] == 'operator'
    assert data['users'][1]['ssh_authorized_keys'] == [key]


def test_ec2_userdata_size_is_checked_after_encoding():
    with pytest.raises(HTTPException): render(Launch(user_data='x'*20000), 'ec2')
    assert render(Launch(user_data='x'*20000), 'lightsail')


def test_password_template_applies_auth_policy_on_launch(control, monkeypatch):
    from botocore.exceptions import ClientError
    from passlib.hash import sha512_crypt
    from controlplane import resources
    from test_tasks import enqueue, request

    web, client, headers, aws = control
    key = public_key()
    password = 'test-instance-password-123'
    template = client.post('/api/configurations', headers=headers, json={'kind': 'bootstrap', 'name': 'root-ssh', 'payload': {
        'root_login': 'password', 'password_auth': True, 'ssh_public_key': key,
    }})
    assert template.status_code == 200, template.text
    assert 'root_password' not in template.json()['payload']
    monkeypatch.setattr(resources, 'catalog', lambda *args: {'bundles': [{'id': 'micro', 'price_usd_per_month': 5}],
                        'blueprints': [{'id': 'debian'}], 'zones': ['us-east-1a']})
    aws.get_instance.side_effect = ClientError({'Error': {'Code': 'NotFoundException'}}, 'GetInstance')

    job = request('create', bundle_id='micro', blueprint_id='debian', bootstrap_id=template.json()['id'])
    missing = client.post('/api/tasks/preview', headers=headers, json=job.model_dump(exclude_unset=True))
    assert missing.status_code == 400
    job.items[0].options.launch = Launch(root_password=password)
    # Simulate the HTTP request including only the per-launch password override.
    preview = client.post('/api/tasks/preview', headers=headers, json=job.model_dump(exclude_unset=True))
    assert preview.json()['items'][0]['ssh_policy'] == {'root_login': 'password', 'password_auth': True, 'root_password_set': True}
    assert password not in preview.text
    enqueue(web, job)
    assert web.control_jobs.run_once()
    user_data = aws.create_instances.call_args.kwargs['userData']
    assert password not in user_data
    config = json.loads(user_data.split('\n', 1)[1])
    assert config['disable_root'] is False and config['ssh_pwauth'] is True
    root = next(user for user in config['users'] if isinstance(user, dict) and user['name'] == 'root')
    assert root['ssh_authorized_keys'] == [key] and root['lock_passwd'] is False
    assert sha512_crypt.verify(password, root['hashed_passwd'])
    assert 'PermitRootLogin yes' in config['runcmd'][0][2]
