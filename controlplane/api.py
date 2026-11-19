import json
import time
from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request, Query
from . import resources, metrics
from .jobs import Jobs
from .configurations import Configurations
from .store import migrate, document, documents, stamp
from .models import JobRequest, ConfigDocument
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
            c.execute('DELETE FROM v2_reports')
        web.audit(web.ip_of(request), 'account', profile=data.profile, operation='settings', detail=f'enabled={data.enabled}; group={data.group_name}')
        return data.model_dump()

    @router.get('/resources')
    def inventory(profiles: str = '', region: str = 'all', service: str = 'all', search: str = '',
                  offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=500), group: str = ''):
        return resources.inventory(web, profiles, region, service, search, offset, limit, group)

    @router.post('/resources/refresh')
    def refresh(request: Request):
        with web.db() as c:
            row = c.execute("SELECT seen_at FROM v2_workers WHERE name='inventory'").fetchone()
            if row and time.time()-datetime.fromisoformat(row[0]).timestamp() < 30:
                raise HTTPException(429, 'Inventory refresh is limited to once every 30 seconds')
            c.execute("INSERT OR REPLACE INTO app_settings VALUES('inventory_refresh','true')")
        web.audit(web.ip_of(request), 'inventory', operation='refresh')
        return {'status': 'queued'}

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
    def tasks(limit: int = Query(50, ge=1, le=100)):
        with web.db() as c:
            rows = c.execute('SELECT * FROM v2_jobs ORDER BY created_at DESC LIMIT ?', (limit,)).fetchall()
        return {'items': [jobs.detail(r['id']) for r in rows]}

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
    def detail(identifier: str):
        return jobs.detail(identifier)

    @router.post('/tasks/{identifier}/cancel')
    def cancel(identifier: str, request: Request):
        return jobs.cancel(identifier, web.ip_of(request))

    @router.post('/tasks/{identifier}/verify')
    def verify(identifier: str, request: Request):
        jobs.detail(identifier)
        return jobs.verify(identifier, web.ip_of(request))

    @router.get('/task-items/{identifier}/output')
    def output(identifier: str, request: Request):
        with web.db() as c:
            row = c.execute('SELECT payload FROM v2_items WHERE id=?', (identifier,)).fetchone()
        if not row:
            raise HTTPException(404, 'Task item not found')
        payload = json.loads(web.vault().decrypt(row[0]))
        web.audit(web.ip_of(request), 'script', resource=identifier, operation='read-output')
        return payload.get('execution', {})


    @router.get('/configurations')
    def configs(kind: str):
        if kind not in ('bootstrap', 'script', 'connection', 'rule'):
            raise HTTPException(400, 'Unsupported configuration kind')
        return {'items': [configs_service.public_document(row) for row in documents(web, kind)]}

    @router.post('/configurations')
    def save(data: ConfigDocument, request: Request):
        return configs_service.save(data, web.ip_of(request))

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
    def events():
        with web.db() as c:
            rows = [dict(r) for r in c.execute('SELECT * FROM v2_events ORDER BY created_at DESC LIMIT 200')]
            deliveries = [dict(r) for r in c.execute('SELECT id,attempts,next_at,delivered FROM v2_deliveries ORDER BY rowid DESC LIMIT 200')]
            historical = [dict(r) for r in c.execute('SELECT * FROM alerts ORDER BY id DESC LIMIT 100')]
            samples = [dict(r) for r in c.execute('SELECT * FROM monitor_samples ORDER BY measured_at DESC LIMIT 100')]
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
    def health():
        with web.db() as c:
            workers = [dict(r) for r in c.execute('SELECT * FROM v2_workers')]
            counts = {r[0]: r[1] for r in c.execute('SELECT status,COUNT(*) FROM v2_items GROUP BY status')}
            paused = c.execute("SELECT value FROM app_settings WHERE name='automation_paused'").fetchone()
        return {'workers': workers, 'task_counts': counts, 'automation_paused': bool(paused and paused[0] == 'true')}

    @router.get('/dashboard/summary')
    def dashboard(profiles: str = '', region: str = 'all', service: str = 'all'):
        data = resources.inventory(web, profiles, region, service, limit=100000)
        items = data['items']
        return {'total': len(items), 'running': sum(x['state'] == 'running' for x in items),
                'stopped': sum(x['state'] == 'stopped' for x in items), 'errors': data['errors'],
                'updated_at': data['updated_at']} | health()

    web.app.include_router(router)
