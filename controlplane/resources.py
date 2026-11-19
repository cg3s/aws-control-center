import json
import threading
from datetime import datetime, timezone
from fastapi import HTTPException
from .store import stamp, heartbeat

_refresh_lock = threading.Lock()


def enabled_profiles(web, profiles=None):
    with web.db() as c:
        disabled = {r[0] for r in c.execute('SELECT profile FROM v2_account_settings WHERE enabled=0')}
    return [p for p in (web.available_profiles() if profiles is None else profiles) if p not in disabled]


def identity(web, profile):
    data = web.client(profile, 'sts', web.REGIONS[0]).get_caller_identity()
    return data['Account'], data['Arn'].split(':')[1]


def observe(web, target):
    api = web.client(target['profile'], target['service'], target['region'])
    if target['service'] == 'lightsail':
        x = api.get_instance(instanceName=target['instance'])['instance']
        created = x.get('createdAt')
        created = created.isoformat() if isinstance(created, datetime) else str(created or '')
        return {'id': x['name'], 'state': x.get('state', {}).get('name', 'unknown'), 'created_at': created,
                'public': x.get('publicIpAddress', ''), 'private': x.get('privateIpAddress', ''),
                'generation': x.get('arn', '') + '/' + str(x.get('createdAt', '')),
                'bundle_id': x.get('bundleId', '')}
    x = api.describe_instances(InstanceIds=[target['instance']])['Reservations'][0]['Instances'][0]
    return {'id': x['InstanceId'], 'state': x['State']['Name'],
            'public': x.get('PublicIpAddress', ''), 'private': x.get('PrivateIpAddress', ''),
            'generation': x['InstanceId'], 'disk_policy': x.get('BlockDeviceMappings', []),
            'image_id': x.get('ImageId', ''), 'detailed_monitoring': x.get('Monitoring', {}).get('State') == 'enabled',
            'created_at': next((t['Value'] for t in x.get('Tags', []) if t['Key'] == 'awscc-created-at'), '')}


def assert_identity(web, target):
    if target['profile'] not in enabled_profiles(web):
        raise HTTPException(409, 'AWS account is disabled')
    account, partition = identity(web, target['profile'])
    if account != target['account_id'] or partition != target['partition']:
        raise HTTPException(409, 'Credential now resolves to a different AWS account')


def refresh(web):
    if not _refresh_lock.acquire(blocking=False):
        return False
    try:
        for profile in enabled_profiles(web):
            try:
                account, partition = identity(web, profile)
                data = web.instances(profile=profile, region='all', service='all')
                for x in data['items']:
                    x['account_id'], x['partition'] = account, partition
                    x['resource_key'] = '/'.join([partition, account, x['region'], x['service'], x['id']])
                with web.db() as c:
                    c.execute('INSERT OR REPLACE INTO v2_inventory VALUES(?,?,?,?,?,?)',
                              (profile, account, partition, stamp(), json.dumps(data['items']), json.dumps(data['errors'])))
            except Exception as exc:
                with web.db() as c:
                    old = c.execute('SELECT profile FROM v2_inventory WHERE profile=?', (profile,)).fetchone()
                    errors = json.dumps([{'profile': profile, 'message': web.aws_error(exc)}])
                    if old:
                        c.execute('UPDATE v2_inventory SET errors=? WHERE profile=?', (errors, profile))
                    else:
                        c.execute('INSERT INTO v2_inventory VALUES(?,?,?,?,?,?)', (profile, '', '', stamp(), '[]', errors))
        heartbeat(web, 'inventory')
        return True
    finally:
        _refresh_lock.release()


