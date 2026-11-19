import base64
import json
import shlex
from pathlib import Path
from fastapi import HTTPException
from passlib.hash import sha512_crypt
from cryptography.hazmat.primitives.serialization import load_ssh_public_key
from .models import Launch

USER_SETUP = Path(__file__).with_name('bootstrap_users.sh').read_text(encoding='utf-8')


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
    root_options = ''
    verify = ''
    if launch.root_login != 'disabled':
        methods = 'any' if launch.root_login == 'password' else 'publickey'
        root_options = f'\nMatch User root\n    AuthenticationMethods {methods}\n'
        if launch.ssh_public_key.strip():
            root_options += '    AuthorizedKeysFile .ssh/authorized_keys .ssh/authorized_keys2\n'
            verify += '''if ! printf '%s\\n' "$effective" | grep -qx 'pubkeyauthentication yes' ||
   ! printf '%s\\n' "$effective" | grep -Eq '^authenticationmethods (.* )?(any|publickey)( |$)'; then
    fail 'The image SSH policy blocks independent root public-key authentication'
fi
'''
    if launch.root_login == 'password':
        verify += '''if ! printf '%s\\n' "$effective" | grep -qx 'permitrootlogin yes' ||
   ! printf '%s\\n' "$effective" | grep -qx 'passwordauthentication yes' ||
   ! printf '%s\\n' "$effective" | grep -Eq '^authenticationmethods (.* )?(any|password)( |$)'; then
    fail 'The image SSH policy still blocks independent root password authentication'
fi
'''
    elif launch.root_login == 'key':
        verify += '''printf '%s\\n' "$effective" | grep -Eq '^permitrootlogin (without-password|prohibit-password)$' || fail 'The image SSH policy blocks root key login'
'''
    else:
        verify += '''printf '%s\\n' "$effective" | grep -qx 'permitrootlogin no' || fail 'The image SSH policy still allows root SSH login'
'''
    return '''check_ssh_policy() {
    sshd -t -f "$1"
    effective=$(sshd -T -f "$1" -C user=root,host=localhost,addr=127.0.0.1)
''' + verify + '''}
config=/etc/ssh/sshd_config
candidate=$(mktemp /etc/ssh/sshd_config.awscc.XXXXXX)
trap 'rm -f "$candidate"' EXIT
install -d -m 0755 /run/sshd
{
    printf '# BEGIN AWSCC SSH\\n'
    printf %s ''' + shlex.quote(options) + '''
    printf '# END AWSCC SSH\\n'
    awk '/^# BEGIN AWSCC SSH$/ { managed=1; next } /^# END AWSCC SSH$/ { managed=0; next } !managed' "$config"
''' + ("    printf '# BEGIN AWSCC SSH\\n'\n    printf %s " + shlex.quote(root_options) + "\n    printf '# END AWSCC SSH\\n'\n" if root_options else '') + '''} > "$candidate"
check_ssh_policy "$candidate"
backup=/etc/ssh/sshd_config.awscc-backup
cp -p "$config" "$backup"
cat "$candidate" > "$config"
reload_ssh() {
    if ! command -v systemctl >/dev/null 2>&1; then
        service ssh reload || service sshd reload
    elif systemctl is-active --quiet ssh.service; then
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
    fail 'SSH reload failed; restored the previous configuration'
fi
printf 'AWSCC: SSH configuration applied\\n'
'''


def render(launch: Launch, service):
    keys = validate_launch(launch)
    lightsail = service == 'lightsail'
    variables = {
        'root_password_hash': sha512_crypt.using(rounds=100000).hash(launch.root_password) if launch.root_password else '',
        'root_keys': '\n'.join(keys) if launch.root_login != 'disabled' else '',
        'extra_user': launch.username if lightsail else '',
        'login_keys': '\n'.join(keys) if lightsail and (launch.username or launch.root_login == 'disabled') else '',
    }
    command = '''#!/bin/bash
set +x
set -Eeuo pipefail
umask 077
export PATH=/usr/sbin:/usr/bin:/sbin:/bin
trap 'printf "AWSCC: Initialization failed at line %s\\n" "$LINENO" >&2' ERR
'''
    command += ''.join(f'{name}={shlex.quote(value)}\n' for name, value in variables.items())
    command += USER_SETUP + '\n' + ssh_config_command(launch)
    if launch.user_data:
        data = base64.b64encode(launch.user_data.replace('\r\n', '\n').encode()).decode()
        command += ('install -d -m 0700 /var/lib/awscc\nprintf %s ' + shlex.quote(data) +
                    ' | base64 -d > /var/lib/awscc/launch.sh\nchmod 700 /var/lib/awscc/launch.sh\nbash /var/lib/awscc/launch.sh\n')
        command += 'verify_root_password\ncheck_ssh_policy "$config"\n'
    command += "printf 'AWSCC: Initialization complete\\n'\n"

    if lightsail:
        result = command
    else:
        path = '/var/lib/cloud/awscc-init.sh'
        config = {'users': ['default'], 'disable_root': launch.root_login == 'disabled',
                  'write_files': [{'path': path, 'content': command, 'permissions': '0700'}],
                  'runcmd': [['/bin/bash', path]]}
        if launch.username:
            config['users'].append({'name': launch.username, 'shell': '/bin/bash',
                                    'ssh_authorized_keys': keys, 'sudo': ['ALL=(ALL) NOPASSWD:ALL']})
        elif keys and launch.root_login == 'disabled':
            config['ssh_authorized_keys'] = keys
        result = '#cloud-config\n' + json.dumps(config)
    if len(result.encode()) > (16384 if service == 'ec2' else 65536):
        raise HTTPException(400, 'Combined initialization exceeds service user-data size limit')
    return result
