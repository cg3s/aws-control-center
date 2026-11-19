import hashlib
import json
import re
import time
import threading
import shlex
from uuid import uuid4
from datetime import datetime, timezone
from fastapi import HTTPException
from botocore.exceptions import ClientError
from . import firewall, resources, ssh
from .bootstrap import render
from .models import Launch
from .store import stamp, document, documents, heartbeat

TERMINAL = {'succeeded', 'failed', 'unknown', 'canceled', 'archived'}
SSH_SLOTS = threading.BoundedSemaphore(2)
ACCOUNT_SLOTS = {}
ACCOUNT_SLOTS_LOCK = threading.Lock()


def mutate(api, method, target, **kwargs):
    with ACCOUNT_SLOTS_LOCK:
        slot = ACCOUNT_SLOTS.setdefault((target['partition'],target['account_id'],target['region']), threading.BoundedSemaphore(2))
    with slot:
        return getattr(api, method)(**kwargs)


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'))


def not_found(exc):
    if not isinstance(exc, ClientError):
        return False
    code = exc.response.get('Error', {}).get('Code')
    return code in ('NotFoundException', 'InvalidInstanceID.NotFound') or (
        code == 'ServiceException' and exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode') == 404)


class Jobs:
    def __init__(self, web):
        self.web = web

    def prepare(self, item):
        w, target = self.web, item.target.model_dump()
        w.valid_profile(target['profile'])
        if target['profile'] not in resources.enabled_profiles(w):
            raise HTTPException(409, 'AWS account is disabled')
        w.check_region(target['region'])
        account, partition = resources.identity(w, target['profile'])
        target.update(account_id=account, partition=partition)
        payload = item.model_dump()
        payload['target'] = target
        options = payload['options']
        if any(not k or len(k)>128 or len(v)>256 or k.lower().startswith('aws:') or k in ('awscc-task','awscc-created-at') for k,v in options['tags'].items()):
            raise HTTPException(400, 'Invalid or reserved resource tag')
        if item.kind != 'create':
            current = resources.observe(w, target)
            if target['generation'] and target['generation'] != current['generation']:
                raise HTTPException(409, 'Instance was replaced; select the current resource')
            target['generation'] = current['generation']
            payload['before'] = current
            if item.kind in ('script', 'bootstrap_verify'):
                connection_id = item.connection_id
                if not connection_id:
                    resource_key = '/'.join([partition, account, target['region'], target['service'], target['instance']])
                    connection_id = next((r['id'] for r in documents(w, 'connection') if r['payload'].get('resource_key') == resource_key), '')
                connection = document(w, connection_id, 'connection')
                payload['connection'] = connection['payload']
                payload['connection_version'] = connection['version']
                if item.kind == 'script':
                    script = document(w, item.script_id, 'script')
                    payload['script'] = script['payload']
                    payload['script_version'] = script['version']
                    parameters = script['payload'].get('parameters', {}) | item.parameters
                    if any(k.startswith('AWSCC_') for k in parameters):
                        raise HTTPException(400, 'AWSCC_ environment variables are reserved')
                    parameters |= {'AWSCC_INSTANCE': target['instance'], 'AWSCC_REGION': target['region'], 'AWSCC_PROFILE': target['profile']}
                    payload['script']['body'] = '\n'.join('export '+k+'='+shlex.quote(v) for k,v in parameters.items())+'\n'+payload['script']['body']
                else:
                    payload['script'] = {'body': 'cloud-init status --wait --long', 'timeout': 600}
            if item.kind == 'delete' and target['service'] == 'ec2':
                api = w.client(target['profile'], 'ec2', target['region'])
                protection = api.describe_instance_attribute(InstanceId=target['instance'], Attribute='disableApiTermination')
                if protection.get('DisableApiTermination', {}).get('Value'):
                    raise HTTPException(409, 'EC2 termination protection is enabled')
            return payload
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{1,62}', target['instance']):
            raise HTTPException(400, 'Create names must be 2–63 letters, digits or hyphens')
        launch = options['launch']
        if options['bootstrap_id']:
            template = document(w, options['bootstrap_id'], 'bootstrap')
            launch = dict(template['payload'])
            launch.update({k: options['launch'][k] for k in item.options.launch.model_fields_set})
            payload['bootstrap_version'] = template['version']
        initialization = Launch(**launch)
        bootstrap = render(initialization, target['service'])
        rules = [rule.model_dump() for rule in initialization.firewall_rules]
        if rules:
            payload['firewall_rules'] = rules
        payload['ssh_policy'] = {'root_login': initialization.root_login, 'password_auth': initialization.password_auth,
                                 'root_password_set': bool(initialization.root_password)}
        api = w.client(target['profile'], target['service'], target['region'])
        if target['service'] == 'lightsail':
            catalog = resources.catalog(w, target['profile'], target['region'], 'lightsail')
            bundle = next((b for b in catalog['bundles'] if b['id'] == options['bundle_id']), None)
            blueprint = next((b for b in catalog['blueprints'] if b['id'] == options['blueprint_id']), None)
            if not bundle or not blueprint or 'windows' in options['blueprint_id'].lower():
                raise HTTPException(400, 'Choose an available Linux blueprint and bundle')
            zone = options['availability_zone'] or next(iter(catalog['zones']), '')
            if zone not in catalog['zones']:
                raise HTTPException(400, 'Unavailable zone')
            try:
                resources.observe(w, target)
            except ClientError as exc:
                if not not_found(exc):
                    raise
            else:
                raise HTTPException(409, 'Lightsail instance name already exists')
            payload['aws_args'] = dict(instanceNames=[target['instance']], availabilityZone=zone,
                                       bundleId=options['bundle_id'], blueprintId=options['blueprint_id'], userData=bootstrap,
                                       tags=[{'key':k,'value':v} for k,v in options['tags'].items()])
            if options['key_pair_name']:
                api.get_key_pair(keyPairName=options['key_pair_name'])
                payload['aws_args']['keyPairName'] = options['key_pair_name']
            payload['estimate_usd_month'] = bundle['price_usd_per_month']
        else:
            if options['launch_template_id']:
                if not options['launch_template_version'].isdigit():
                    raise HTTPException(400, 'Specify an immutable numeric launch template version')
                data = api.describe_launch_template_versions(LaunchTemplateId=options['launch_template_id'],
                       Versions=[options['launch_template_version']])['LaunchTemplateVersions'][0]['LaunchTemplateData']
                interfaces = data.get('NetworkInterfaces', [])
                if data.get('InstanceMarketOptions') or len(interfaces) != 1 or not interfaces[0].get('SubnetId') or any(interfaces[0].get(k) for k in ('NetworkInterfaceId','PrivateIpAddress','PrivateIpAddresses')):
                    raise HTTPException(400, 'Template requires one new network interface with an explicit subnet and on-demand instances')
                args = {k: v for k, v in data.items() if k in ('ImageId', 'InstanceType', 'SubnetId', 'SecurityGroupIds',
                        'KeyName', 'IamInstanceProfile', 'BlockDeviceMappings', 'EbsOptimized')}
                args['SubnetId'] = interfaces[0]['SubnetId']
                args['SecurityGroupIds'] = interfaces[0].get('Groups') or data.get('SecurityGroupIds', [])
            else:
                args = dict(ImageId=options['image_id'], InstanceType=options['instance_type'], SubnetId=options['subnet_id'],
                            SecurityGroupIds=options['security_group_ids'])
                if options['key_pair_name']:
                    args['KeyName'] = options['key_pair_name']
                if options['instance_profile_arn']:
                    args['IamInstanceProfile'] = {'Arn': options['instance_profile_arn']}
            image = api.describe_images(ImageIds=[args.get('ImageId', '')])['Images']
            if not image or image[0].get('Platform') == 'windows' or image[0].get('State') != 'available':
                raise HTTPException(400, 'Choose an available Linux AMI')
            types = api.describe_instance_types(InstanceTypes=[args.get('InstanceType', '')])['InstanceTypes']
            if image[0]['Architecture'] not in types[0]['ProcessorInfo']['SupportedArchitectures']:
                raise HTTPException(400, 'AMI architecture does not match instance type')
            subnet = api.describe_subnets(SubnetIds=[args.get('SubnetId', '')])['Subnets'][0]
            if rules:
                payload['firewall_vpc_id'] = subnet['VpcId']
            else:
                groups = api.describe_security_groups(GroupIds=args.get('SecurityGroupIds', []))['SecurityGroups']
                if not args.get('SecurityGroupIds') or any(g['VpcId'] != subnet['VpcId'] for g in groups):
                    raise HTTPException(400, 'Choose security groups from the subnet VPC')
            if not options['launch_template_id']:
                root = image[0].get('RootDeviceName')
                if not root or image[0].get('RootDeviceType') != 'ebs':
                    raise HTTPException(400, 'An EBS-backed Linux AMI is required')
                minimum = next((b.get('Ebs', {}).get('VolumeSize', 8) for b in image[0].get('BlockDeviceMappings', []) if b['DeviceName'] == root), 8)
                if options['disk_gib'] < minimum:
                    raise HTTPException(400, 'Root disk is smaller than the AMI minimum')
                args['BlockDeviceMappings'] = [{'DeviceName': root, 'Ebs': {'VolumeSize': options['disk_gib'], 'VolumeType': 'gp3', 'DeleteOnTermination': True}}]
            if options['launch_template_id']:
                args.pop('SubnetId')
                args.pop('SecurityGroupIds')
                args['NetworkInterfaces'] = interfaces
            else:
                args['NetworkInterfaces'] = [{'DeviceIndex': 0, 'SubnetId': args.pop('SubnetId'),
                                             'Groups': args.pop('SecurityGroupIds'), 'AssociatePublicIpAddress': options['public_ip']}]
            inherited_tags = {t['Key']:t['Value'] for spec in data.get('TagSpecifications',[]) if spec['ResourceType']=='instance' for t in spec.get('Tags',[]) if t['Key'] not in ('awscc-task','awscc-created-at')} if options['launch_template_id'] else {}
            tags = inherited_tags | options['tags'] | {'Name': target['instance'], 'awscc-created-at': stamp()}
            if any(k.lower().startswith('aws:') for k in tags):
                raise HTTPException(400, 'Reserved AWS tag prefix')
            args.update(MinCount=1, MaxCount=1, UserData=bootstrap,
                        TagSpecifications=[{'ResourceType': 'instance', 'Tags': [{'Key': k, 'Value': v} for k, v in tags.items()]}],
                        MetadataOptions={'HttpTokens': 'required'})
            if options['launch_template_id']:
                # Keep the template's network interface, overriding only its groups when requested.
                args = {k: args[k] for k in ('MinCount','MaxCount','UserData','MetadataOptions','TagSpecifications')} | {
                    'LaunchTemplate': {'LaunchTemplateId': options['launch_template_id'], 'Version': options['launch_template_version']}}
                if rules:
                    args['NetworkInterfaces'] = interfaces
                args['TagSpecifications'] += [spec for spec in data.get('TagSpecifications',[]) if spec['ResourceType']!='instance']
                if options['key_pair_name']:
                    args['KeyName'] = options['key_pair_name']
            payload['aws_args'] = args
            payload['estimate_usd_month'] = None
        # Passwords are already hashed into bootstrap; do not persist plaintext launch parameters.
        payload['options'].pop('launch', None)
        return payload

    def preview(self, request, actor):
        keys = [(x.kind, x.target.profile, x.target.region, x.target.service, x.target.instance) for x in request.items]
        if len(set(keys)) != len(keys):
            raise HTTPException(400, 'Duplicate targets in batch')
        prepared = [self.prepare(x) for x in request.items]
        physical = [(x['kind'],)+tuple(x['target'][k] for k in ('partition','account_id','region','service','instance')) for x in prepared]
        if len(set(physical)) != len(physical):
            raise HTTPException(400, 'Duplicate resource operation, including aliases of the same AWS account')
        fingerprint = hashlib.sha256(encode(request.model_dump(exclude={'confirm', 'preview_id'})).encode()).hexdigest()
        identifier = str(uuid4())
        payload = {'fingerprint': fingerprint, 'items': prepared, 'expires': time.time()+900, 'actor': actor}
        with self.web.db() as c:
            c.execute('INSERT INTO v2_documents VALUES(?,?,?,?,?,?,?)', (identifier, 'preview', 'Task preview', 1,
                      self.web.vault().encrypt(encode(payload).encode()), stamp(), stamp()))
        return {'preview_id': identifier, 'items': [{'kind': x['kind'], 'target': x['target'],
                    'before': x.get('before'), 'estimate_usd_month': x.get('estimate_usd_month'),
                    'ssh_policy': x.get('ssh_policy'), 'firewall_rules': x.get('firewall_rules', [])} for x in prepared],
                'confirmation': 'DELETE' if any(x.kind == 'delete' for x in request.items) else 'EXECUTE',
                'note': 'EC2 price is not estimated. Retained disks and IPs may continue to incur charges.'}

    def submit(self, request, actor):
        w = self.web
        fingerprint = hashlib.sha256(encode(request.model_dump(exclude={'confirm', 'preview_id'})).encode()).hexdigest()
        with w.db() as c:
            old = c.execute('SELECT id,fingerprint FROM v2_jobs WHERE request_key=?', (request.request_key,)).fetchone()
        if old:
            if old['fingerprint'] != fingerprint:
                raise HTTPException(409, 'Idempotency key was used for a different request')
            return self.detail(old['id'])
        preview = document(w, request.preview_id, 'preview')['payload']
        if preview['fingerprint'] != fingerprint or preview['actor'] != actor or preview['expires'] < time.time():
            raise HTTPException(409, 'Preview expired or changed; preview again')
        confirmation = 'DELETE' if any(x.kind == 'delete' for x in request.items) else 'EXECUTE'
        if request.confirm != confirmation:
            raise HTTPException(400, 'Explicit confirmation required: '+confirmation)
        return self.enqueue(preview['items'], request.request_key, fingerprint, actor)

    def enqueue(self, items, request_key, fingerprint, actor):
        w, identifier, now = self.web, str(uuid4()), stamp()
        with w.db() as c:
            c.execute('BEGIN IMMEDIATE')
            old = c.execute('SELECT id,fingerprint FROM v2_jobs WHERE request_key=?', (request_key,)).fetchone()
            if old:
                if old['fingerprint'] != fingerprint:
                    raise HTTPException(409, 'Idempotency key belongs to another task')
                return self.detail(old['id'])
            if c.execute("SELECT COUNT(*) FROM v2_items WHERE status='queued'").fetchone()[0] + len(items) > 1000:
                raise HTTPException(429, 'Task queue is full')
            c.execute('INSERT INTO v2_jobs VALUES(?,?,?,?,?,?,?)', (identifier, request_key, fingerprint, 'queued', actor, now, now))
            for x in items:
                item_id = str(uuid4())
                if x['kind'] == 'create':
                    if x['target']['service'] == 'ec2':
                        x['aws_args']['ClientToken'] = item_id
                        x['aws_args']['TagSpecifications'][0]['Tags'].append({'Key': 'awscc-task', 'Value': item_id})
                    else:
                        x['aws_args']['tags'].append({'key': 'awscc-task', 'value': item_id})
                c.execute('INSERT INTO v2_items(id,job_id,kind,target,payload,status,updated_at) VALUES(?,?,?,?,?,?,?)',
                          (item_id, identifier, x['kind'], encode(x['target']), w.vault().encrypt(encode(x).encode()), 'queued', now))
        profiles = {item['target']['profile'] for item in items}
        w.audit(actor, 'task', profile=next(iter(profiles)) if len(profiles) == 1 else '',
                resource=identifier, operation='enqueue', detail=f'{len(items)} items')
        return self.detail(identifier)

    def detail(self, identifier, profile=None):
        with self.web.db() as c:
            row = c.execute('SELECT * FROM v2_jobs WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise HTTPException(404, 'Task not found')
            items = [dict(r) for r in c.execute('''SELECT id,kind,target,status,result,error,attempted_at,updated_at
                FROM v2_items WHERE job_id=? AND (? IS NULL OR json_extract(target,'$.profile')=?) ORDER BY rowid''',
                (identifier, profile, profile))]
        if profile is not None and not items:
            raise HTTPException(404, 'Task not found for this account')
        for x in items:
            x['target'], x['result'] = json.loads(x['target']), json.loads(x['result'])
        return dict(row) | {'items': items, 'status': self.status_from_items([i['status'] for i in items]) if profile is not None else row['status']}

    @staticmethod
    def status_from_items(states):
        if states and all(state == 'queued' for state in states):
            return 'queued'
        if any(state not in TERMINAL for state in states):
            return 'running'
        return states[0] if len(set(states)) == 1 else 'partial'

    def summarize(self, c, job_id):
        states = [r[0] for r in c.execute('SELECT status FROM v2_items WHERE job_id=?', (job_id,))]
        c.execute('UPDATE v2_jobs SET status=?,updated_at=? WHERE id=?', (self.status_from_items(states), stamp(), job_id))

    def cancel(self, identifier, actor, profile=None):
        if self.detail(identifier, profile)['status'] == 'archived':
            raise HTTPException(409, 'Historical tasks are read-only')
        with self.web.db() as c:
            c.execute("""UPDATE v2_items SET status='canceled',updated_at=? WHERE job_id=? AND status='queued'
                         AND (? IS NULL OR json_extract(target,'$.profile')=?)""", (stamp(), identifier, profile, profile))
            self.summarize(c, identifier)
        self.web.audit(actor, 'task', profile=profile or '', resource=identifier, operation='cancel')
        return self.detail(identifier, profile)

    def verify(self, identifier, actor, profile=None):
        if self.detail(identifier, profile)['status'] == 'archived':
            raise HTTPException(409, 'Inspect historical instances from the resource list')
        with self.web.db() as c:
            c.execute("""UPDATE v2_items SET status='verifying',lease_until=NULL WHERE job_id=?
                AND status IN ('submitted','unknown') AND kind NOT IN ('script','bootstrap_verify')
                AND (? IS NULL OR json_extract(target,'$.profile')=?)""", (identifier, profile, profile))
        self.web.audit(actor, 'task', profile=profile or '', resource=identifier, operation='verify')
        return self.detail(identifier, profile)

    def run_once(self):
        w, owner = self.web, str(uuid4())
        heartbeat(w, 'tasks')
        with w.db() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute("UPDATE v2_items SET status='unknown',error='Worker lease expired; verify before retry',lease_until=NULL WHERE status='running' AND lease_until<?", (time.time(),))
            row = c.execute("""SELECT i.* FROM v2_items i JOIN v2_jobs j ON j.id=i.job_id
                WHERE i.status IN ('queued','submitted','verifying') AND (i.lease_until IS NULL OR i.lease_until<?)
                AND NOT EXISTS (SELECT 1 FROM v2_items previous WHERE previous.job_id=i.job_id
                  AND previous.rowid<i.rowid AND previous.status NOT IN ('succeeded','failed','unknown','canceled','archived')
                  AND json_extract(previous.target,'$.account_id')=json_extract(i.target,'$.account_id')
                  AND json_extract(previous.target,'$.partition')=json_extract(i.target,'$.partition')
                  AND json_extract(previous.target,'$.region')=json_extract(i.target,'$.region')
                  AND json_extract(previous.target,'$.service')=json_extract(i.target,'$.service')
                  AND json_extract(previous.target,'$.instance')=json_extract(i.target,'$.instance'))
                AND NOT (j.actor='automation' AND EXISTS (SELECT 1 FROM app_settings WHERE name='automation_paused' AND value='true'))
                AND NOT (j.actor='automation' AND EXISTS (SELECT 1 FROM v2_events e LEFT JOIN v2_rule_flags f ON f.rule_id=e.rule_id
                    WHERE j.request_key='event-'||e.id AND COALESCE(f.enabled,0)=0))
                ORDER BY j.updated_at,i.updated_at,i.rowid LIMIT 1""", (time.time(),)).fetchone()
            if not row:
                for job in c.execute("SELECT id FROM v2_jobs WHERE status IN ('queued','running')").fetchall():
                    self.summarize(c, job['id'])
                return False
            row = dict(row)
            checking = row['status'] != 'queued'
            c.execute('UPDATE v2_items SET status=?,lease_until=?,lease_owner=?,attempted_at=COALESCE(attempted_at,?),updated_at=? WHERE id=?',
                      ('verifying' if checking else 'running', time.time()+1900, owner, stamp(), stamp(), row['id']))
            c.execute("UPDATE v2_jobs SET status='running',updated_at=? WHERE id=?", (stamp(), row['job_id']))
        payload = json.loads(w.vault().decrypt(row['payload']))
        target, kind, result, status, error = payload['target'], row['kind'], json.loads(row['result']), 'submitted', ''
        try:
            resources.assert_identity(w, target)
            if payload.get('prepare_error'):
                raise HTTPException(400, payload['prepare_error'])
            if checking:
                status, result = self.check(row, payload, result)
            else:
                api = w.mutation_client(target['profile'], target['service'], target['region'])
                if kind == 'create':
                    if target['service'] == 'ec2':
                        if payload.get('firewall_rules'):
                            read_api = w.client(target['profile'], 'ec2', target['region'])
                            group_id = firewall.ec2_group(read_api, api, payload['firewall_vpc_id'], payload['firewall_rules'])
                            payload['aws_args']['NetworkInterfaces'][0]['Groups'] = [group_id]
                            result.update(firewall_group_id=group_id, firewall_status='applied')
                        for tag in payload['aws_args']['TagSpecifications'][0]['Tags']:
                            if tag['Key'] == 'awscc-created-at':
                                tag['Value'] = stamp()
                        response = mutate(api, 'run_instances', target, **payload['aws_args'])
                        result['instance_id'] = response['Instances'][0]['InstanceId']
                    else:
                        response = mutate(api, 'create_instances', target, **payload['aws_args'])
                    result['request_id'] = response.get('ResponseMetadata', {}).get('RequestId', '')
                else:
                    current = resources.observe(w, target)
                    if current['generation'] != target['generation']:
                        raise HTTPException(409, 'Instance generation changed')
                    if kind in ('script', 'bootstrap_verify'):
                        with SSH_SLOTS:
                            execution = ssh.execute(w, target, payload['connection'], payload['script'])
                        payload['execution'] = execution
                        result = {'exit_code': execution['exit_code'], 'output_available': True}
                        status = 'succeeded' if execution['exit_code'] == 0 else 'failed'
                    elif target['service'] == 'ec2':
                        fn = {'delete': 'terminate_instances', 'start': 'start_instances', 'stop': 'stop_instances', 'reboot': 'reboot_instances'}[kind]
                        mutate(api, fn, target, InstanceIds=[target['instance']])
                    else:
                        fn = {'delete': 'delete_instance', 'start': 'start_instance', 'stop': 'stop_instance', 'reboot': 'reboot_instance'}[kind]
                        mutate(api, fn, target, instanceName=target['instance'])
                result['submitted_at'] = stamp()
        except Exception as exc:
            if kind == 'delete' and not_found(exc):
                status = 'succeeded'
            elif checking and kind == 'create' and not_found(exc) and time.time()-datetime.fromisoformat(row['attempted_at']).timestamp() < 1800:
                status = 'verifying'
                result['note'] = 'Waiting for eventual consistency'
            else:
                # Read failures during verification must not provoke a second mutation.
                ambiguous = isinstance(exc, ClientError) and (exc.response.get('ResponseMetadata', {}).get('HTTPStatusCode', 0) >= 500 or
                    exc.response.get('Error', {}).get('Code') in ('RequestTimeout', 'RequestTimeoutException', 'InternalError', 'ServiceUnavailable'))
                status = 'unknown' if checking or ambiguous or not isinstance(exc, (HTTPException, ClientError)) else 'failed'
                error = str(exc.detail) if isinstance(exc, HTTPException) else w.aws_error(exc)
        with w.db() as c:
            c.execute('UPDATE v2_items SET status=?,result=?,error=?,payload=?,updated_at=?,lease_until=?,lease_owner=NULL WHERE id=? AND lease_owner=?',
                      (status, encode(result), error[:350], w.vault().encrypt(encode(payload).encode()), stamp(),
                       time.time()+15 if status in ('submitted', 'verifying') else None, row['id'], owner))
            self.summarize(c, row['job_id'])
        w.audit('task-worker', 'task', profile=target['profile'], region=target['region'], service=target['service'],
                resource=target['instance'], operation=kind, result=status, detail=error)
        return True

    def check(self, row, payload, result):
        target = dict(payload['target'])
        if row['kind'] == 'create' and target['service'] == 'ec2':
            if not result.get('instance_id'):
                api = self.web.client(target['profile'], 'ec2', target['region'])
                matches = api.describe_instances(Filters=[{'Name': 'client-token', 'Values': [row['id']]}])
                found = [i for r in matches.get('Reservations', []) for i in r['Instances']]
                if len(found) != 1:
                    waiting = not found and time.time()-datetime.fromisoformat(row['attempted_at']).timestamp() < 1800
                    return ('verifying' if waiting else 'unknown'), result
                result['instance_id'] = found[0]['InstanceId']
            target['instance'] = result['instance_id']
        current = resources.observe(self.web, target)
        if row['kind'] == 'create' and target['service'] == 'lightsail':
            read_api = self.web.client(target['profile'], 'lightsail', target['region'])
            x = read_api.get_instance(instanceName=target['instance'])['instance']
            if not any(t.get('key') == 'awscc-task' and t.get('value') == row['id'] for t in x.get('tags', [])):
                waiting = time.time()-datetime.fromisoformat(row['attempted_at']).timestamp() < 1800
                return ('verifying' if waiting else 'unknown'), result | {'state': current['state'], 'note': 'Task ownership not verified'}
            if payload.get('firewall_rules'):
                write_api = self.web.mutation_client(target['profile'], 'lightsail', target['region'])
                result['state'] = current['state']
                firewall_state = firewall.lightsail_state(read_api, write_api, target['instance'], payload['firewall_rules'], result)
                if firewall_state != 'succeeded':
                    if firewall_state == 'verifying' and time.time()-datetime.fromisoformat(row['attempted_at']).timestamp() > 1800:
                        return 'unknown', result
                    return firewall_state, result
        elif row['kind'] != 'create' and current['generation'] != target['generation']:
            return 'unknown', result | {'note': 'Instance generation changed'}
        expected = {'create': 'running', 'start': 'running', 'stop': 'stopped', 'delete': 'terminated', 'reboot': 'running'}[row['kind']]
        # AWS acceptance is the strongest available reboot acknowledgement here.
        if row['kind'] == 'reboot':
            return 'succeeded', result | {'state': current['state'], 'verification': 'request_accepted'}
        result |= {'state': current['state'], 'verified_at': stamp()}
        if current['state'] == expected:
            return 'succeeded', result
        attempted = datetime.fromisoformat(row['attempted_at'])
        if (datetime.now(timezone.utc)-attempted).total_seconds() > 1800:
            return 'unknown', result
        return 'verifying', result
