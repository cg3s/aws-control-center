import json
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
from uuid import uuid4
from concurrent.futures import ThreadPoolExecutor, as_completed
from .store import documents, stamp, heartbeat, ClosingConnection
from . import resources
from fastapi import HTTPException


def report(web, profile, region, service, instance, window='1h', timezone_name='UTC', period=300):
    if window not in ('5m','1h','24h','7d','30d','day','month') or service not in ('ec2','lightsail') or period not in (60,300):
        raise HTTPException(400, 'Unsupported traffic query')
    web.check_region(region)
    web.valid_profile(profile)
    try:
        ZoneInfo(timezone_name)
    except (ValueError, KeyError):
        raise HTTPException(400, 'Unknown timezone')
    account, partition = resources.identity(web,profile)
    target = dict(profile=profile,region=region,service=service,instance=instance,account_id=account,partition=partition,period=period)
    observed = resources.observe(web,target)
    target.update(generation=observed['generation'],created_at=observed.get('created_at',''))
    end = int(time.time()-600)//period*period
    with connect(web) as c:
        last = c.execute('SELECT MAX(at) FROM buckets WHERE resource=? AND period=?',(key(target),period)).fetchone()[0]
    if last is None or last < end-period:
        collect(web,target)
    start,end = interval(window,end,timezone_name)
    return query(web,target,start,end) | {'target':target}


def key(target):
    return '/'.join(str(target.get(k, '')) for k in ('partition', 'account_id', 'region', 'service', 'instance', 'generation'))+'/'+str(target.get('period', 300))


def connect(web):
    c = sqlite3.connect(web.DATA_DIR / 'metrics.db', timeout=15, factory=ClosingConnection)
    c.row_factory = sqlite3.Row
    c.execute('PRAGMA journal_mode=WAL')
    c.execute('''CREATE TABLE IF NOT EXISTS buckets(resource TEXT NOT NULL, period INTEGER NOT NULL,
                 at INTEGER NOT NULL, inbound REAL, outbound REAL, count INTEGER NOT NULL,
                 PRIMARY KEY(resource,period,at))''')
    c.execute('CREATE INDEX IF NOT EXISTS metric_time ON buckets(period,at)')
    return c


def interval(window, end, zone='UTC'):
    local = datetime.fromtimestamp(end, timezone.utc).astimezone(ZoneInfo(zone))
    if window == 'month':
        start = local.replace(day=1, hour=0, minute=0, second=0, microsecond=0).timestamp()
    elif window == 'day':
        start = local.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
    else:
        start = end - {'5m': 300, '1h': 3600, '24h': 86400, '7d': 7*86400, '30d': 30*86400}[window]
    return int(start), end


