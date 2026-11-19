import json
from controlplane import resources


def test_only_one_ui_and_api(control):
    web,c,h,_=control
    assert c.get('/legacy').status_code == 404
    assert c.get('/api/instances').status_code == 404
    assert c.post('/api/instance/action',headers=h,json={}).status_code == 404
    assert c.get('/api/v2/resources').status_code == 404
    paths=list(web.app.openapi()['paths'])
    assert paths.count('/api/traffic') == 1
    assert set(web.app.openapi()['paths']['/api/tasks']) == {'get', 'post'}
    assert c.get('/').status_code == 200
    assert c.get('/v8/assets/../../app.py').status_code == 404


def test_cached_inventory_deduplicates_account_aliases(control,monkeypatch):
    web,c,_,_=control
    monkeypatch.setattr(web,'available_profiles',lambda:['default','alias'])
    monkeypatch.setattr(web,'instances',lambda **kw:{'items':[{'id':'node','name':'node','profile':kw['profile'],'region':'us-east-1','service':'lightsail','state':'running'}], 'errors':[]})
    resources.refresh(web)
    web.instances=lambda **kw: (_ for _ in ()).throw(AssertionError('cache endpoint must not call AWS inventory'))
    data=c.get('/api/resources').json()
    assert data['total'] == 1 and len(data['updated_at']) == 2
    assert c.get('/api/resources?profiles=missing').status_code == 400
    assert c.get('/api/dashboard/summary').json()['running'] == 1


def test_sanitized_validation_does_not_echo_secrets(control):
    _,c,h,_=control
    secret='secret-never-echo-this'
    response=c.post('/api/configurations',json={'kind':'connection','name':'x','payload':secret},headers=h)
    assert response.status_code == 422 and secret not in response.text
    assert c.post('/api/tasks',json={'items':secret},headers=h).status_code == 422


def test_cli_uses_same_queue_and_requires_confirmation(control,capsys):
    web,_,_,_=control
    import cli
    assert cli.main(['automation','pause'],api=web) == 2
    assert cli.main(['automation','pause','--yes'],api=web) == 0
    assert cli.main(['tasks','list','--json'],api=web) == 0
    assert 'items' in capsys.readouterr().out
    assert cli.main(['traffic','report'],api=web) == 2


def test_migration_preserves_historical_request_semantics(control):
    web,_,_,_=control
    from controlplane.history import import_tasks
    with web.db() as c:
        c.executescript('''CREATE TABLE tasks(id TEXT,profile TEXT,region TEXT,status TEXT,created_at TEXT,updated_at TEXT);
          CREATE TABLE task_items(id INTEGER,task_id TEXT,resource_name TEXT,status TEXT,error TEXT);''')
        c.execute("INSERT INTO tasks VALUES('old','default','us-east-1','completed','2026-01-01','2026-01-01')")
        c.execute("INSERT INTO task_items VALUES(1,'old','node','submitted','')")
    import_tasks(web);import_tasks(web)
    job=web.control_jobs.detail('historical-old')
    assert job['status']=='archived' and len(job['items']) == 1
    assert job['items'][0]['result']['legacy_status'] == 'submitted'
    assert not web.control_jobs.run_once()


def test_catalog_paginates_and_filters_windows(control):
    web,c,_,aws=control
    aws.get_bundles.side_effect=[{'bundles':[], 'nextPageToken':'next'}, {'bundles':[{'bundleId':'micro','price':5}]}]
    aws.get_blueprints.return_value={'blueprints':[{'blueprintId':'windows','platform':'WINDOWS'},{'blueprintId':'debian','platform':'LINUX_UNIX'}]}
    aws.get_regions.return_value={'regions':[{'name':'us-east-1','availabilityZones':[{'zoneName':'us-east-1a','state':'available'}]}]}
    data=c.get('/api/catalog?profile=default&region=us-east-1&service=lightsail').json()
    assert data['bundles'][0]['id']=='micro'
    assert [x['id'] for x in data['blueprints']]==['debian']
    assert aws.get_bundles.call_args.kwargs['pageToken']=='next'


def test_login_failure_counter_persists(control):
    _,c,_,_=control
    for _ in range(5):
        assert c.post('/api/login',json={'password':'wrong'}).status_code==401
    assert c.post('/api/login',json={'password':'control-test-password-1234'}).status_code==429


