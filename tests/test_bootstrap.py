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


@pytest.mark.parametrize('launch', [dict(root_password='short'), dict(ssh_public_key='private-key'), dict(root_login='key'), dict(root_login='password',root_password='a-strong-password-1234')])
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