def inventory(web, profiles='', region='all', service='all', search='', offset=0, limit=50, group=''):
    allowed = set(enabled_profiles(web))
    chosen = set(profiles.split(',')) if profiles else allowed
    if not chosen <= allowed:
        raise HTTPException(400, 'Unknown account/profile')
    with web.db() as c:
        if group:
            chosen &= {r[0] for r in c.execute('SELECT profile FROM v2_account_settings WHERE group_name=?', (group,))}
        rows = c.execute('SELECT * FROM v2_inventory').fetchall()
    result, errors, seen, updates = [], [], set(), {}
    for row in rows:
        if row['profile'] not in chosen:
            continue
        updates[row['profile']] = row['fetched_at']
        errors.extend(json.loads(row['errors']))
        for x in json.loads(row['items']):
            key = x['resource_key']
            if key in seen or (region != 'all' and x['region'] != region) or (service != 'all' and x['service'] != service):
                continue
            seen.add(key)
            if search.lower() not in json.dumps(x, ensure_ascii=False).lower():
                continue
            result.append(x)
    missing = chosen - updates.keys()
    errors.extend({'profile': p, 'message': 'Inventory not collected yet'} for p in sorted(missing))
    result.sort(key=lambda x: x['resource_key'])
    return {'items': result[offset:offset+limit], 'total': len(result), 'errors': errors, 'updated_at': updates}


def pages(api, operation, field, **kwargs):
    return [x for p in api.get_paginator(operation).paginate(**kwargs) for x in p.get(field, [])]


def select_targets(web, selector, period=300):
    profiles = selector.get('profiles', [])
    data = inventory(web, ','.join(profiles), selector.get('region', 'all'), selector.get('service', 'all'), limit=500)
    if data['errors']:
        raise HTTPException(409, 'Selector inventory is incomplete; wait for a successful refresh')
    result = []
    for item in data['items']:
        if not all(item.get('tags', {}).get(k) == v for k,v in selector['tags'].items()):
            continue
        if period == 60 and (item['service'] != 'ec2' or not item.get('detailed_monitoring')):
            raise HTTPException(400, 'Selector includes resources without EC2 detailed monitoring')
        if not item.get('generation'):
            raise HTTPException(409, 'Refresh inventory before using tag selectors')
        result.append({k: item.get(k, '') for k in ('profile','region','service','account_id','partition','generation','created_at')} |
                      {'instance': item['id'], 'period': period})
    if len(result) > 100:
        raise HTTPException(400, 'Selector exceeds 100 targets; narrow its tags or account scope')
    return result


def catalog(web, profile, region, service):
    web.valid_profile(profile)
    web.check_region(region)
    if service == 'lightsail':
        api = web.client(profile, 'lightsail', region)
        def all_pages(method, field, **kwargs):
            values, token = [], None
            for _ in range(100):
                page = method(**kwargs, **({'pageToken': token} if token else {}))
                values.extend(page.get(field, []))
                token = page.get('nextPageToken')
                if not token:
                    return values
            raise HTTPException(502, 'Catalog pagination limit exceeded')
        bundles = all_pages(api.get_bundles, 'bundles', includeInactive=False)
        blueprints = all_pages(api.get_blueprints, 'blueprints', includeInactive=False)
        selected = next((r for r in api.get_regions(includeAvailabilityZones=True).get('regions', []) if r['name'] == region), {})
        return {'bundles': [{'id': b['bundleId'], 'name': b.get('name', ''), 'price_usd_per_month': b.get('price'),
                 'ram_gb': b.get('ramSizeInGb')} for b in bundles if b.get('isActive', True)],
                'blueprints': [{'id': b['blueprintId'], 'name': b.get('name', ''), 'version': b.get('version', '')}
                  for b in blueprints if b.get('isActive', True) and b.get('platform') != 'WINDOWS' and 'windows' not in b['blueprintId'].lower()],
                'zones': [z['zoneName'] for z in selected.get('availabilityZones', []) if z.get('state', 'available') == 'available']}
    api = web.client(profile, 'ec2', region)
    return {
        'subnets': pages(api, 'describe_subnets', 'Subnets'),
        'security_groups': pages(api, 'describe_security_groups', 'SecurityGroups'),
        'vpcs': pages(api, 'describe_vpcs', 'Vpcs'),
        'key_pairs': api.describe_key_pairs().get('KeyPairs', []),
        'launch_templates': pages(api, 'describe_launch_templates', 'LaunchTemplates'),
        'instance_types': pages(api, 'describe_instance_types', 'InstanceTypes'),
        'images': api.describe_images(Owners=['self'], Filters=[{'Name': 'state', 'Values': ['available']},
                                   {'Name': 'platform-details', 'Values': ['Linux/UNIX']}]).get('Images', []),
        'note': 'Images include owned AMIs. An explicit public Linux AMI ID can also be validated at preview.',
    }
