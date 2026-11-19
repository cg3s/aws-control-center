import json
import time
from datetime import datetime, timezone
from controlplane import metrics
from controlplane.models import Rule
from controlplane.store import document


def target():
    return dict(profile='default', region='us-east-1', service='lightsail', instance='node',
                account_id='123456789012', partition='aws', generation='arn:node/2026-11-01')


def insert(web, t, start, end, amount=1024**3):
    with metrics.connect(web) as c:
        c.executemany('INSERT OR REPLACE INTO buckets VALUES(?,300,?,?,?,1)',
                      [(metrics.key(t), at, amount, 0) for at in range(start, end, 300)])


def test_disjoint_buckets_and_missing_vs_zero(control):
    web, _, _, _ = control
    t, end = target(), 7200
    insert(web, t, 3600, end, 0)
    with metrics.connect(web) as c:
        c.execute('INSERT INTO buckets VALUES(?,3600,3600,0,0,12)', (metrics.key(t),))
    data = metrics.query(web, t, 3600, end)
    assert data['complete'] and data['inbound'] == 0 and data['covered_buckets'] == 12
    data = metrics.query(web, t, 3300, end)
    assert not data['complete'] and data['covered_buckets'] == 12
    insert(web, t, 3300, 3600)
    data = metrics.query(web, t, 3300, end)
    assert data['complete'] and data['inbound'] == 1024**3


def test_collection_is_idempotent_and_handles_late_points(control):
    web, _, _, aws = control
    now = int(time.time())//3600*3600+900
    end = (now-600)//300*300
    point = {'timestamp': datetime.fromtimestamp(end-300, timezone.utc), 'sum': 100}
    aws.get_instance_metric_data.return_value = {'metricData': [point]}
    metrics.collect(web, target(), now)
    metrics.collect(web, target(), now)
    data = metrics.query(web, target(), end-300, end)
    assert data['complete'] and data['inbound'] == 100 and data['outbound'] == 100
    point['sum'] = 150
    metrics.collect(web, target(), now)
    assert metrics.query(web, target(), end-300, end)['inbound'] == 150


def test_month_timezone_boundaries():
    end = int(datetime(2026,11,1,0,5,tzinfo=timezone.utc).timestamp())
    start, _ = metrics.interval('month', end, 'Asia/Shanghai')
    assert datetime.fromtimestamp(start,timezone.utc).isoformat() == '2026-10-31T16:00:00+00:00'


def rule(web, **kwargs):
    payload = Rule(targets=[target()], threshold=.5, window='5m', **kwargs).model_dump()
    return {'id': 'rule-test', 'name': 'test', 'version': 1, 'payload': payload}


def test_consecutive_cooldown_and_no_duplicate_events(control):
    web, _, _, _ = control
    r = rule(web)
    insert(web, target(), 3000, 3900)
    assert metrics.evaluate(web, web.control_jobs, r, 3300) == []
    assert len(metrics.evaluate(web, web.control_jobs, r, 3600)) == 1
    assert metrics.evaluate(web, web.control_jobs, r, 3600) == []
    assert metrics.evaluate(web, web.control_jobs, r, 3900) == []
    with web.db() as c:
        assert c.execute('SELECT COUNT(*) FROM v2_events').fetchone()[0] == 1


def test_missing_data_breaks_consecutive_and_simulation_does_not_write(control):
    web, _, _, _ = control
    r = rule(web)
    insert(web, target(), 3000, 3300)
    metrics.evaluate(web, web.control_jobs, r, 3300)
    metrics.evaluate(web, web.control_jobs, r, 3600)
    insert(web, target(), 3600, 3900)
    assert metrics.evaluate(web, web.control_jobs, r, 3900) == []
    values = metrics.evaluate(web, web.control_jobs, r, 3900, simulate=True)
    assert values[0]['would_exceed']
    with web.db() as c:
        assert c.execute('SELECT COUNT(*) FROM v2_events').fetchone()[0] == 0


def test_rule_api_version_pause_and_credential_redaction(control):
    web, c, h, _ = control
    connection = {'kind':'connection','name':'ssh','payload':{'username':'root','fingerprint':'SHA256:'+'A'*43,'password':'connection-secret'}}
    response = c.post('/api/configurations', json=connection, headers=h)
    assert response.status_code == 200, response.text
    assert 'connection-secret' not in response.text
    assert 'connection-secret' not in c.get('/api/configurations?kind=connection').text
    data = {'kind':'rule','name':'watch','payload':rule(web)['payload']}
    first = c.post('/api/configurations',json=data,headers=h)
    assert first.status_code == 200, first.text
    second = c.post('/api/configurations',json=data,headers=h).json()
    assert second['version'] == 2
    assert not document(web, first.json()['id'])['payload']['enabled']
    assert c.post('/api/configurations/'+second['id']+'/enabled?enabled=false',headers=h).status_code == 200
    assert not document(web,second['id'])['payload']['enabled']


def test_notification_outbox_retry_is_independent(control):
    web, _, _, _ = control
    with web.db() as c:
        c.execute("INSERT INTO v2_deliveries(id,message) VALUES('notify','test')")
    metrics.run_once(web,web.control_jobs,lambda _:False)
    with web.db() as c:
        row=c.execute('SELECT * FROM v2_deliveries').fetchone()
        assert row['attempts']==1 and not row['delivered'] and row['next_at'] > time.time()
        c.execute('UPDATE v2_deliveries SET next_at=0')
    metrics.run_once(web,web.control_jobs,lambda _:True)
    with web.db() as c:
        assert c.execute('SELECT delivered FROM v2_deliveries').fetchone()[0] == 1


