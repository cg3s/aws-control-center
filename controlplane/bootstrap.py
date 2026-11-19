import base64
import json
import shlex
from fastapi import HTTPException
from passlib.hash import sha512_crypt
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from .models import Launch


def render(launch: Launch, service):
    if launch.username == 'root':
        raise HTTPException(400, 'Use the root login settings instead of adding root as an ordinary user')
    keys = [k.strip() for k in launch.ssh_public_key.splitlines() if k.strip()]
    try:
        for key in keys:
            load_ssh_public_key(key.encode())
    except Exception:
        raise HTTPException(400, 'Invalid SSH public key')
    if launch.root_password and (len(launch.root_password) < 16 or any(c in launch.root_password for c in '\r\n\0')):
        raise HTTPException(400, 'Root password must have at least 16 characters and no control newlines')
    if launch.root_login == 'key' and not keys:
        raise HTTPException(400, 'Root key login requires public keys')
    if launch.root_login == 'password' and not (launch.root_password and launch.password_auth):
        raise HTTPException(400, 'Root password login requires a password and password authentication')
    config = {'users': ['default'], 'disable_root': launch.root_login == 'disabled',
              'ssh_pwauth': launch.password_auth, 'runcmd': []}
    if launch.username:
        config['users'].append({'name': launch.username, 'shell': '/bin/bash',
                                'ssh_authorized_keys': keys, 'sudo': ['ALL=(ALL) NOPASSWD:ALL']})
    root = {'name': 'root'}
    if launch.root_password:
        root.update(hashed_passwd=sha512_crypt.using(rounds=100000).hash(launch.root_password), lock_passwd=False)
    if keys and launch.root_login != 'disabled':
        root['ssh_authorized_keys'] = keys
    if len(root) > 1:
        config['users'].append(root)
    if keys and not launch.username and launch.root_login == 'disabled':
        config['ssh_authorized_keys'] = keys
    policy = {'disabled': 'no', 'key': 'prohibit-password', 'password': 'yes'}[launch.root_login]
    options = f'PermitRootLogin {policy}\nPubkeyAuthentication yes\nPasswordAuthentication {"yes" if launch.password_auth else "no"}\n'
    command = ('set -eu\ncp -p /etc/ssh/sshd_config /etc/ssh/sshd_config.awscc-backup\n'
               '{ printf %s ' + shlex.quote(options) + '; cat /etc/ssh/sshd_config.awscc-backup; } > /etc/ssh/sshd_config\n'
               'if ! /usr/sbin/sshd -t; then cp -p /etc/ssh/sshd_config.awscc-backup /etc/ssh/sshd_config; exit 1; fi\n'
               'systemctl reload ssh || systemctl reload sshd\n')
    config['runcmd'].append(['sh', '-c', command])
    if launch.user_data:
        data = base64.b64encode(launch.user_data.replace('\r\n', '\n').encode()).decode()
        config['runcmd'].append(['bash', '-c', 'printf %s '+shlex.quote(data)+
                                ' | base64 -d > /var/lib/cloud/awscc-launch.sh && chmod 700 /var/lib/cloud/awscc-launch.sh && bash /var/lib/cloud/awscc-launch.sh'])
    result = '#cloud-config\n' + json.dumps(config)
    if len(result.encode()) > (16384 if service == 'ec2' else 65536):
        raise HTTPException(400, 'Combined initialization exceeds service user-data size limit')
    return result
