#!/usr/bin/env python3
"""root-only native launcher: read config, then drop privileges to systemd service user."""
import os
import pwd
import subprocess
import sys
from pathlib import Path

CONFIG=Path('/etc/aws-control-center/awscc.env')
SERVICE=Path('/etc/systemd/system/awscc-web.service')

def main():
    if os.geteuid()!=0: sys.exit('Run: sudo awscc <resource command>')
    values={}
    for raw in CONFIG.read_text().splitlines():
        line=raw.strip()
        if not line or line.startswith('#'):continue
        if '=' not in line:sys.exit('Invalid .env config line')
        key,val=line.split('=',1)
        if not key.isidentifier():sys.exit('Invalid env key')
        values[key]=val.strip().strip('"').strip("'")
    svc=SERVICE.read_text()
    user=next((line.split('=',1)[1].strip() for line in svc.splitlines() if line.startswith('User=')),None)
    if not user:sys.exit('Missing service user')
    account=pwd.getpwnam(user)
    env=dict(os.environ)
    # Prevent inherited AWS override vars from selecting a different account.
    for key in tuple(env):
        if key.startswith('AWS_') or key in ('DATA_DIR','APP_PASSWORD','COOKIE_SECURE'):
            env.pop(key,None)
    env.update(values)
    env['HOME']=account.pw_dir
    env['PYTHONDONTWRITEBYTECODE']='1'
    def drop():
        os.initgroups(user,account.pw_gid)
        os.setgid(account.pw_gid)
        os.setuid(account.pw_uid)
    proc=subprocess.run(['/opt/aws-control-center/.venv/bin/python','/opt/aws-control-center/cli.py',*sys.argv[1:]],env=env,cwd='/opt/aws-control-center',preexec_fn=drop)
    return proc.returncode

if __name__=='__main__':sys.exit(main())