def query(web, target, start, end):
    """Use disjoint hourly and raw ranges; missing periods are never zero-filled."""
    requested_start, period = start, target.get('period', 300)
    birth = None
    if target.get('created_at'):
        try:
            birth_time = datetime.fromisoformat(target['created_at'])
            if birth_time.tzinfo is None:
                birth_time = birth_time.replace(tzinfo=timezone.utc)
            birth = int(birth_time.timestamp())
            start = max(start, ((birth+period-1)//period)*period)
        except ValueError:
            pass
    boundary_start = ((start+3599)//3600)*3600
    boundary_end = (end//3600)*3600
    rows = []
    with connect(web) as c:
        if boundary_start < boundary_end:
            rows.extend(c.execute('SELECT * FROM buckets WHERE resource=? AND period=3600 AND at>=? AND at<? ORDER BY at',
                                  (key(target), boundary_start, boundary_end)).fetchall())
        for a, b in ((start, min(end, boundary_start)), (max(start, boundary_end, boundary_start), end)):
            if a < b:
                edge = c.execute('SELECT * FROM buckets WHERE resource=? AND period=? AND at>=? AND at<? ORDER BY at',
                                 (key(target), period, a, b)).fetchall()
                if not edge and birth is not None and a == start and birth >= start-period and b == boundary_start:
                    # A retained first-hour aggregate contains no pre-creation traffic.
                    saved = c.execute('SELECT * FROM buckets WHERE resource=? AND period=3600 AND at=?', (key(target), a//3600*3600)).fetchone()
                    if saved and saved['count'] == (b-a)//period:
                        edge = [saved]
                rows.extend(edge)
    # For sub-hour windows the two edge ranges collapse into the first range.
    rows = list({(r['period'], r['at']): r for r in rows}.values())
    rows.sort(key=lambda r: r['at'])
    covered = sum(r['count'] for r in rows)
    expected = max(0, (end-start)//period)
    inbound = sum(r['inbound'] or 0 for r in rows)
    outbound = sum(r['outbound'] or 0 for r in rows)
    return {'requested_start': requested_start, 'start': start, 'end': end, 'inbound': inbound, 'outbound': outbound,
            'rate_mbps': (inbound+outbound)*8/max(1, end-start)/1000000,
            'expected_buckets': expected, 'covered_buckets': covered,
            'complete': expected > 0 and covered == expected,
            'points': [dict(r) for r in rows], 'period_seconds': period,
            'note': 'Network metrics, not billable traffic or remaining plan allowance'}


def collect(web, target, now=None):
    now = int(now or time.time())
    resources.assert_identity(web, target)
    current = resources.observe(web, target)
    if target['generation'] != current['generation']:
        raise ValueError('Instance generation changed; recreate monitoring target')
    period = target.get('period', 300)
    if period == 60 and (target['service'] != 'ec2' or not current.get('detailed_monitoring')):
        raise ValueError('One-minute metrics require existing EC2 detailed monitoring')
    end = (now-600)//period*period
    with connect(web) as c:
        last = c.execute('SELECT MAX(at) FROM buckets WHERE resource=? AND period=?', (key(target), period)).fetchone()[0]
    month_start = interval('month', end)[0]
    start = max(month_start, last-7200) if last is not None else min(month_start, end-30*86400)
    if period == 60:
        start = max(start, end-14*86400)
    api = web.client(target['profile'], 'lightsail' if target['service'] == 'lightsail' else 'cloudwatch', target['region'])
    incoming, outgoing = {}, {}
    for name, points in (('NetworkIn', incoming), ('NetworkOut', outgoing)):
        # Bounded requests also respect CloudWatch's per-call datapoint limit.
        for a in range(start, end, period*1000):
            b = min(end, a+period*1000)
            kwargs = dict(startTime=datetime.fromtimestamp(a, timezone.utc), endTime=datetime.fromtimestamp(b, timezone.utc))
            if target['service'] == 'lightsail':
                values = api.get_instance_metric_data(instanceName=target['instance'], metricName=name, period=300,
                        unit='Bytes', statistics=['Sum'], **kwargs).get('metricData', [])
                values = [(p['timestamp'], p.get('sum')) for p in values]
            else:
                values = api.get_metric_statistics(Namespace='AWS/EC2', MetricName=name,
                         Dimensions=[{'Name': 'InstanceId', 'Value': target['instance']}], Period=period,
                         StartTime=kwargs['startTime'], EndTime=kwargs['endTime'], Unit='Bytes', Statistics=['Sum']).get('Datapoints', [])
                values = [(p['Timestamp'], p.get('Sum')) for p in values]
            for timestamp, value in values:
                at = int(timestamp.timestamp())
                if start <= at < end and at % period == 0 and value is not None and value >= 0:
                    points[at] = float(value)
    with connect(web) as c:
        c.executemany('INSERT OR REPLACE INTO buckets VALUES(?,?,?,?,?,?)',
                      [(key(target), period, at, incoming.get(at), outgoing.get(at), int(at in incoming and at in outgoing))
                       for at in sorted(incoming.keys() | outgoing.keys())])
        for at in range(start//3600*3600, end//3600*3600, 3600):
            c.execute('''INSERT OR REPLACE INTO buckets SELECT resource,3600,?,SUM(inbound),SUM(outbound),SUM(count)
                         FROM buckets WHERE resource=? AND period=? AND at>=? AND at<? GROUP BY resource''',
                      (at, key(target), period, at, at+3600))
        for at in range(start//86400*86400, end//86400*86400, 86400):
            c.execute('''INSERT OR REPLACE INTO buckets SELECT resource,86400,?,SUM(inbound),SUM(outbound),SUM(count)
                         FROM buckets WHERE resource=? AND period=3600 AND at>=? AND at<? GROUP BY resource''',
                      (at, key(target), at, at+86400))
        c.execute('DELETE FROM buckets WHERE (period IN (60,300) AND at<?) OR (period=3600 AND at<?) OR (period=86400 AND at<?)',
                  (now-14*86400, now-90*86400, now-730*86400))
    return end


def evaluate(web, jobs, rule, end, simulate=False):
    payload, triggered = rule['payload'], []
    if not payload['enabled'] and not simulate:
        return triggered
    if not simulate and payload.get('maintenance_until') and datetime.fromisoformat(payload['maintenance_until']).timestamp() > time.time():
        return triggered
    with web.db() as c:
        paused = c.execute("SELECT value FROM app_settings WHERE name='automation_paused'").fetchone()
    if paused and paused[0] == 'true' and not simulate:
        return triggered
    groups = [payload['targets']] if payload.get('aggregation') == 'group' else [[t] for t in payload['targets']]
    for group in groups:
        if not group:
            continue
        target = group if payload.get('aggregation') == 'group' else group[0]
        start, _ = interval(payload['window'], end, payload['timezone'])
        points = [query(web, t, start, end) for t in group]
        data = {'complete': all(p['complete'] for p in points)}
        value = sum(p['rate_mbps'] if payload['metric'] == 'rate_mbps' else (
            p['inbound']+p['outbound'] if payload['metric'] == 'total' else p[payload['metric']])/1024**3 for p in points)
        if simulate:
            triggered.append({'target': target, 'value': value, 'complete': data['complete'], 'would_exceed': value >= payload['threshold']})
            continue
        target_key = 'group' if isinstance(target, list) else key(target)
        marker = datetime.fromtimestamp(end, timezone.utc).astimezone(ZoneInfo(payload['timezone'])).strftime('%Y-%m-%d' if payload['window'] == 'day' else '%Y-%m')
        with web.db() as c:
            c.execute('BEGIN IMMEDIATE')
            state = c.execute('SELECT * FROM v2_rule_state WHERE rule_id=? AND target_key=?', (rule['id'], target_key)).fetchone()
            state = dict(state) if state else {'streak': 0, 'active': 0, 'last_bucket': None, 'last_trigger': None, 'cycle': 0}
            if state['last_bucket'] == str(end):
                continue
            if not data['complete']:
                # A missing sample interrupts consecutive evaluation, never performs a mutation.
                state['streak'] = 0
                c.execute('INSERT OR REPLACE INTO v2_rule_state VALUES(?,?,?,?,?,?,?)',
                          (rule['id'], target_key, 0, state['active'], str(end), state['last_trigger'], state['cycle']))
                continue
            recovery = payload['recovery'] if payload['recovery'] is not None else payload['threshold']*.9
            if payload['window'] in ('day', 'month') and state['last_bucket']:
                previous_marker = datetime.fromtimestamp(int(state['last_bucket']), timezone.utc).astimezone(ZoneInfo(payload['timezone'])).strftime('%Y-%m-%d' if payload['window'] == 'day' else '%Y-%m')
                if previous_marker != marker:
                    state.update(active=0, streak=0, cycle=state['cycle']+1)
            elif value < recovery and state['active']:
                state.update(active=0, streak=0, cycle=state['cycle']+1)
            state['streak'] = state['streak']+1 if value >= payload['threshold'] else 0
            elapsed = time.time()-datetime.fromisoformat(state['last_trigger']).timestamp() if state['last_trigger'] else float('inf')
            today = datetime.now(ZoneInfo(payload['timezone'])).replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc).isoformat()
            count = c.execute('SELECT COUNT(*) FROM v2_events WHERE rule_id=? AND created_at>=?', (rule['id'], today)).fetchone()[0]
            fire = not state['active'] and state['streak'] >= payload['consecutive'] and elapsed >= payload['cooldown'] and count < payload['daily_limit']
            event_id = str(uuid4()) if fire else None
            if fire:
                state.update(active=1, last_trigger=stamp())
                c.execute('INSERT INTO v2_events VALUES(?,?,?,?,?,?,?)', (event_id, rule['id'], json.dumps(target), stamp(), value, None, json.dumps({'status': 'pending', 'rule_version': rule['version']})))
            c.execute('INSERT OR REPLACE INTO v2_rule_state VALUES(?,?,?,?,?,?,?)',
                      (rule['id'], target_key, state['streak'], state['active'], str(end), state['last_trigger'], state['cycle']))
        if event_id:
            triggered.append(event_id)
    return triggered


def dispatch(web, jobs):
    # Events are an outbox. A restart between enqueue and acknowledgement reuses the same key.
    with web.db() as c:
        events = c.execute('SELECT * FROM v2_events WHERE job_id IS NULL ORDER BY created_at LIMIT 100').fetchall()
    rules = {r['id']: r for r in documents(web, 'rule')}
    from .models import JobItem
    for event in events:
        event_detail = json.loads(event['detail'])
        if event_detail.get('next_at', 0) > time.time():
            continue
        rule = rules.get(event['rule_id'])
        if not rule or not rule['payload']['enabled'] or rule['version'] != json.loads(event['detail'])['rule_version']:
            with web.db() as c:
                c.execute("UPDATE v2_events SET job_id='canceled' WHERE id=?", (event['id'],))
            continue
        p, target, prepared = rule['payload'], json.loads(event['target']), []
        frozen = target if isinstance(target, list) else [target]
        with web.db() as c:
            paused = c.execute("SELECT value FROM app_settings WHERE name='automation_paused'").fetchone()
        if paused and paused[0] == 'true':
            continue
        if 'notify' in p['actions'] or p['mode'] == 'notify':
            with web.db() as c:
                c.execute('INSERT OR IGNORE INTO v2_deliveries(id,message) VALUES(?,?)',
                          (event['id'], f"AWSCC {rule['name']}: "+', '.join(t['profile']+'/'+t['instance'] for t in frozen)+f" value={event['value']:.3f}"))
        try:
            if p['mode'] == 'execute':
                for resource in frozen:
                    for action in p['actions']:
                        if action != 'notify':
                            try:
                                prepared.append(jobs.prepare(JobItem(kind=action, target=resource, script_id=p['script_id'], connection_id=p['connection_id'], parameters=p.get('script_parameters',{}))))
                            except Exception as exc:
                                if action != 'script':
                                    raise
                                prepared.append({'kind':'script','target':resource,'prepare_error':'Script preparation failed: '+type(exc).__name__})
            job = jobs.enqueue(prepared, 'event-'+event['id'], event['id'], 'automation') if prepared else None
            with web.db() as c:
                if 'notify' in p['actions'] or p['mode'] == 'notify':
                    c.execute('INSERT OR IGNORE INTO v2_deliveries(id,message) VALUES(?,?)',
                              (event['id'], f"AWSCC {rule['name']}: "+', '.join(t['profile']+'/'+t['instance'] for t in frozen)+f" value={event['value']:.3f}"))
                c.execute('UPDATE v2_events SET job_id=?,detail=? WHERE id=?',
                          (job['id'] if job else 'notification', json.dumps({'status': 'dispatched', 'rule_version': rule['version']}), event['id']))
        except Exception as exc:
            web.logger.warning('Automation event dispatch failed id=%s type=%s', event['id'], type(exc).__name__)
            attempts = event_detail.get('attempts',0)+1
            terminal = attempts >= 5 or (getattr(exc,'status_code',500) in (400,401,403,404,409))
            detail = {'status':'failed' if terminal else 'retrying','rule_version':rule['version'],
                      'attempts':attempts,'next_at':time.time()+min(3600,60*2**attempts),'error':type(exc).__name__}
            with web.db() as c:
                c.execute('UPDATE v2_events SET job_id=?,detail=? WHERE id=?', ('failed' if terminal else None,json.dumps(detail),event['id']))


def run_once(web, jobs, sender):
    heartbeat(web, 'monitor')
    rules = documents(web, 'rule')
    selector_errors = []
    for rule in rules:
        p = rule['payload']
        if p['enabled'] and p.get('selector'):
            try:
                p['targets'] = resources.select_targets(web, p['selector'], p.get('period',300))
                with web.db() as c:
                    c.execute('UPDATE v2_documents SET payload=? WHERE id=?', (web.vault().encrypt(json.dumps(p).encode()), rule['id']))
            except Exception as exc:
                p['targets'] = []
                selector_errors.append({'rule_id': rule['id'], 'error': type(exc).__name__})
    targets = {key(t): t for r in rules if r['payload']['enabled'] for t in r['payload']['targets']}
    ends, errors = {}, selector_errors
    with ThreadPoolExecutor(max_workers=6) as pool:
        pending = {pool.submit(collect, web, target): (k, target) for k, target in targets.items()}
        for future in as_completed(pending):
            k, target = pending[future]
            try:
                ends[k] = future.result()
            except Exception as exc:
                errors.append({'target': target, 'error': web.aws_error(exc)})
    for rule in rules:
        if rule['payload']['targets'] and all(key(t) in ends for t in rule['payload']['targets']):
            evaluate(web, jobs, rule, min(ends[key(t)] for t in rule['payload']['targets']))
    dispatch(web, jobs)
    with web.db() as c:
        deliveries = c.execute('SELECT * FROM v2_deliveries WHERE delivered=0 AND attempts<5 AND next_at<=?', (time.time(),)).fetchall()
    for row in deliveries:
        delivered = sender(row['message'])
        with web.db() as c:
            c.execute('UPDATE v2_deliveries SET delivered=?,attempts=attempts+1,next_at=? WHERE id=?',
                      (int(delivered), time.time()+min(3600, 60*2**row['attempts']), row['id']))
    heartbeat(web, 'monitor', json.dumps({'targets': len(targets), 'errors': errors})[:10000])
    return len(targets)