def test_disabled_accounts_block_writes_and_remain_manageable(control):
    web,c,h,_=control
    result=c.post('/api/account-settings',headers=h,json={'profile':'default','group_name':'production','enabled':False})
    assert result.status_code==200
    assert c.get('/api/account-settings').json()['items'][0]['group_name']=='production'
    from test_tasks import request
    assert c.post('/api/tasks/preview',headers=h,json=request().model_dump()).status_code==409
    assert c.get('/api/resources').json()['total']==0


def test_inventory_capacity_20_accounts_500_instances(control,monkeypatch):
    web,c,_,_=control
    profiles=['account'+str(i) for i in range(20)]
    monkeypatch.setattr(web,'available_profiles',lambda:profiles)
    monkeypatch.setattr(resources,'identity',lambda w,p:(str(100000000000+profiles.index(p)),'aws'))
    calls=[]
    def scan(**kw):
        calls.append(kw['profile'])
        return {'items':[{'id':'node-'+str(i),'name':'node-'+str(i),'profile':kw['profile'],'region':'us-east-1','service':'lightsail','state':'running'} for i in range(25)],'errors':[]}
    monkeypatch.setattr(web,'instances',scan)
    resources.refresh(web)
    assert c.get('/api/resources?offset=450&limit=50').json()['total']==500
    assert len(c.get('/api/resources?offset=450&limit=50').json()['items'])==50
    assert c.get('/api/dashboard/summary').json()['total']==500
    assert len(calls)==20


def test_cost_report_is_cached_and_deduplicates_aliases(control,monkeypatch):
    web,c,_,_=control
    monkeypatch.setattr(web,'available_profiles',lambda:['default','alias'])
    calls=[]
    def fetch(profile):
        calls.append(profile)
        return {'month':'2026-11','total':5,'items':[{'name':'Lightsail','amount':5}]}
    monkeypatch.setattr(web,'_fetch_costs',fetch)
    first=c.get('/api/cost?profile=all').json()
    assert first['total']==5 and len(first['accounts'])==1
    assert c.get('/api/cost?profile=all').json()['total']==5
    assert len(calls)==1


def test_build_asset_links_are_served(control):
    import re
    _,c,_,_=control
    page=c.get('/')
    assert page.status_code==200
    links=re.findall(r'(?:src|href)="(/assets/[^\"]+)"',page.text)
    assert links
    for link in links:
        assert c.get(link).status_code==200


def test_rule_cli_reuses_web_configuration_service(control,tmp_path,capsys):
    web,c,_,_=control
    import cli
    path=tmp_path/'rule.json'
    path.write_text(json.dumps({'name':'cli-rule','payload':{'targets':[{'profile':'default','region':'us-east-1','service':'lightsail','instance':'node'}],'threshold':1}}),encoding='utf-8')
    assert cli.main(['rules','save','--file',str(path)],api=web)==2
    assert cli.main(['rules','save','--file',str(path),'--yes'],api=web)==0
    saved=c.get('/api/configurations?kind=rule').json()['items'][0]
    assert saved['name']=='cli-rule'
    assert cli.main(['rules','disable',saved['id'],'--yes'],api=web)==0
    assert c.get('/api/configurations?kind=rule').json()['items'][0]['payload']['enabled'] is False
    assert cli.main(['rules','delete',saved['id'],'--yes'],api=web)==0
    assert c.get('/api/configurations?kind=rule').json()['items']==[]


def test_unreachable_legacy_monitor_is_imported_disabled_and_can_be_deleted(control):
    web,c,h,aws=control
    from controlplane.history import import_monitors
    from botocore.exceptions import ClientError
    with web.db() as conn:
        conn.execute("INSERT INTO monitors(profile,region,instance,threshold_gib,enabled,created_at) VALUES('default','us-east-1','gone',1,1,'2026-01-01')")
    aws.get_instance.side_effect=ClientError({'Error':{'Code':'NotFoundException'}},'GetInstance')
    import_monitors(web)
    entries=c.get('/api/configurations?kind=rule').json()['items']
    assert len(entries)==1 and entries[0]['payload']['enabled'] is False
    identifier=entries[0]['id']
    assert c.delete('/api/configurations/'+identifier,headers=h).status_code==200
    aws.get_instance.reset_mock()
    import_monitors(web)
    aws.get_instance.assert_not_called()
    assert c.get('/api/configurations?kind=rule').json()['items']==[]
