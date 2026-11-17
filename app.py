"""AWS Control Center v9: self-hosted single-admin API."""
import json
import ipaddress
import socket
import logging
import os
import re
import secrets
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import boto3
from botocore.config import Config
from botocore.exceptions import BotoCoreError, ClientError
from cryptography.fernet import Fernet, InvalidToken
from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.responses import FileResponse, JSONResponse
from fastapi.exceptions import RequestValidationError
from pydantic import BaseModel, Field

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv('DATA_DIR', str(ROOT / 'data')))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / 'control.db'
PASSWORD = os.getenv('APP_PASSWORD', '')
if len(PASSWORD) < 16 or PASSWORD == 'CHANGE_ME_TO_A_LONG_RANDOM_PASSWORD':
    raise RuntimeError('APP_PASSWORD must be set to a strong password of at least 16 characters')
COOKIE_SECURE = os.getenv('COOKIE_SECURE', 'true').lower() == 'true'
REGIONS = list(dict.fromkeys(x.strip() for x in os.getenv('AWS_REGIONS', 'us-east-1,us-west-2,ap-northeast-1,ap-northeast-2,ap-southeast-1,eu-west-1').split(',') if x.strip()))
if not REGIONS:
    raise RuntimeError('AWS_REGIONS must not be empty')
TTL = 8 * 3600
AWS_CONFIG = Config(connect_timeout=5, read_timeout=18, retries={'max_attempts': 2, 'mode': 'standard'})
logger = logging.getLogger('awscc')
logging.basicConfig(level=logging.INFO)
app = FastAPI(title='AWS Control Center v9.1', docs_url=None, redoc_url=None)


@app.exception_handler(RequestValidationError)
async def safe_validation_error(request: Request, exc: RequestValidationError):
    if request.url.path == '/api/key-accounts':
        return JSONResponse(status_code=422, content={'detail': 'Invalid AWS credential form fields'})
    if request.url.path == '/api/tasks/lightsail/create':
        return JSONResponse(status_code=422, content={'detail': 'Invalid create form fields; check names, sizes and launch configuration'})
    return JSONResponse(status_code=422, content={'detail': exc.errors()})


def db():
    c = sqlite3.connect(DB_PATH, timeout=10)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('PRAGMA busy_timeout=10000')
    return c