def test_event_dispatch_reuses_job_after_lost_acknowledgement(control):
    web,c,h,aws=control
    data={'kind':'rule','name':'automatic-stop','payload':rule(web,consecutive=1,mode='execute',actions=['notify','stop'])['payload']}
    response=c.post('/api/configurations',json=data,headers=h)
    assert response.status_code==200,response.text
    saved=document(web,response.json()['id'])
    end=int(time.time()-600)//300*300
    insert(web,target(),end-300,end)
    assert len(metrics.evaluate(web,web.control_jobs,saved,end))==1
    metrics.dispatch(web,web.control_jobs)
    with web.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM v2_jobs').fetchone()[0]==1
        conn.execute('UPDATE v2_events SET job_id=NULL')
    metrics.dispatch(web,web.control_jobs)
    with web.db() as conn:
        assert conn.execute('SELECT COUNT(*) FROM v2_jobs').fetchone()[0]==1
        assert conn.execute('SELECT COUNT(*) FROM v2_deliveries').fetchone()[0]==1
    web.control_jobs.run_once()
    aws.stop_instance.assert_called_once()


def test_one_minute_rule_requires_detailed_ec2_monitoring(control):
    _,c,h,aws=control
    payload=rule(None)['payload']
    payload['period']=60
    assert c.post('/api/configurations',headers=h,json={'kind':'rule','name':'minute','payload':payload}).status_code==400
    payload['targets'][0].update(service='ec2',instance='i-0123456789abcdef0')
    assert c.post('/api/configurations',headers=h,json={'kind':'rule','name':'minute','payload':payload}).status_code==400
    aws.describe_instances.return_value['Reservations'][0]['Instances'][0]['Monitoring']={'State':'enabled'}
    response=c.post('/api/configurations',headers=h,json={'kind':'rule','name':'minute','payload':payload})
    assert response.status_code==200,response.text
    assert response.json()['payload']['targets'][0]['period']==60


def test_group_threshold_freezes_all_targets_in_one_event(control):
    web,_,_,_=control
    a=target();b=target()|{'instance':'other','generation':'arn:other'}
    payload=Rule(targets=[a,b],threshold=1.5,window='5m',consecutive=1,aggregation='group').model_dump()
    insert(web,a,3000,3300);insert(web,b,3000,3300)
    saved={'id':'group','name':'group','version':1,'payload':payload}
    assert len(metrics.evaluate(web,web.control_jobs,saved,3300))==1
    with web.db() as c:
        row=c.execute("SELECT * FROM v2_events WHERE rule_id='group'").fetchone()
    assert len(json.loads(row['target']))==2 and row['value']==2


def test_creation_time_and_retained_first_hour_do_not_create_false_gaps(control):
    web,_,_,_=control
    t=target()|{'created_at':'1970-01-01T01:35:00+00:00'}
    insert(web,t,5700,7200,1)
    data=metrics.query(web,t,3600,7200)
    assert data['complete'] and data['expected_buckets']==5 and data['inbound']==5
    with metrics.connect(web) as c:
        c.execute('INSERT INTO buckets VALUES(?,3600,3600,5,0,5)',(metrics.key(t),))
        c.execute('DELETE FROM buckets WHERE period=300')
    assert metrics.query(web,t,3600,7200)['complete']


def test_tag_selector_uses_current_cached_membership(control):
    web,c,h,_=control
    from controlplane import resources
    from controlplane.store import stamp
    items=[{'id':'node','name':'node','profile':'default','region':'us-east-1','service':'lightsail','state':'running',
            'account_id':'123456789012','partition':'aws','resource_key':'aws/123456789012/us-east-1/lightsail/node',
            'generation':target()['generation'],'tags':{'environment':'production'}}]
    with web.db() as conn:
        conn.execute('INSERT INTO v2_inventory VALUES(?,?,?,?,?,?)',('default','123456789012','aws',stamp(),json.dumps(items),'[]'))
    selector={'profiles':[],'region':'all','service':'all','tags':{'environment':'production'}}
    data={'kind':'rule','name':'tags','payload':{'selector':selector,'threshold':1}}
    response=c.post('/api/configurations',json=data,headers=h)
    assert response.status_code==200,response.text
    assert response.json()['payload']['targets'][0]['instance']=='node'
    items[0]['tags']['environment']='development'
    with web.db() as conn:
        conn.execute('UPDATE v2_inventory SET items=?',(json.dumps(items),))
    assert resources.select_targets(web,selector)==[]


def test_rule_pause_cancels_tasks_even_when_event_ack_was_lost(control):
    web,c,h,aws=control
    response=c.post('/api/configurations',headers=h,json={'kind':'rule','name':'pause','payload':rule(web,consecutive=1,mode='execute',actions=['stop'])['payload']})
    saved=document(web,response.json()['id'])
    end=int(time.time()-600)//300*300
    insert(web,target(),end-300,end)
    metrics.evaluate(web,web.control_jobs,saved,end)
    metrics.dispatch(web,web.control_jobs)
    with web.db() as conn:
        conn.execute('UPDATE v2_events SET job_id=NULL')
    assert c.post('/api/configurations/'+saved['id']+'/enabled?enabled=false',headers=h).status_code==200
    assert not web.control_jobs.run_once()
    aws.stop_instance.assert_not_called()
