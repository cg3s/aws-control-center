"""V7 durable bounded Lightsail create queue. Single task worker recommended.

Creation calls are intentionally NOT automatically retried: an AWS timeout may mean
that AWS created a resource despite the client receiving an exception.
"""
import json
import base64
import os
import re
import shlex
import sqlite3
import time
from datetime import datetime, timezone
from typing import Optional
from uuid import uuid4

from botocore.exceptions import BotoCoreError, ClientError
from fastapi import HTTPException
from pydantic import BaseModel, Field
from passlib.hash import sha512_crypt
from cryptography.exceptions import UnsupportedAlgorithm

from app import audit, check_region, client, db, valid_profile, vault

MAX_BATCH = 10
MAX_PENDING = 100
NAME_PATTERN = re.compile(r'^[a-zA-Z0-9][a-zA-Z0-9-]{1,62}$')


class CreateBatch(BaseModel):
    profile: str = Field(min_length=1, max_length=80)
    region: str
    names: list[str] = Field(min_length=1, max_length=MAX_BATCH)
    bundle_id: str = Field(min_length=1, max_length=100)
    blueprint_id: str = Field(min_length=1, max_length=100)
    availability_zone: str = Field(default='', max_length=100)
    dry_run: bool = True
    confirm: str = ''
    user_data: str = Field(default='', max_length=40000)
    key_pair_name: str = Field(default='', max_length=255)
    ssh_public_key: str = Field(default='', max_length=12000)
    root_password: str = Field(default='', max_length=256, repr=False)
    enable_root_ssh: bool = False


def launch_script(batch: CreateBatch):
    """Generate Linux cloud-init user data without embedding a plaintext password."""
    if batch.root_password and (len(batch.root_password) < 16 or any(c in batch.root_password for c in '\r\n\x00')):
        raise HTTPException(400, 'Root password must be at least 16 characters without newlines or NUL')
    keys = [line.strip() for line in batch.ssh_public_key.splitlines() if line.strip()]
    from cryptography.hazmat.primitives.serialization import load_ssh_public_key
    for key in keys:
        try:
            load_ssh_public_key(key.encode())
        except (ValueError, TypeError, UnsupportedAlgorithm) as exc:
            raise HTTPException(400, 'Invalid OpenSSH public key; paste a public key, not a private key') from exc
    if batch.enable_root_ssh and not (keys or batch.root_password):
        raise HTTPException(400, 'Root SSH requires a root public key or root password')
    if keys and not batch.enable_root_ssh:
        raise HTTPException(400, 'Enable root SSH to install root public keys, or use a Lightsail key pair for the default user')
    config = {'users': ['default'], 'runcmd': []}
    if batch.root_password or keys or batch.enable_root_ssh:
        root = {'name': 'root'}
        if keys:
            root['ssh_authorized_keys'] = keys
        if batch.root_password:
            root.update(hashed_passwd=sha512_crypt.using(rounds=100000).hash(batch.root_password), lock_passwd=False)
        config['users'].append(root)
    if batch.enable_root_ssh:
        config['disable_root'] = False
        if batch.root_password:
            config['ssh_pwauth'] = True
        # Put global options before Include/Match: OpenSSH uses the first value.
        options = 'PermitRootLogin yes\nPubkeyAuthentication yes\n'
        if batch.root_password:
            options += 'PasswordAuthentication yes\n'
        command = "set -eu\ncp -p /etc/ssh/sshd_config /etc/ssh/sshd_config.awscc-backup\n"
        command += "{ printf %s " + shlex.quote(options) + "; cat /etc/ssh/sshd_config.awscc-backup; } > /etc/ssh/sshd_config\n"
        command += 'if ! /usr/sbin/sshd -t; then cp -p /etc/ssh/sshd_config.awscc-backup /etc/ssh/sshd_config; exit 1; fi\n'
        command += '(systemctl reload ssh || systemctl reload sshd)\n'
        config['runcmd'].append(['sh', '-c', command])
    if batch.user_data:
        script = base64.b64encode(batch.user_data.replace('\r\n', '\n').encode()).decode()
        config['runcmd'].append(['bash', '-c', f'printf %s {shlex.quote(script)} | base64 -d > /var/lib/cloud/awscc-launch.sh && chmod 700 /var/lib/cloud/awscc-launch.sh && bash /var/lib/cloud/awscc-launch.sh'])
    if not config['runcmd'] and len(config['users']) == 1:
        return ''
    result = '#cloud-config\n' + json.dumps(config)
    if len(result.encode()) > 65536:
        raise HTTPException(400, 'Combined launch configuration exceeds 64 KiB')
    return result