def initialize_db():
    with db() as c:
        c.execute('CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, csrf TEXT NOT NULL, expiry INTEGER NOT NULL)')
        c.execute('CREATE TABLE IF NOT EXISTS attempts (ip TEXT PRIMARY KEY, count INTEGER NOT NULL, window_start INTEGER NOT NULL, locked_until INTEGER NOT NULL)')
        c.execute('''CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT, created_at TEXT NOT NULL, ip TEXT NOT NULL,
            category TEXT NOT NULL, profile TEXT, region TEXT, service TEXT, resource TEXT,
            operation TEXT, result TEXT NOT NULL, detail TEXT NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS audit_created ON audit(id DESC)')
        c.execute('''CREATE TABLE IF NOT EXISTS monitors (
            id INTEGER PRIMARY KEY AUTOINCREMENT, profile TEXT NOT NULL, region TEXT NOT NULL,
            instance TEXT NOT NULL, threshold_gib REAL NOT NULL, enabled INTEGER NOT NULL DEFAULT 1,
            last_alert_at TEXT, created_at TEXT NOT NULL,
            UNIQUE(profile,region,instance))''')
        c.execute('''CREATE TABLE IF NOT EXISTS monitor_samples (
            monitor_id INTEGER NOT NULL, measured_at TEXT NOT NULL, inbound INTEGER NOT NULL,
            outbound INTEGER NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(monitor_id, measured_at))''')
        c.execute('CREATE INDEX IF NOT EXISTS samples_recent ON monitor_samples(measured_at DESC)')
        c.execute('''CREATE TABLE IF NOT EXISTS alerts (
            id INTEGER PRIMARY KEY AUTOINCREMENT, monitor_id INTEGER NOT NULL,
            created_at TEXT NOT NULL, level TEXT NOT NULL, detail TEXT NOT NULL,
            delivered INTEGER NOT NULL DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS ip_checks (
            id INTEGER PRIMARY KEY AUTOINCREMENT, checked_at TEXT NOT NULL,
            profile TEXT NOT NULL, region TEXT NOT NULL, instance TEXT NOT NULL,
            address TEXT NOT NULL, port INTEGER NOT NULL, reachable INTEGER NOT NULL,
            latency_ms INTEGER NOT NULL, error TEXT NOT NULL)''')
        c.execute('CREATE INDEX IF NOT EXISTS ip_checks_recent ON ip_checks(id DESC)')
        c.execute('''CREATE TABLE IF NOT EXISTS key_accounts (
            alias TEXT PRIMARY KEY, access_key_id TEXT NOT NULL, secret_encrypted BLOB NOT NULL,
            aws_account_id TEXT NOT NULL, arn TEXT NOT NULL, created_at TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS app_settings (
            name TEXT PRIMARY KEY, value TEXT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS accounts (
            alias TEXT PRIMARY KEY, source_profile TEXT NOT NULL, role_arn TEXT NOT NULL,
            external_id TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL)''')


initialize_db()


def audit(ip, category, *, profile='', region='', service='', resource='', operation='', result='ok', detail=''):
    with db() as c:
        c.execute('''INSERT INTO audit(created_at,ip,category,profile,region,service,resource,operation,result,detail)
        VALUES(?,?,?,?,?,?,?,?,?,?)''', (datetime.now(timezone.utc).isoformat(timespec='seconds'), ip[:128], category,
         profile, region, service, resource[:200], operation, result, detail[:400]))


def digest(token):
    import hashlib
    return hashlib.sha256(token.encode()).hexdigest()


def ip_of(request: Request):
    # Never trust X-Forwarded-For from an unauthenticated client.
    return request.client.host if request.client else 'unknown'


def auth(request: Request):
    token = request.cookies.get('awscc_session', '')
    if not token:
        raise HTTPException(401, 'Login required')
    with db() as c:
        record = c.execute('SELECT csrf, expiry FROM sessions WHERE token_hash=?', (digest(token),)).fetchone()
    if not record or record['expiry'] < int(time.time()):
        raise HTTPException(401, 'Session expired')
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        header = request.headers.get('x-awscc-csrf', '')
        if not header or not secrets.compare_digest(header, record['csrf']):
            raise HTTPException(403, 'CSRF token missing or invalid')
    return True


def base_profiles():
    return ['default'] + [p for p in boto3.Session().available_profiles if p != 'default']


def accounts():
    with db() as c:
        return [dict(r) for r in c.execute('SELECT * FROM accounts ORDER BY alias').fetchall()]


def vault_key():
    # Shared encrypted key used by all API and Worker processes.
    supplied = os.getenv('AWSCC_ENCRYPTION_KEY', '').strip()
    if supplied:
        return supplied.encode('ascii')
    key_path = DATA_DIR / 'credential.key'
    # Exclusive create prevents competing workers from replacing an existing key.
    try:
        fd = os.open(key_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, 'wb') as out:
            out.write(Fernet.generate_key())
            out.flush()
            os.fsync(out.fileno())
    return key_path.read_bytes().strip()


def vault():
    try:
        return Fernet(vault_key())
    except (ValueError, InvalidToken) as exc:
        raise RuntimeError('AWSCC_ENCRYPTION_KEY or credential.key is invalid') from exc


def saved_keys():
    with db() as c:
        return [dict(x) for x in c.execute('''SELECT alias,access_key_id,aws_account_id,arn,created_at
                                          FROM key_accounts ORDER BY alias''').fetchall()]


def active_profile():
    with db() as c:
        row = c.execute("SELECT value FROM app_settings WHERE name='active_profile'").fetchone()
    selected = row['value'] if row else 'default'
    return selected if selected in available_profiles() else 'default'


def available_profiles():
    return list(dict.fromkeys(base_profiles() + [x['alias'] for x in accounts()] +
                              [x['alias'] for x in saved_keys()]))


def valid_profile(profile):
    if profile in base_profiles():
        return ('base', profile)
    stored = next((a for a in saved_keys() if a['alias'] == profile), None)
    if stored is not None:
        return ('key', stored)
    entry = next((a for a in accounts() if a['alias'] == profile), None)
    if entry is None:
        raise HTTPException(400, 'Unknown account/profile')
    return ('role', entry)


def aws_session(profile):
    kind, item = valid_profile(profile)
    if kind == 'base':
        return boto3.Session() if item == 'default' else boto3.Session(profile_name=item)
    if kind == 'key':
        with db() as c:
            row = c.execute('SELECT secret_encrypted FROM key_accounts WHERE alias=?', (profile,)).fetchone()
        if not row:
            raise HTTPException(404, 'Credential entry not found')
        try:
            secret = vault().decrypt(row['secret_encrypted']).decode('utf-8')
        except InvalidToken:
            raise HTTPException(503, 'Credential decryption failed; check encryption key')
        return boto3.Session(aws_access_key_id=item['access_key_id'],
                             aws_secret_access_key=secret)
    source = item['source_profile']
    if source not in base_profiles():
        raise HTTPException(400, 'Configured source profile no longer exists')
    original = boto3.Session() if source == 'default' else boto3.Session(profile_name=source)
    args = {'RoleArn': item['role_arn'], 'RoleSessionName': 'AWSControlCenter'}
    if item['external_id']:
        args['ExternalId'] = item['external_id']
    result = original.client('sts', config=AWS_CONFIG).assume_role(**args)['Credentials']
    return boto3.Session(aws_access_key_id=result['AccessKeyId'], aws_secret_access_key=result['SecretAccessKey'],
                         aws_session_token=result['SessionToken'])


def client(profile, service, region):
    return aws_session(profile).client(service, region_name=region, config=AWS_CONFIG)


def check_region(region):
    if region not in REGIONS:
        raise HTTPException(400, 'Region is not enabled')


def aws_error(exc):
    if isinstance(exc, ClientError):
        return exc.response.get('Error', {}).get('Code', 'AWS API failure') + ': AWS request failed (permissions, service or region)'
    return type(exc).__name__ + ': AWS request failed'


class Login(BaseModel):
    password: str


class InstanceAction(BaseModel):
    service: str
    profile: str
    region: str
    instance: str = Field(min_length=1, max_length=200)
    action: str


class BatchAction(BaseModel):
    items: list[InstanceAction] = Field(min_length=1, max_length=20)


class RoleAccount(BaseModel):
    alias: str = Field(min_length=2, max_length=40)
    source_profile: str
    role_arn: str
    external_id: str = Field(default='', max_length=200)


class KeyAccountIn(BaseModel):
    alias: str = Field(min_length=2, max_length=40)
    access_key_id: str = Field(min_length=16, max_length=128)
    secret_access_key: str = Field(min_length=20, max_length=256)


class ActivateAccountIn(BaseModel):
    alias: str = Field(min_length=1, max_length=80)


class MonitorIn(BaseModel):
    profile: str = Field(min_length=1, max_length=80)
    region: str
    instance: str = Field(min_length=1, max_length=200)
    threshold_gib: float = Field(gt=0, le=100000)
    enabled: bool = True


class StaticIpAction(BaseModel):
    profile: str = Field(min_length=1, max_length=80)
    region: str
    name: str = Field(min_length=1, max_length=200)
    action: str
    instance: str = Field(default='', max_length=200)
    confirm: str = Field(default='', max_length=200)


class IpCheck(BaseModel):
    profile: str
    region: str
    instance: str = Field(min_length=1, max_length=200)
    port: int


@app.get('/')
def home():
    modern = ROOT / 'static' / 'v8' / 'index.html'
    return FileResponse(modern if modern.is_file() else ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/legacy')
def legacy_home():
    return FileResponse(ROOT / 'static' / 'index.html', headers={'Cache-Control': 'no-store'})


@app.get('/v8/assets/{filename}')
def modern_asset(filename: str):
    # Only serve hashed build products from the fixed directory, never arbitrary paths.
    if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.(?:js|css|svg|png)', filename):
        raise HTTPException(404, 'Not found')
    target = ROOT / 'static' / 'v8' / 'assets' / filename
    if not target.is_file():
        raise HTTPException(404, 'Not found')
    return FileResponse(target)


@app.get('/api/dashboard/summary', dependencies=[Depends(auth)])
def dashboard_summary(profile: str = 'default', region: str = 'all', service: str = 'all'):
    # Costs no extra AWS calls compared with inventory, and retains partial failures.
    data = instances(profile=profile, service=service, region=region)
    resources = data['items']
    with db() as conn:
        try:
            task_counts = {r['status']: r['n'] for r in conn.execute(
                'SELECT status, COUNT(*) AS n FROM tasks GROUP BY status')}
        except sqlite3.OperationalError as e:
            if 'no such table' not in str(e).lower():
                raise
            task_counts = {}
        recent_alerts = conn.execute('SELECT COUNT(*) FROM alerts WHERE created_at >= ?',
            ((datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec='seconds'),)).fetchone()[0]
    by_region = {}
    by_service = {}
    states = {}
    for item in resources:
        for grouping, key in ((by_region, item['region']), (by_service, item['service']), (states, item['state'])):
            grouping[key] = grouping.get(key, 0) + 1
    return {'profile':profile,'region':region,'service':service,
            'total':len(resources),'running':sum(1 for i in resources if i['state']=='running'),
            'stopped':sum(1 for i in resources if i['state']=='stopped'),
            'by_region':by_region,'by_service':by_service,'by_state':states,
            'task_counts':task_counts,'alerts_24h':recent_alerts,'errors':data['errors'],
            'generated_at':datetime.now(timezone.utc).isoformat(timespec='seconds')}



@app.post('/api/login')
def login(data: Login, request: Request, response: Response):
    ip = ip_of(request)
    now = int(time.time())
    with db() as c:
        old = c.execute('SELECT * FROM attempts WHERE ip=?', (ip,)).fetchone()
        if old and old['locked_until'] > now:
            raise HTTPException(429, 'Too many attempts. Try again later')
        if not secrets.compare_digest(data.password.encode(), PASSWORD.encode()):
            count = 1 if not old or now - old['window_start'] > 900 else old['count'] + 1
            start = now if count == 1 else old['window_start']
            locked = now + 900 if count >= 5 else 0
            c.execute('INSERT OR REPLACE INTO attempts VALUES(?,?,?,?)', (ip, count, start, locked))
            raise HTTPException(401, 'Invalid password')
        c.execute('DELETE FROM attempts WHERE ip=?', (ip,))
        c.execute('DELETE FROM sessions WHERE expiry<?', (now,))
        token, csrf = secrets.token_urlsafe(36), secrets.token_urlsafe(32)
        c.execute('INSERT INTO sessions VALUES(?,?,?)', (digest(token), csrf, now + TTL))
    response.set_cookie('awscc_session', token, max_age=TTL, httponly=True, secure=COOKIE_SECURE, samesite='strict', path='/')
    audit(ip, 'login', detail='Login accepted')
    return {'ok': True, 'csrf': csrf}


@app.get('/api/session', dependencies=[Depends(auth)])
def session(request: Request):
    with db() as c:
        row = c.execute('SELECT csrf FROM sessions WHERE token_hash=?', (digest(request.cookies['awscc_session']),)).fetchone()
    return {'csrf': row['csrf']}


@app.post('/api/logout', dependencies=[Depends(auth)])
def logout(request: Request, response: Response):
    with db() as c:
        c.execute('DELETE FROM sessions WHERE token_hash=?', (digest(request.cookies['awscc_session']),))
    response.delete_cookie('awscc_session', path='/')
    return {'ok': True}


@app.get('/api/config', dependencies=[Depends(auth)])
def config():
    return {'profiles': available_profiles(), 'active_profile': active_profile(), 'regions': REGIONS}


@app.get('/api/accounts', dependencies=[Depends(auth)])
def list_accounts():
    return {'items': [{'alias': x['alias'], 'source_profile': x['source_profile'], 'role_arn': x['role_arn']} for x in accounts()]}


@app.post('/api/accounts', dependencies=[Depends(auth)])
def create_account(item: RoleAccount, request: Request):
    if not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9_-]{1,39}', item.alias):
        raise HTTPException(400, 'Invalid alias')
    if item.alias in available_profiles():
        raise HTTPException(409, 'Alias conflicts with an AWS profile')
    if item.source_profile not in base_profiles():
        raise HTTPException(400, 'Source profile must exist')
    if not re.fullmatch(r'arn:aws[a-zA-Z-]*:iam::\d{12}:role/[\w+=,.@/-]+', item.role_arn):
        raise HTTPException(400, 'Invalid IAM role ARN')
    try:
        with db() as c:
            c.execute('INSERT INTO accounts VALUES(?,?,?,?,?)', (item.alias, item.source_profile,
                      item.role_arn, item.external_id, datetime.now(timezone.utc).isoformat()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Account alias already exists')
    audit(ip_of(request), 'account', profile=item.alias, operation='create')
    return {'ok': True}


@app.delete('/api/accounts/{alias}', dependencies=[Depends(auth)])
def remove_account(alias: str, request: Request):
    with db() as c:
        count = c.execute('DELETE FROM accounts WHERE alias=?', (alias,)).rowcount
    if not count:
        raise HTTPException(404, 'Account not found')
    audit(ip_of(request), 'account', profile=alias, operation='delete')
    return {'ok': True}


@app.get('/api/key-accounts', dependencies=[Depends(auth)])
def list_key_accounts():
    # Deliberately never expose full keys or encrypted secrets.
    return {'items': [{**{k: x[k] for k in ('alias','aws_account_id','arn','created_at')},
                      'access_key_tail': x['access_key_id'][-4:]} for x in saved_keys()],
            'active_profile': active_profile()}


@app.post('/api/key-accounts', dependencies=[Depends(auth)])
def create_key_account(item: KeyAccountIn, request: Request):
    if not re.fullmatch(r'[A-Za-z][A-Za-z0-9_-]{1,39}', item.alias):
        raise HTTPException(400, 'Invalid account alias')
    if item.alias in available_profiles():
        raise HTTPException(409, 'Account alias already exists')
    if not re.fullmatch(r'[A-Z0-9]{16,128}', item.access_key_id):
        raise HTTPException(400, 'Invalid Access Key ID format')
    # Validate keys with AWS STS before saving them; only mask errors to avoid leakage.
    try:
        identity = boto3.Session(aws_access_key_id=item.access_key_id,
                                 aws_secret_access_key=item.secret_access_key).client(
                                     'sts', region_name='us-east-1', config=AWS_CONFIG).get_caller_identity()
    except (ClientError, BotoCoreError, OSError) as exc:
        audit(ip_of(request), 'credential', profile=item.alias, operation='create', result='failed',
              detail=aws_error(exc))
        raise HTTPException(400, 'AWS credential validation failed; verify key, IAM status and network')
    encrypted = vault().encrypt(item.secret_access_key.encode('utf-8'))
    try:
        with db() as c:
            c.execute('''INSERT INTO key_accounts(alias,access_key_id,secret_encrypted,aws_account_id,arn,created_at)
                         VALUES(?,?,?,?,?,?)''', (item.alias,item.access_key_id,encrypted,
                          identity['Account'],identity['Arn'],datetime.now(timezone.utc).isoformat()))
    except sqlite3.IntegrityError:
        raise HTTPException(409, 'Account alias already exists')
    audit(ip_of(request), 'credential', profile=item.alias, operation='create',
          detail='Saved encrypted key; STS verified account '+identity['Account'])
    return {'ok':True, 'alias':item.alias, 'aws_account_id':identity['Account'], 'arn':identity['Arn']}


@app.post('/api/key-accounts/{alias}/verify', dependencies=[Depends(auth)])
def verify_key_account(alias: str, request: Request):
    if alias not in {x['alias'] for x in saved_keys()}:
        raise HTTPException(404, 'Saved credential not found')
    try:
        identity = aws_session(alias).client('sts',region_name='us-east-1',config=AWS_CONFIG).get_caller_identity()
    except (ClientError, BotoCoreError, OSError) as exc:
        audit(ip_of(request), 'credential', profile=alias, operation='verify', result='failed', detail=aws_error(exc))
        raise HTTPException(502, 'AWS STS verification failed')
    audit(ip_of(request), 'credential', profile=alias, operation='verify')
    return {'alias':alias, 'aws_account_id':identity['Account'],'arn':identity['Arn']}


@app.post('/api/key-accounts/activate', dependencies=[Depends(auth)])
def activate_key_account(item: ActivateAccountIn, request: Request):
    # Activation is server-persisted; existing in-flight operations keep their explicit profile.
    valid_profile(item.alias)
    with db() as c:
        c.execute('''INSERT INTO app_settings(name,value) VALUES('active_profile',?)
                     ON CONFLICT(name) DO UPDATE SET value=excluded.value''',(item.alias,))
    audit(ip_of(request), 'account', profile=item.alias, operation='activate')
    return {'ok':True,'active_profile':item.alias}


@app.delete('/api/key-accounts/{alias}', dependencies=[Depends(auth)])
def delete_key_account(alias: str, request: Request):
    with db() as c:
        if c.execute('SELECT 1 FROM monitors WHERE profile=? LIMIT 1',(alias,)).fetchone():
            raise HTTPException(409, 'Remove monitoring rules for this account first')
        # Existing queued work must not silently run on a removed account.
        try:
            if c.execute("SELECT 1 FROM tasks WHERE profile=? AND status IN ('queued','running') LIMIT 1",(alias,)).fetchone():
                raise HTTPException(409, 'Cancel or finish queued tasks before removing account')
        except sqlite3.OperationalError as e:
            if 'no such table' not in str(e):
                raise
        count = c.execute('DELETE FROM key_accounts WHERE alias=?',(alias,)).rowcount
        if not count:
            raise HTTPException(404, 'Saved credential not found')
        c.execute("UPDATE app_settings SET value='default' WHERE name='active_profile' AND value=?",(alias,))
    audit(ip_of(request), 'credential', profile=alias, operation='delete')
    return {'ok':True, 'active_profile':active_profile()}


@app.get('/api/identity', dependencies=[Depends(auth)])
def identity(profile: str = 'default'):
    try:
        return aws_session(profile).client('sts', config=AWS_CONFIG).get_caller_identity()
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))


def scan_one(session, profile, region, service):
    rows = []
    failures = []
    if service in ('all', 'lightsail'):
        try:
            api = session.client('lightsail', region_name=region, config=AWS_CONFIG)
            token = None
            while True:
                data = api.get_instances(**({'pageToken': token} if token else {}))
                for x in data.get('instances', []):
                    rows.append({'service': 'lightsail', 'profile': profile, 'region': region,
                                 'id': x['name'], 'name': x['name'], 'state': x.get('state', {}).get('name', 'unknown'),
                                 'ip': x.get('publicIpAddress', ''), 'private_ip': x.get('privateIpAddress', ''),
                                 'type': x.get('bundleId', ''), 'zone': x.get('location', {}).get('availabilityZone', '')})
                token = data.get('nextPageToken')
                if not token:
                    break
        except (ClientError, BotoCoreError) as e:
            failures.append({'region': region, 'service': 'lightsail', 'message': aws_error(e)})
    if service in ('all', 'ec2'):
        try:
            api = session.client('ec2', region_name=region, config=AWS_CONFIG)
            for page in api.get_paginator('describe_instances').paginate():
                for reservation in page.get('Reservations', []):
                    for x in reservation.get('Instances', []):
                        name = next((tag['Value'] for tag in x.get('Tags', []) if tag.get('Key') == 'Name'), x['InstanceId'])
                        rows.append({'service': 'ec2', 'profile': profile, 'region': region,
                                     'id': x['InstanceId'], 'name': name, 'state': x.get('State', {}).get('Name', 'unknown'),
                                     'ip': x.get('PublicIpAddress', ''), 'private_ip': x.get('PrivateIpAddress', ''),
                                     'type': x.get('InstanceType', ''), 'zone': x.get('Placement', {}).get('AvailabilityZone', '')})
        except (ClientError, BotoCoreError) as e:
            failures.append({'region': region, 'service': 'ec2', 'message': aws_error(e)})
    return rows, failures


@app.get('/api/instances', dependencies=[Depends(auth)])
def instances(profile: str = 'default', service: str = 'all', region: str = 'all'):
    if service not in ('all', 'lightsail', 'ec2'):
        raise HTTPException(400, 'Unsupported service')
    chosen = REGIONS if region == 'all' else [region]
    for r in chosen:
        check_region(r)
    try:
        session = aws_session(profile)  # AssumeRole once per inventory request.
        rows, failures = [], []
        with ThreadPoolExecutor(max_workers=min(6, len(chosen))) as pool:
            jobs = {pool.submit(scan_one, session, profile, r, service): r for r in chosen}
            for job in as_completed(jobs):
                try:
                    result, errs = job.result()
                    rows.extend(result)
                    failures.extend(errs)
                except Exception as e:
                    logger.exception('Unexpected inventory error')
                    failures.append({'region': jobs[job], 'service': service, 'message': aws_error(e)})
        return {'items': sorted(rows, key=lambda x: (x['region'], x['service'], x['name'])), 'errors': failures}
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))


def invoke_action(data: InstanceAction):
    check_region(data.region)
    if data.action not in ('start', 'stop', 'reboot') or data.service not in ('lightsail', 'ec2'):
        raise HTTPException(400, 'Unsupported action/service')
    api = client(data.profile, data.service, data.region)
    if data.service == 'ec2':
        if not re.fullmatch(r'i-[0-9a-f]{8,17}', data.instance):
            raise HTTPException(400, 'Invalid EC2 instance ID')
        fn = {'start': 'start_instances', 'stop': 'stop_instances', 'reboot': 'reboot_instances'}[data.action]
        getattr(api, fn)(InstanceIds=[data.instance])
    else:
        fn = {'start': 'start_instance', 'stop': 'stop_instance', 'reboot': 'reboot_instance'}[data.action]
        getattr(api, fn)(instanceName=data.instance)


@app.post('/api/instance/action', dependencies=[Depends(auth)])
def action(data: InstanceAction, request: Request):
    info = dict(ip=ip_of(request), category='instance', profile=data.profile, region=data.region,
                service=data.service, resource=data.instance, operation=data.action)
    try:
        invoke_action(data)
        audit(**info, result='submitted')
        return {'ok': True, 'message': 'AWS accepted the operation'}
    except (ClientError, BotoCoreError, HTTPException) as e:
        message = e.detail if isinstance(e, HTTPException) else aws_error(e)
        audit(**info, result='failed', detail=str(message))
        if isinstance(e, HTTPException):
            raise
        raise HTTPException(502, message)


@app.post('/api/instance/batch', dependencies=[Depends(auth)])
def batch_action(data: BatchAction, request: Request):
    if len({(x.profile, x.region, x.service, x.instance) for x in data.items}) != len(data.items):
        raise HTTPException(400, 'Duplicate instances in batch')
    # Sequential and bounded to avoid sudden high-volume state changes.
    results = []
    for x in data.items:
        info = dict(ip=ip_of(request), category='instance', profile=x.profile, region=x.region,
                    service=x.service, resource=x.instance, operation=x.action)
        try:
            invoke_action(x)
            result, detail = 'submitted', ''
        except (ClientError, BotoCoreError, HTTPException) as e:
            result = 'failed'
            detail = str(e.detail) if isinstance(e, HTTPException) else aws_error(e)
        audit(**info, result=result, detail=detail)
        results.append({'instance': x.instance, 'region': x.region, 'service': x.service, 'result': result, 'message': detail})
    return {'items': results, 'submitted': sum(x['result'] == 'submitted' for x in results)}


@app.get('/api/traffic', dependencies=[Depends(auth)])
def traffic(profile: str, region: str, instance: str, days: int = 7, service: str = 'lightsail'):
    check_region(region)
    if not 1 <= days <= 30 or not 1 <= len(instance) <= 200:
        raise HTTPException(400, 'Invalid traffic parameters')
    if service not in ('lightsail', 'ec2'):
        raise HTTPException(400, 'Invalid traffic service')
    end = datetime.now(timezone.utc)
    start = end - timedelta(days=days)
    try:
        api = client(profile, 'lightsail' if service == 'lightsail' else 'cloudwatch', region)
        out = {}
        # Lightsail CloudWatch metrics NetworkIn/NetworkOut return bytes per period.
        for metric in ('NetworkIn', 'NetworkOut'):
            if service == 'lightsail':
                data = api.get_instance_metric_data(instanceName=instance, metricName=metric,
                     period=3600, startTime=start, endTime=end, unit='Bytes', statistics=['Sum'])
                points = data.get('metricData', [])
            else:
                data = api.get_metric_statistics(Namespace='AWS/EC2', MetricName=metric,
                    Dimensions=[{'Name': 'InstanceId', 'Value': instance}], Period=3600,
                    StartTime=start, EndTime=end, Unit='Bytes', Statistics=['Sum'])
                points = [{'timestamp': p['Timestamp'], 'sum': p.get('Sum', 0)} for p in data.get('Datapoints', [])]
            points = sorted(points, key=lambda p: p['timestamp'])
            out[metric] = {'bytes': round(sum(float(p.get('sum', 0) or 0) for p in points)),
                           'points': [{'time': p['timestamp'].astimezone(timezone.utc).isoformat(),
                                      'bytes': float(p.get('sum', 0) or 0)} for p in points]}
        return {'instance': instance, 'region': region, 'profile': profile, 'days': days, 'service': service,
                'start': start.isoformat(), 'end': end.isoformat(), 'metrics': out,
                'note': 'CloudWatch sample sums only. Not Lightsail monthly billable transfer or remaining quota.'}
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))


@app.get('/api/audit', dependencies=[Depends(auth)])
def audit_list(limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(400, 'Limit must be 1..500')
    with db() as c:
        return {'items': [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (limit,))]}


@app.get('/api/cost', dependencies=[Depends(auth)])
def costs(profile: str = 'default'):
    today = date.today()
    start = today.replace(day=1).isoformat()
    end = (today + timedelta(days=1)).isoformat()
    try:
        api = client(profile, 'ce', 'us-east-1')
        data = api.get_cost_and_usage(TimePeriod={'Start': start, 'End': end}, Granularity='MONTHLY',
                                      Metrics=['UnblendedCost'], GroupBy=[{'Type': 'DIMENSION', 'Key': 'SERVICE'}])
        groups = data.get('ResultsByTime', [{}])[0].get('Groups', [])
        items = [{'name': x['Keys'][0], 'amount': round(float(x['Metrics']['UnblendedCost']['Amount']), 4)} for x in groups]
        return {'currency': 'USD', 'month': start[:7], 'items': sorted(items, key=lambda x: -x['amount']),
                'total': round(sum(x['amount'] for x in items), 4)}
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))


# V3 monitor configuration is intentionally explicit, to avoid billing and API surprises.
@app.get('/api/monitors', dependencies=[Depends(auth)])
def list_monitors():
    with db() as c:
        rows = [dict(x) for x in c.execute('SELECT * FROM monitors ORDER BY id DESC')]
    for x in rows:
        x['enabled'] = bool(x['enabled'])
    return {'items': rows}


@app.post('/api/monitors', dependencies=[Depends(auth)])
def save_monitor(item: MonitorIn, request: Request):
    check_region(item.region)
    valid_profile(item.profile)
    # Creating a watch doesn't modify instances, but validate it exists and is Lightsail.
    try:
        api = client(item.profile, 'lightsail', item.region)
        api.get_instance(instanceName=item.instance)
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))
    now = datetime.now(timezone.utc).isoformat(timespec='seconds')
    with db() as c:
        c.execute('''INSERT INTO monitors(profile,region,instance,threshold_gib,enabled,created_at)
                     VALUES(?,?,?,?,?,?) ON CONFLICT(profile,region,instance) DO UPDATE SET
                     threshold_gib=excluded.threshold_gib,enabled=excluded.enabled''',
                  (item.profile, item.region, item.instance, item.threshold_gib, int(item.enabled), now))
    audit(ip_of(request), 'monitor', profile=item.profile, region=item.region,
          resource=item.instance, operation='upsert', detail='One-hour network sample threshold configured')
    return {'ok': True}


@app.delete('/api/monitors/{monitor_id}', dependencies=[Depends(auth)])
def delete_monitor(monitor_id: int, request: Request):
    with db() as c:
        row = c.execute('SELECT * FROM monitors WHERE id=?', (monitor_id,)).fetchone()
        if row:
            c.execute('DELETE FROM monitors WHERE id=?', (monitor_id,))
    if not row:
        raise HTTPException(404, 'Monitor not found')
    audit(ip_of(request), 'monitor', profile=row['profile'], region=row['region'],
          resource=row['instance'], operation='delete')
    return {'ok': True}


@app.get('/api/monitor/samples', dependencies=[Depends(auth)])
def monitor_samples(monitor_id: int, limit: int = 96):
    if not 1 <= limit <= 500:
        raise HTTPException(400, 'Limit must be between 1 and 500')
    with db() as c:
        if not c.execute('SELECT 1 FROM monitors WHERE id=?', (monitor_id,)).fetchone():
            raise HTTPException(404, 'Monitor not found')
        rows = [dict(x) for x in c.execute(
            'SELECT * FROM monitor_samples WHERE monitor_id=? ORDER BY measured_at DESC LIMIT ?',
            (monitor_id,limit))]
    return {'items': rows}


@app.get('/api/alerts', dependencies=[Depends(auth)])
def list_alerts(limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(400, 'Limit must be between 1 and 500')
    with db() as c:
        rows = [dict(x) for x in c.execute('''SELECT a.*,m.profile,m.region,m.instance
            FROM alerts a LEFT JOIN monitors m ON m.id=a.monitor_id
            ORDER BY a.id DESC LIMIT ?''', (limit,))]
    return {'items': rows}


@app.post('/api/ip/check', dependencies=[Depends(auth)])
def ip_check(data: IpCheck, request: Request):
    # Resolve the destination only through AWS inventory. Never accept arbitrary hosts/ports.
    check_region(data.region)
    if data.port not in (22, 80, 443):
        raise HTTPException(400, 'Port must be 22, 80 or 443')
    try:
        instance = client(data.profile, 'lightsail', data.region).get_instance(
            instanceName=data.instance)['instance']
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))
    address = instance.get('publicIpAddress', '')
    try:
        parsed = ipaddress.ip_address(address)
        if not parsed.is_global or parsed.version != 4:
            raise ValueError('Not public IPv4')
    except ValueError:
        raise HTTPException(400, 'Instance has no globally routable public IPv4')
    began = time.monotonic()
    try:
        with socket.create_connection((address, data.port), timeout=2.5):
            reachable = True
            error = ''
    except OSError as e:
        reachable = False
        error = type(e).__name__
    elapsed = round((time.monotonic() - began) * 1000)
    with db() as conn:
        conn.execute('''INSERT INTO ip_checks(checked_at,profile,region,instance,address,port,reachable,latency_ms,error)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                     (datetime.now(timezone.utc).isoformat(timespec='seconds'), data.profile, data.region,
                      data.instance, address, data.port, int(reachable), elapsed, error))
    audit(ip_of(request), 'ipcheck', profile=data.profile, region=data.region,
          service='lightsail', resource=data.instance, operation=str(data.port),
          result='open' if reachable else 'closed', detail=error)
    return {'address': address, 'port': data.port, 'tcp_reachable': reachable,
            'latency_ms': elapsed, 'error': error,
            'note': 'TCP connect from panel server only; not an ICMP or geo-distributed reachability test.'}


# V4: Lightsail static IP operations. Explicit confirmation for destructive release.
@app.get('/api/lightsail/static-ips', dependencies=[Depends(auth)])
def static_ips(profile: str = 'default', region: str = ''):
    check_region(region)
    try:
        api = client(profile, 'lightsail', region)
        items, token = [], None
        for _ in range(30):
            kw = {'pageToken': token} if token else {}
            result = api.get_static_ips(**kw)
            for x in result.get('staticIps', []):
                items.append({'name': x.get('name', ''), 'ip': x.get('ipAddress', ''),
                              'attached_to': x.get('attachedTo', ''), 'is_attached': bool(x.get('isAttached', False))})
            token = result.get('nextPageToken')
            if not token:
                break
        else:
            raise HTTPException(502, 'Static IP pagination exceeded 30 pages')
        return {'items': items}
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))


@app.post('/api/lightsail/static-ip/action', dependencies=[Depends(auth)])
def static_ip_action(data: StaticIpAction, request: Request):
    check_region(data.region)
    if data.action not in ('allocate', 'attach', 'detach', 'release'):
        raise HTTPException(400, 'Invalid static IP action')
    if data.action == 'attach' and not data.instance:
        raise HTTPException(400, 'Attach requires instance')
    # A release is destructive; refuse unless exact resource name supplied twice.
    if data.action == 'release' and data.confirm != data.name:
        raise HTTPException(400, 'Release requires exact static IP name confirmation')
    # Name is an AWS name, never a user-supplied destination address.
    try:
        api = client(data.profile, 'lightsail', data.region)
        if data.action == 'allocate':
            api.allocate_static_ip(staticIpName=data.name)
        elif data.action == 'attach':
            # Validate target exists in same account/region before attaching.
            api.get_instance(instanceName=data.instance)
            api.attach_static_ip(staticIpName=data.name, instanceName=data.instance)
        elif data.action == 'detach':
            api.detach_static_ip(staticIpName=data.name)
        else:
            # Never silently detach/release an in-use address.
            current = api.get_static_ip(staticIpName=data.name)['staticIp']
            if current.get('isAttached') or current.get('attachedTo'):
                raise HTTPException(409, 'Static IP is still attached; detach it first')
            api.release_static_ip(staticIpName=data.name)
    except (ClientError, BotoCoreError) as e:
        audit(ip_of(request), 'static_ip', profile=data.profile, region=data.region,
              service='lightsail', resource=data.name, operation=data.action,
              result='failed', detail=aws_error(e))
        raise HTTPException(502, aws_error(e))
    audit(ip_of(request), 'static_ip', profile=data.profile, region=data.region,
          service='lightsail', resource=data.name, operation=data.action,
          detail=('target='+data.instance if data.instance else 'submitted'))
    return {'ok': True, 'submitted': True, 'action': data.action, 'name': data.name}


@app.get('/api/ip/history', dependencies=[Depends(auth)])
def ip_history(profile: str = '', region: str = '', instance: str = '', limit: int = 100):
    if not 1 <= limit <= 500:
        raise HTTPException(400, 'Limit must be 1..500')
    query = 'SELECT * FROM ip_checks WHERE 1=1'
    params = []
    for col, val in [('profile', profile), ('region', region), ('instance', instance)]:
        if val:
            query += f' AND {col}=?'
            params.append(val)
    with db() as c:
        rows = [dict(r) for r in c.execute(query+' ORDER BY id DESC LIMIT ?', [*params, limit])]
    for item in rows:
        item['reachable'] = bool(item.pop('reachable'))
    return {'items': rows}


@app.get('/api/lightsail/allowance', dependencies=[Depends(auth)])
def lightsail_allowance(profile: str, region: str, instance: str):
    check_region(region)
    try:
        api = client(profile, 'lightsail', region)
        inst = api.get_instance(instanceName=instance)['instance']
        bundle_id = inst.get('bundleId')
        if not bundle_id:
            raise HTTPException(404, 'Instance bundle ID unavailable')
        token, bundle = None, None
        for _ in range(30):
            kw = {'pageToken': token} if token else {}
            result = api.get_bundles(**kw)
            bundle = next((b for b in result.get('bundles', []) if b.get('bundleId') == bundle_id), None)
            if bundle or not result.get('nextPageToken'):
                break
            token = result['nextPageToken']
        if not bundle:
            raise HTTPException(404, 'Bundle metadata unavailable or retired')
        return {'instance': instance, 'bundle_id': bundle_id,
                'transfer_per_month_gb': bundle.get('transferPerMonthInGb'),
                'monthly_price_usd': bundle.get('price'),
                'note': 'Plan metadata only. NOT actual AWS billable transfer, usage or remaining allowance. Region-dependent charging and Lightsail rules may apply.'}
    except (ClientError, BotoCoreError) as e:
        raise HTTPException(502, aws_error(e))


from taskqueue import CreateBatch

# V6 durable task endpoints. The task worker is an independent process.
@app.post('/api/tasks/lightsail/create', dependencies=[Depends(auth)])
def create_lightsail_task(request: Request, batch: 'CreateBatch'):
    from taskqueue import submit
    return submit(batch, actor=ip_of(request))


@app.get('/api/tasks', dependencies=[Depends(auth)])
def tasks_list(limit: int = 30):
    from taskqueue import list_tasks
    return list_tasks(limit)


@app.get('/api/tasks/{task_id}', dependencies=[Depends(auth)])
def tasks_detail(task_id: str):
    from taskqueue import detail
    return detail(task_id)


@app.post('/api/tasks/{task_id}/cancel', dependencies=[Depends(auth)])
def tasks_cancel(task_id: str, request: Request):
    from taskqueue import cancel
    return cancel(task_id, actor=ip_of(request))


@app.get('/api/lightsail/catalog', dependencies=[Depends(auth)])
def lightsail_catalog(profile: str, region: str):
    from taskqueue import catalog
    return catalog(profile, region)


@app.post('/api/tasks/{task_id}/verify', dependencies=[Depends(auth)])
def tasks_verify(task_id: str, request: Request):
    from taskqueue import verify_task
    return verify_task(task_id, actor=ip_of(request))
