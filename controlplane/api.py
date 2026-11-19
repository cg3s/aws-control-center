import json
import time
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from . import resources, metrics
from .jobs import Jobs
from .configurations import Configurations, rule_profiles
from .store import migrate, document, documents, stamp
from .models import BootstrapUpdate, JobRequest, ConfigDocument
from .models import Strict
from pydantic import Field


class AccountSetting(Strict):
    profile: str
    group_name: str = Field(default='', max_length=80)
    enabled: bool = True


def register(web):
    migrate(web)
    from .history import import_tasks
    import_tasks(web)
    jobs = Jobs(web)
    web.control_jobs = jobs
    configs_service = Configurations(web, jobs)
    router = APIRouter(prefix='/api', dependencies=[Depends(web.auth)])


    @router.get('/account-settings')
    def account_settings():
        with web.db() as c:
            saved = {r['profile']: dict(r) for r in c.execute('SELECT * FROM v2_account_settings')}
            cached = {r['profile']: dict(r) for r in c.execute('SELECT profile,account_id,errors,fetched_at FROM v2_inventory')}
        return {'items': [saved.get(p, {'profile': p, 'group_name': '', 'enabled': True}) | cached.get(p, {})
                          for p in web.available_profiles()]}

    @router.post('/account-settings')
    def save_account_settings(data: AccountSetting, request: Request):
        web.valid_profile(data.profile)
        with web.db() as c:
            c.execute('INSERT OR REPLACE INTO v2_account_settings VALUES(?,?,?)', (data.profile, data.group_name, int(data.enabled)))
            if not data.enabled:
                c.execute("DELETE FROM app_settings WHERE name='active_profile' AND value=?", (data.profile,))
            c.execute('DELETE FROM v2_reports')
        web.audit(web.ip_of(request), 'account', profile=data.profile, operation='settings', detail=f'enabled={data.enabled}; group={data.group_name}')
        return data.model_dump()

    @router.get('/resources')
    def inventory(profiles: str = '', region: str = 'all', service: str = 'all', search: str = '',
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500), group: str = ''):
        return resources.inventory(web, profiles, region, service, search, offset, limit, group)

    @router.post('/resources/refresh')
    def refresh(request: Request, profile: str | None = None):
        profile = web.require_selected_profile(profile)
        if profile not in resources.enabled_profiles(web):
            raise HTTPException(400, 'Unknown or disabled account')
        key = 'inventory_refresh:' + profile
        now = time.time()
        with web.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute('SELECT value FROM app_settings WHERE name=?', (key,)).fetchone()
            if row and now - float(row['value']) < 30:
                raise HTTPException(429, '请等待 30 秒后再刷新此账户的清单')
            c.execute('INSERT OR REPLACE INTO app_settings VALUES(?,?)', (key, str(now)))
        refreshed = resources.refresh_profile(web, profile)
        web.audit(web.ip_of(request), 'inventory', profile=profile, operation='refresh', result='ok' if refreshed else 'failed')
        if not refreshed:
            raise HTTPException(502, 'AWS 清单刷新失败，已保留上次结果')
        return {'status': 'refreshed'}

    @router.get('/catalog')
    def catalog(profile: str, region: str, service: str):
        if service not in ('ec2', 'lightsail'):
            raise HTTPException(400, 'Unsupported service')
        try:
            return resources.catalog(web, profile, region, service)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, web.aws_error(exc))

    @router.get('/tasks')
    def tasks(limit: int = Query(50, ge=1, le=100), profile: str | None = None):
        with web.db() as c:
            rows = c.execute('''SELECT j.id FROM v2_jobs j WHERE ? IS NULL OR EXISTS (
                    SELECT 1 FROM v2_items i WHERE i.job_id=j.id AND json_extract(i.target,'$.profile')=?
                ) ORDER BY j.created_at DESC LIMIT ?''', (profile, profile, limit)).fetchall()
        return {'items': [jobs.detail(r['id'], profile) for r in rows]}

    @router.post('/tasks/preview')
    def preview(data: JobRequest, request: Request):
        try:
            return jobs.preview(data, web.ip_of(request))
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502, web.aws_error(exc))

    @router.post('/tasks')
    def submit(data: JobRequest, request: Request):
        return jobs.submit(data, web.ip_of(request))

    @router.get('/tasks/{identifier}')
    def detail(identifier: str, profile: str | None = None):
        return jobs.detail(identifier, profile)

    @router.post('/tasks/{identifier}/cancel')
    def cancel(identifier: str, request: Request, profile: str | None = None):
        return jobs.cancel(identifier, web.ip_of(request), profile)

    @router.post('/tasks/{identifier}/verify')
    def verify(identifier: str, request: Request, profile: str | None = None):
        return jobs.verify(identifier, web.ip_of(request), profile)

    @router.get('/task-items/{identifier}/output')
    def output(identifier: str, request: Request, profile: str | None = None):
        with web.db() as c:
            row = c.execute("""SELECT payload FROM v2_items WHERE id=? AND (? IS NULL OR json_extract(target,'$.profile')=?)""",
                            (identifier, profile, profile)).fetchone()
        if not row:
            raise HTTPException(404, 'Task item not found')
        payload = json.loads(web.vault().decrypt(row[0]))
        web.audit(web.ip_of(request), 'script', profile=profile or '', resource=identifier, operation='read-output')
        return payload.get('execution', {})


    @router.get('/configurations')
    def configs(kind: str, profile: str | None = None):
        if kind not in ('bootstrap', 'script', 'connection', 'rule'):
            raise HTTPException(400, 'Unsupported configuration kind')
        rows = documents(web, kind)
        if profile is not None and kind == 'rule':
            rows = [row for row in rows if rule_profiles(row['payload']) == {profile}]
        if profile is not None and kind == 'connection':
            with web.db() as c:
                account = c.execute('SELECT partition, account_id FROM v2_inventory WHERE profile=?', (profile,)).fetchone()
            prefix = f"{account['partition']}/{account['account_id']}/" if account and account['account_id'] else None
            rows = [row for row in rows if not (resource := row['payload'].get('resource_key')) or (prefix and resource.startswith(prefix))]
        return {'items': [configs_service.public_document(row) for row in rows]}

    @router.post('/configurations')
    def save(data: ConfigDocument, request: Request):
        return configs_service.save(data, web.ip_of(request))

    @router.put('/configurations/{identifier}')
    def update_bootstrap(identifier: str, data: BootstrapUpdate, request: Request):
        return configs_service.update_bootstrap(identifier, data, web.ip_of(request))

    @router.post('/configurations/{identifier}/enabled')
    def enabled(identifier: str, request: Request, enabled: bool):
        return configs_service.set_enabled(identifier, enabled, web.ip_of(request))

    @router.delete('/configurations/{identifier}')
    def delete_config(identifier: str, request: Request):
        return configs_service.delete(identifier, web.ip_of(request))

    @router.post('/rules/{identifier}/simulate')
    def simulate(identifier: str):
        rule = document(web, identifier, 'rule')
        if rule['payload'].get('selector'):
            rule['payload']['targets'] = resources.select_targets(web, rule['payload']['selector'], rule['payload'].get('period',300))
        return {'items': metrics.evaluate(web, jobs, rule, int(time.time()-600)//300*300, simulate=True)}

    @router.get('/events')
    def events(profile: str | None = None):
        with web.db() as c:
            rows = [dict(r) for r in c.execute('''SELECT * FROM v2_events WHERE ? IS NULL OR
                json_extract(target,'$.profile')=? OR EXISTS (
                    SELECT 1 FROM json_each(CASE WHEN json_type(target)='array' THEN target ELSE '[]' END)
                    WHERE json_extract(value,'$.profile')=?
                ) ORDER BY created_at DESC LIMIT 200''', (profile, profile, profile))]
            if profile is None:
                deliveries = [dict(r) for r in c.execute('SELECT id,attempts,next_at,delivered FROM v2_deliveries ORDER BY rowid DESC LIMIT 200')]
            else:
                ids = [row['id'] for row in rows]
                placeholders = ','.join('?' for _ in ids)
                deliveries = [dict(r) for r in c.execute(f'''SELECT id,attempts,next_at,delivered FROM v2_deliveries
                    WHERE id IN ({placeholders}) ORDER BY rowid DESC''', ids)] if ids else []
            historical = [dict(r) for r in c.execute('''SELECT a.* FROM alerts a LEFT JOIN monitors m ON a.monitor_id=m.id
                WHERE ? IS NULL OR m.profile=? ORDER BY a.id DESC LIMIT 100''', (profile, profile))]
            samples = [dict(r) for r in c.execute('''SELECT s.* FROM monitor_samples s LEFT JOIN monitors m ON s.monitor_id=m.id
                WHERE ? IS NULL OR m.profile=? ORDER BY s.measured_at DESC LIMIT 100''', (profile, profile))]
        if profile is not None:
            for row in rows:
                targets = json.loads(row['target'])
                if isinstance(targets, list):
                    matching = [target for target in targets if target['profile'] == profile]
                    if len(matching) != len(targets):
                        row.update(target=json.dumps(matching), value=None)
        return {'items': rows, 'deliveries': deliveries, 'historical_alerts': historical, 'historical_samples': samples}

    @router.post('/automation/pause')
    def pause(request: Request, paused: bool = True):
        with web.db() as c:
            c.execute("INSERT OR REPLACE INTO app_settings VALUES('automation_paused',?)", ('true' if paused else 'false',))
        web.audit(web.ip_of(request), 'automation', operation='pause' if paused else 'resume')
        return {'paused': paused}

    @router.get('/traffic')
    def traffic(profile: str, region: str, service: str, instance: str,
                window: str = '1h', timezone_name: str = 'UTC', period: int = 300):
        try:
            return metrics.report(web,profile,region,service,instance,window,timezone_name,period)
        except HTTPException:
            raise
        except Exception as exc:
            raise HTTPException(502,web.aws_error(exc))

    @router.get('/traffic/summary')
    def traffic_summary(profiles: str = '', region: str = 'all', service: str = 'all', window: str = '1h'):
        if window not in ('5m', '1h', '24h', '7d', '30d', 'day', 'month'):
            raise HTTPException(400, 'Unsupported window')
        chosen = set(profiles.split(',')) if profiles else set(web.available_profiles())
        targets = {}
        for rule in documents(web, 'rule'):
            if not rule['payload']['enabled']:
                continue
            for t in rule['payload']['targets']:
                if t['profile'] not in chosen or (region != 'all' and t['region'] != region) or (service != 'all' and t['service'] != service):
                    continue
                physical = metrics.key(t).rsplit('/', 1)[0]
                if physical not in targets or t.get('period',300) < targets[physical].get('period',300):
                    targets[physical] = t
        end = int(time.time()-600)//300*300
        start, end = metrics.interval(window, end)
        items = [metrics.query(web, t, start, end) | {'target': t} for t in targets.values()]
        items.sort(key=lambda x: x['inbound']+x['outbound'], reverse=True)
        return {'items': items, 'inbound': sum(x['inbound'] for x in items), 'outbound': sum(x['outbound'] for x in items),
                'complete': bool(items) and all(x['complete'] for x in items), 'end': end}

    @router.get('/health')
    def health(profiles: str = ''):
        with web.db() as c:
            workers = [dict(r) for r in c.execute('SELECT * FROM v2_workers')]
            counts = {r[0]: r[1] for r in c.execute('''SELECT status,COUNT(*) FROM v2_items
                WHERE ?='' OR json_extract(target,'$.profile') IN (SELECT value FROM json_each(?)) GROUP BY status''',
                (profiles, json.dumps(profiles.split(','))))}
            paused = c.execute("SELECT value FROM app_settings WHERE name='automation_paused'").fetchone()
        if profiles:
            for worker in workers:
                worker['detail'] = ''
        return {'workers': workers, 'task_counts': counts, 'automation_paused': bool(paused and paused[0] == 'true')}

    @router.get('/dashboard/summary')
    def dashboard(profiles: str = '', region: str = 'all', service: str = 'all'):
        data = resources.inventory(web, profiles, region, service, limit=100000)
        items = data['items']
        return {'total': len(items), 'running': sum(x['state'] == 'running' for x in items),
                'stopped': sum(x['state'] == 'stopped' for x in items), 'errors': data['errors'],
                'updated_at': data['updated_at']} | health(profiles)

    web.app.include_router(router)