def now():
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def initialize():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS tasks (
            id TEXT PRIMARY KEY, kind TEXT NOT NULL, profile TEXT NOT NULL, region TEXT NOT NULL,
            status TEXT NOT NULL, payload TEXT NOT NULL, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, cancel_requested INTEGER NOT NULL DEFAULT 0,
            estimate_usd REAL NOT NULL DEFAULT 0, error TEXT NOT NULL DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS task_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL,
            resource_name TEXT NOT NULL, status TEXT NOT NULL,
            attempted_at TEXT, finished_at TEXT, error TEXT NOT NULL DEFAULT '',
            FOREIGN KEY(task_id) REFERENCES tasks(id), UNIQUE(task_id,resource_name))''')
        # Additive migration: prior V6 data and item state are preserved.
        existing = {row['name'] for row in c.execute('PRAGMA table_info(task_items)')}
        for col, typ in [('verification',"TEXT NOT NULL DEFAULT 'unchecked'"),
                         ('verified_at','TEXT'), ('verification_detail',"TEXT NOT NULL DEFAULT ''")]:
            if col not in existing:
                c.execute(f'ALTER TABLE task_items ADD COLUMN {col} {typ}')
        c.execute('CREATE INDEX IF NOT EXISTS task_order ON tasks(created_at DESC)')
        c.execute('CREATE INDEX IF NOT EXISTS task_item_status ON task_items(task_id,status)')


def catalog_pages(method, key, **kwargs):
    values, token = [], None
    for _ in range(20):
        response = method(**kwargs, **({'pageToken': token} if token else {}))
        values.extend(response.get(key, []))
        token = response.get('nextPageToken')
        if not token:
            return values
    raise HTTPException(502, 'Catalog exceeds pagination safety limit')


def catalog(profile: str, region: str):
    """Read-only regional Lightsail catalog. Never assume IDs are globally valid."""
    check_region(region)
    valid_profile(profile)
    aws = client(profile, 'lightsail', region)
    bundles = catalog_pages(aws.get_bundles, 'bundles', includeInactive=False)
    blueprints = catalog_pages(aws.get_blueprints, 'blueprints', includeInactive=False)
    regions = aws.get_regions(includeAvailabilityZones=True).get('regions', [])
    selected = next((x for x in regions if x.get('name') == region), {})
    return {'profile':profile, 'region':region,
            'bundles':[{'id':b.get('bundleId'), 'name':b.get('name'), 'price_usd_per_month':b.get('price'),
                        'ram_gb':b.get('ramSizeInGb'), 'cpu_count':b.get('cpuCount'),
                        'transfer_gb':b.get('transferPerMonthInGb')}
                       for b in bundles if b.get('isActive', True) and b.get('bundleId')],
            'blueprints':[{'id':b.get('blueprintId'), 'name':b.get('name'), 'version':b.get('version')}
                          for b in blueprints if b.get('isActive', True) and b.get('blueprintId')],
            'zones':[z.get('zoneName') for z in selected.get('availabilityZones',[]) if z.get('zoneName')]}


def verify_task(identifier: str, actor='web'):
    """Read AWS instance state; never retry or mutate a create operation.

    Verification is a separately recorded observation, not a billable success guarantee.
    """
    task = detail(identifier)
    # Only verify requests that may already have reached AWS. Failed includes ambiguous timeouts.
    candidates = [x for x in task['items'] if x['status'] in ('submitted','failed')]
    if not candidates:
        return detail(identifier)
    api = client(task['profile'], 'lightsail', task['region'])
    for item in candidates:
        name = item['resource_name']
        try:
            found = api.get_instance(instanceName=name).get('instance', {})
            state = str(found.get('state', {}).get('name') or 'unknown').lower()
            verification = 'running' if state == 'running' else 'found'
            message = state[:120]
        except ClientError as exc:
            code = exc.response.get('Error',{}).get('Code','ClientError')
            http = exc.response.get('ResponseMetadata',{}).get('HTTPStatusCode')
            if code in ('NotFoundException','ServiceException') and http == 404:
                verification, message = 'not_found', 'Not found at verification time; eventual consistency is possible'
            else:
                verification, message = 'unknown', code[:120]
        except (BotoCoreError, OSError) as exc:
            verification, message = 'unknown', type(exc).__name__
        with db() as conn:
            conn.execute("UPDATE task_items SET verification=?,verified_at=?,verification_detail=? WHERE task_id=? AND resource_name=?",
                         (verification,now(),message[:250],identifier,name))
    audit(actor,'task',profile=task['profile'],region=task['region'],service='lightsail',
          resource=identifier,operation='verify',result='checked',detail=f'{len(candidates)} items')
    return detail(identifier)


def validate(batch: CreateBatch):
    check_region(batch.region)
    valid_profile(batch.profile)
    if any(not NAME_PATTERN.fullmatch(x) for x in batch.names):
        raise HTTPException(400, 'Names must be 2-63 letters/digits/hyphens, starting alphanumeric')
    if len(set(batch.names)) != len(batch.names):
        raise HTTPException(400, 'Duplicate instance names')
    if batch.availability_zone and not re.fullmatch(re.escape(batch.region) + r'[a-z]', batch.availability_zone):
        raise HTTPException(400, 'Availability zone must belong to selected region')
    if batch.confirm and batch.confirm != 'CREATE':
        raise HTTPException(400, 'Confirmation must be CREATE')
    aws = client(batch.profile, 'lightsail', batch.region)
    # AWS supplies region-specific bundle and blueprint catalogs.
    bundles = catalog_pages(aws.get_bundles, 'bundles', includeInactive=False)
    bundle = next((b for b in bundles if b.get('bundleId') == batch.bundle_id and b.get('isActive', True)), None)
    if bundle is None:
        raise HTTPException(400, 'Bundle ID is not available in this region')
    blueprints = catalog_pages(aws.get_blueprints, 'blueprints', includeInactive=False)
    blueprint = next((b for b in blueprints if b.get('blueprintId') == batch.blueprint_id and b.get('isActive', True)), None)
    if blueprint is None:
        raise HTTPException(400, 'Blueprint ID is not available in this region')
    if (batch.user_data or batch.ssh_public_key or batch.root_password or batch.enable_root_ssh) and (
            blueprint.get('platform') == 'WINDOWS' or 'windows' in batch.blueprint_id.lower()):
        raise HTTPException(400, 'Launch scripts and root configuration require a Linux/Unix blueprint')
    estimated = round(float(bundle.get('price') or 0) * len(batch.names), 2)
    return aws, estimated


def submit(batch: CreateBatch, actor='web'):
    if batch.dry_run and batch.confirm:
        raise HTTPException(400, 'Confirmation requires dry_run=false; request was not submitted')
    aws, estimate = validate(batch)
    bootstrap = launch_script(batch)
    if batch.dry_run:
        # This is a metadata/validation preview, NOT an AWS transactional dry-run.
        return {'dry_run': True, 'names': batch.names, 'count': len(batch.names),
                'bundle_id': batch.bundle_id, 'blueprint_id': batch.blueprint_id,
                'estimate_usd_per_month': estimate,
                'warning': 'Catalog price estimate only; excludes transfer overages, static IP, taxes and other charges. AWS resources NOT created.'}
    if batch.confirm != 'CREATE':
        raise HTTPException(400, 'To submit a real create task, set confirm to CREATE')
    # Preflight existing instance names so obvious conflicts never reach the worker.
    for name in batch.names:
        try:
            aws.get_instance(instanceName=name)
        except ClientError as e:
            if e.response.get('Error', {}).get('Code') not in ('ServiceException', 'NotFoundException') or e.response.get('ResponseMetadata', {}).get('HTTPStatusCode') not in (404, None):
                raise HTTPException(502, 'Unable to verify whether an instance already exists')
        else:
            raise HTTPException(409, f'Instance already exists: {name}')
    identifier = str(uuid4())
    timestamp = now()
    payload = batch.model_dump() if hasattr(batch, 'model_dump') else batch.dict()
    payload.pop('confirm', None)
    # Arbitrary launch scripts can also contain secrets. Never store or return them in clear text.
    for field in ('root_password', 'user_data', 'ssh_public_key'):
        payload.pop(field, None)
    if bootstrap:
        payload['launch_encrypted'] = vault().encrypt(bootstrap.encode()).decode()
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        pending = conn.execute("SELECT count(*) FROM task_items ti JOIN tasks t ON ti.task_id=t.id WHERE ti.status='queued' AND t.status IN ('queued','running')").fetchone()[0]
        if pending + len(batch.names) > MAX_PENDING:
            raise HTTPException(429, 'Too many queued instance creations')
        conn.execute('''INSERT INTO tasks(id,kind,profile,region,status,payload,created_at,updated_at,estimate_usd)
                        VALUES(?,?,?,?,?,?,?,?,?)''',
                     (identifier, 'lightsail_create', batch.profile, batch.region, 'queued',
                      json.dumps(payload), timestamp, timestamp, estimate))
        conn.executemany('INSERT INTO task_items(task_id,resource_name,status) VALUES(?,?,?)',
                         [(identifier, n, 'queued') for n in batch.names])
    audit(actor, 'task', profile=batch.profile, region=batch.region, service='lightsail',
          resource=identifier, operation='enqueue-create', result='queued', detail=f'{len(batch.names)} items')
    return {'id': identifier, 'status': 'queued', 'count': len(batch.names), 'estimate_usd_per_month': estimate}


def list_tasks(limit=30):
    with db() as conn:
        rows = [dict(r) for r in conn.execute('''SELECT t.id,t.kind,t.profile,t.region,t.status,t.created_at,t.updated_at,
                  t.estimate_usd,t.cancel_requested,
                  (SELECT count(*) FROM task_items WHERE task_id=t.id) total,
                  (SELECT count(*) FROM task_items WHERE task_id=t.id AND status='submitted') submitted,
                  (SELECT count(*) FROM task_items WHERE task_id=t.id AND status='failed') failed
                  FROM tasks t ORDER BY t.created_at DESC LIMIT ?''', (max(1, min(limit, 100)),))]
    return {'items': rows}


def detail(identifier):
    with db() as conn:
        record = conn.execute('SELECT * FROM tasks WHERE id=?', (identifier,)).fetchone()
        if record is None:
            raise HTTPException(404, 'Task not found')
        task = dict(record)
        task['payload'] = json.loads(task['payload'])
        if task['payload'].pop('launch_encrypted', None):
            task['payload']['launch_configured'] = True
        task['items'] = [dict(x) for x in conn.execute('SELECT resource_name,status,attempted_at,finished_at,error,verification,verified_at,verification_detail FROM task_items WHERE task_id=? ORDER BY id', (identifier,))]
    return task


def cancel(identifier, actor='web'):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        task = conn.execute('SELECT status,profile,region FROM tasks WHERE id=?', (identifier,)).fetchone()
        if not task:
            raise HTTPException(404, 'Task not found')
        if task['status'] in ('completed', 'failed', 'canceled', 'partial'):
            raise HTTPException(409, 'Task is already finished')
        conn.execute('UPDATE tasks SET cancel_requested=1,updated_at=? WHERE id=?', (now(), identifier))
        conn.execute("UPDATE task_items SET status='canceled',finished_at=? WHERE task_id=? AND status='queued'", (now(), identifier))
        running = conn.execute("SELECT count(*) FROM task_items WHERE task_id=? AND status='running'", (identifier,)).fetchone()[0]
        if not running:
            completed = conn.execute("SELECT count(*) FROM task_items WHERE task_id=? AND status IN ('submitted','failed')", (identifier,)).fetchone()[0]
            conn.execute('UPDATE tasks SET status=? WHERE id=?', ('partial' if completed else 'canceled', identifier))
    audit(actor, 'task', profile=task['profile'], region=task['region'], resource=identifier,
          operation='cancel', result='requested')
    return {'ok': True, 'message': 'Queued items canceled; already submitted AWS operations cannot be reversed'}


def claim_one():
    """Claim a single item atomically. One worker per DB recommended."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        item = conn.execute('''SELECT i.id,i.task_id,i.resource_name,t.profile,t.region,t.payload FROM task_items i
            JOIN tasks t ON i.task_id=t.id WHERE i.status='queued' AND t.cancel_requested=0
            AND t.status IN ('queued','running') ORDER BY t.created_at,i.id LIMIT 1''').fetchone()
        if not item:
            return None
        stamp = now()
        conn.execute("UPDATE task_items SET status='running',attempted_at=? WHERE id=? AND status='queued'", (stamp, item['id']))
        conn.execute("UPDATE tasks SET status='running',updated_at=? WHERE id=?", (stamp, item['task_id']))
        return dict(item)


def finish_item(item, success, error=''):
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('UPDATE task_items SET status=?,error=?,finished_at=? WHERE id=?',
                     ('submitted' if success else 'failed', error[:350], now(), item['id']))
        remaining = conn.execute("SELECT count(*) FROM task_items WHERE task_id=? AND status IN ('queued','running')", (item['task_id'],)).fetchone()[0]
        if remaining == 0:
            summary = {row['status']: row['n'] for row in conn.execute('SELECT status,count(*) n FROM task_items WHERE task_id=? GROUP BY status', (item['task_id'],))}
            state = ('canceled' if not summary.get('submitted') and not summary.get('failed') else
                     'completed' if not summary.get('failed') and not summary.get('canceled') else
                     'failed' if not summary.get('submitted') and not summary.get('canceled') else 'partial')
            conn.execute('UPDATE tasks SET status=?,updated_at=? WHERE id=?', (state, now(), item['task_id']))
        else:
            conn.execute('UPDATE tasks SET updated_at=? WHERE id=?', (now(), item['task_id']))


def run_once():
    item = claim_one()
    if not item:
        return False
    args = json.loads(item['payload'])
    try:
        api = client(item['profile'], 'lightsail', item['region'])
        kwargs = dict(instanceNames=[item['resource_name']], availabilityZone=args.get('availability_zone') or item['region'] + 'a',
                      blueprintId=args['blueprint_id'], bundleId=args['bundle_id'])
        if args.get('launch_encrypted'):
            kwargs['userData'] = vault().decrypt(args['launch_encrypted'].encode()).decode()
        if args.get('key_pair_name'):
            kwargs['keyPairName'] = args['key_pair_name']
        # Server receives one create call for one name, limiting ambiguous partial responses.
        api.create_instances(**kwargs)
        finish_item(item, True)
        audit('task-worker', 'task', profile=item['profile'], region=item['region'], service='lightsail',
              resource=item['resource_name'], operation='create', result='submitted')
    except Exception as exc:
        # NO automatic retry: AWS may have accepted create before a timeout.
        message = type(exc).__name__
        if isinstance(exc, ClientError):
            message = str(exc.response.get('Error', {}).get('Code', 'ClientError'))
        finish_item(item, False, message)
        audit('task-worker', 'task', profile=item['profile'], region=item['region'], service='lightsail',
              resource=item['resource_name'], operation='create', result='failed', detail=message)
    return True


def reconcile_interrupted():
    """Do not retry potentially submitted creates after worker crash."""
    with db() as conn:
        conn.execute('BEGIN IMMEDIATE')
        count = conn.execute("UPDATE task_items SET status='failed',error='worker interrupted; verify in AWS before resubmitting',finished_at=? WHERE status='running'", (now(),)).rowcount
        for row in conn.execute("SELECT id FROM tasks WHERE status='running'"):
            remaining = conn.execute("SELECT count(*) FROM task_items WHERE task_id=? AND status='queued'", (row['id'],)).fetchone()[0]
            if remaining == 0:
                conn.execute("UPDATE tasks SET status='partial',updated_at=? WHERE id=?", (now(),row['id']))
    return count


initialize()

if __name__ == '__main__':
    reconcile_interrupted()
    interval = max(1, int(os.getenv('TASK_POLL_SECONDS', '3')))
    while True:
        if not run_once():
            time.sleep(interval)
