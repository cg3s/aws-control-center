import base64
import json
import shlex
from fastapi import HTTPException
from passlib.hash import sha512_crypt
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from .models import Launch


def validate_launch(launch: Launch, *, template=False):
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
    if launch.root_login == 'password' and not launch.password_auth:
        raise HTTPException(400, '允许 root 密码登录时，请启用 SSH 密码认证')
    if launch.root_login == 'password' and not launch.root_password and not template:
        raise HTTPException(400, '允许 root 密码登录时，请在创建实例时填写 root 初始密码')
    if launch.root_password and launch.password_auth and launch.root_login == 'disabled':
        raise HTTPException(400, '已设置 root 密码并启用 SSH 密码认证，但 root SSH 登录仍为禁止。请选择允许密码、仅密钥或关闭 SSH 密码认证')
    return keys


def ssh_config_command(launch: Launch):
    policy = {'disabled': 'no', 'key': 'prohibit-password', 'password': 'yes'}[launch.root_login]
    options = f'PermitRootLogin {policy}\nPubkeyAuthentication yes\nPasswordAuthentication {"yes" if launch.password_auth else "no"}\n'
    verify = ''
    if launch.root_login == 'password':
        verify = '''effective=$(sshd -T -f "$candidate" -C user=root,host=localhost,addr=127.0.0.1)
if ! printf '%s\\n' "$effective" | grep -qx 'permitrootlogin yes' ||
   ! printf '%s\\n' "$effective" | grep -qx 'passwordauthentication yes' ||
   ! printf '%s\\n' "$effective" | grep -Eq '^authenticationmethods (.* )?(any|password)( |$)'; then
    echo 'AWSCC: The image SSH policy still blocks root password login' >&2
    exit 1
fi
'''
    return '''set -eu
config=/etc/ssh/sshd_config
candidate=$(mktemp /etc/ssh/sshd_config.awscc.XXXXXX)
trap 'rm -f "$candidate"' EXIT
install -d -m 0755 /run/sshd
''' + '{ printf %s ' + shlex.quote(options) + '''; cat "$config"; } > "$candidate"
sshd -t -f "$candidate"
''' + verify + '''backup=/etc/ssh/sshd_config.awscc-backup
cp -p "$config" "$backup"
cat "$candidate" > "$config"
reload_ssh() {
    if systemctl is-active --quiet ssh.service; then
        systemctl reload ssh.service
    elif systemctl is-active --quiet sshd.service; then
        systemctl reload sshd.service
    elif systemctl is-active --quiet ssh.socket || systemctl is-active --quiet sshd.socket; then
        # The next socket-activated sshd starts with the new configuration.
        return 0
    else
        systemctl reload ssh.service || systemctl reload sshd.service
    fi
}
if ! reload_ssh; then
    cp -p "$backup" "$config"
    echo 'AWSCC: SSH reload failed; restored the previous configuration' >&2
    exit 1
fi
'''


def render(launch: Launch, service):
    keys = validate_launch(launch)
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
    command = ssh_config_command(launch)
    if launch.user_data:
        data = base64.b64encode(launch.user_data.replace('\r\n', '\n').encode()).decode()
        command += ('umask 077\nprintf %s ' + shlex.quote(data) +
                    ' | base64 -d > /var/lib/cloud/awscc-launch.sh\nchmod 700 /var/lib/cloud/awscc-launch.sh\nbash /var/lib/cloud/awscc-launch.sh\n')
    config['runcmd'].append(['sh', '-c', command])
    result = '#cloud-config\n' + json.dumps(config)
    if len(result.encode()) > (16384 if service == 'ec2' else 65536):
        raise HTTPException(400, 'Combined initialization exceeds service user-data size limit')
    return result
