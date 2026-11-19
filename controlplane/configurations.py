"""Shared configuration services used by the Web API and native CLI."""
import json
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from fastapi import HTTPException
from . import resources
from .models import Launch, Script, Connection, Rule
from .bootstrap import validate_launch
from .store import document, documents, stamp


def rule_profiles(payload):
    selector = payload.get('selector')
    return set(selector['profiles']) if selector is not None else {target['profile'] for target in payload['targets']}


def bootstrap_payload(fields):
    try:
        launch = Launch(**fields)
    except ValidationError:
        raise HTTPException(400, 'Invalid configuration fields') from None
    if launch.root_password:
        raise HTTPException(400, 'Saved templates cannot contain root passwords; set them per creation')
    validate_launch(launch, template=True)
    return launch.model_dump()


class Configurations:
    def __init__(self, web, jobs):
        self.web, self.jobs = web, jobs

    def public_document(self, row):
        data = dict(row)
        p = dict(row['payload'])
        if row['kind'] == 'connection':
            p = {k: v for k, v in p.items() if k not in ('private_key', 'password')}
            p['credential_configured'] = True
        elif row['kind'] == 'bootstrap':
            p.pop('root_password', None)
        data['payload'] = p
        return data


    def cancel_queue(self, c, rule_id):
        ids = [r[0] for r in c.execute("SELECT j.id FROM v2_jobs j JOIN v2_events e ON j.request_key='event-'||e.id WHERE e.rule_id=?", (rule_id,))]
        for identifier in ids:
            c.execute("UPDATE v2_items SET status='canceled',updated_at=? WHERE status='queued' AND job_id=?", (stamp(),identifier))
            self.jobs.summarize(c, identifier)


    def save(self, data, actor):
        try:
            if data.kind == 'bootstrap':
                payload = bootstrap_payload(data.payload)
            else:
                model = {'connection': Connection, 'script': Script, 'rule': Rule}[data.kind]
                payload = model(**data.payload).model_dump()
            if data.kind == 'connection' and not (payload['private_key'] or payload['password']):
                raise HTTPException(400, 'Supply a private key or password')
            if data.kind == 'rule':
                ZoneInfo(payload['timezone'])
                if payload['selector']:
                    for profile in payload['selector']['profiles']:
                        self.web.valid_profile(profile)
                    if payload['selector']['region'] != 'all':
                        self.web.check_region(payload['selector']['region'])
                    payload['targets'] = resources.select_targets(self.web, payload['selector'], payload['period'])
                elif not payload['targets']:
                    raise HTTPException(400, 'Choose explicit targets or a tag selector')
                if payload['recovery'] is not None and payload['recovery'] >= payload['threshold']:
                    raise HTTPException(400, 'Recovery must be below threshold')
                if len(set(payload['actions'])) != len(payload['actions']):
                    raise HTTPException(400, 'Duplicate rule actions')
                if payload['maintenance_until']:
                    dt = datetime.fromisoformat(payload['maintenance_until'])
                    if dt.tzinfo is None:
                        raise HTTPException(400, 'Maintenance timestamp requires timezone')
                for t in payload['targets']:
                    t['period'] = payload['period']
                    self.web.valid_profile(t['profile'])
                    self.web.check_region(t['region'])
                    t['account_id'], t['partition'] = resources.identity(self.web, t['profile'])
                    observed = resources.observe(self.web, t)
                    t['generation'] = observed['generation']
                    t['created_at'] = observed.get('created_at', '')
                    if t['period'] == 60 and (t['service'] != 'ec2' or not observed.get('detailed_monitoring')):
                        raise HTTPException(400, 'One-minute metrics require existing EC2 detailed monitoring')
                if 'script' in payload['actions']:
                    document(self.web, payload['script_id'], 'script')
                    if payload['connection_id']:
                        document(self.web, payload['connection_id'], 'connection')
        except HTTPException:
            raise
        except (ValidationError, ValueError, KeyError):
            raise HTTPException(400, 'Invalid configuration fields')
        identifier = str(uuid4())
        with self.web.db() as c:
            c.execute('BEGIN IMMEDIATE')
            versions = c.execute('SELECT id,version,payload FROM v2_documents WHERE kind=? AND name=?', (data.kind, data.name)).fetchall()
            if data.kind == 'rule':
                profiles = rule_profiles(payload)
                previous = []
                for old in versions:
                    old_payload = json.loads(self.web.vault().decrypt(old['payload']))
                    if rule_profiles(old_payload) != profiles:
                        continue
                    previous.append(old)
                    old_payload['enabled'] = False
                    c.execute('UPDATE v2_documents SET payload=? WHERE id=?',
                              (self.web.vault().encrypt(json.dumps(old_payload).encode()), old['id']))
                    c.execute('INSERT OR REPLACE INTO v2_rule_flags VALUES(?,0)', (old['id'],))
                    self.cancel_queue(c, old['id'])
                versions = previous
            version = max((old['version'] for old in versions), default=0) + 1
            c.execute('INSERT INTO v2_documents VALUES(?,?,?,?,?,?,?)', (identifier, data.kind, data.name, version,
                      self.web.vault().encrypt(json.dumps(payload).encode()), stamp(), stamp()))
            if data.kind == 'rule':
                c.execute('INSERT INTO v2_rule_flags VALUES(?,?)', (identifier, int(payload['enabled'])))
        profiles = rule_profiles(payload) if data.kind == 'rule' else set()
        self.web.audit(actor, data.kind, profile=next(iter(profiles)) if len(profiles) == 1 else '',
                       resource=identifier, operation='save-version', detail=data.name)
        return self.public_document(document(self.web, identifier))


    def update_bootstrap(self, identifier, data, actor):
        payload = bootstrap_payload(data.payload)
        encrypted = self.web.vault().encrypt(json.dumps(payload).encode())
        with self.web.db() as c:
            c.execute('BEGIN IMMEDIATE')
            row = c.execute("SELECT * FROM v2_documents WHERE id=? AND kind='bootstrap'", (identifier,)).fetchone()
            if row is None:
                raise HTTPException(404, 'Configuration not found')
            if row['version'] != data.version:
                raise HTTPException(409, '模板版本已更新，请刷新页面后重新编辑。')
            latest = c.execute("SELECT MAX(version) FROM v2_documents WHERE kind='bootstrap' AND name=?", (data.name,)).fetchone()[0]
            version = max(row['version'], latest or 0) + 1
            updated_at = stamp()
            c.execute('UPDATE v2_documents SET name=?,version=?,payload=?,updated_at=? WHERE id=?',
                      (data.name, version, encrypted, updated_at, identifier))
        self.web.audit(actor, 'bootstrap', resource=identifier, operation='edit', detail=data.name)
        return self.public_document(dict(row) | dict(name=data.name, version=version, payload=payload, updated_at=updated_at))


    def set_enabled(self, identifier, enabled, actor):
        row = document(self.web, identifier, 'rule')
        if enabled:
            p = row['payload']
            if p.get('selector'):
                p['targets'] = resources.select_targets(self.web,p['selector'],p.get('period',300))
            for target in p['targets']:
                self.web.valid_profile(target['profile'])
                self.web.check_region(target['region'])
                if target['profile'] not in resources.enabled_profiles(self.web):
                    raise HTTPException(409,'Account is disabled')
                account,partition = resources.identity(self.web,target['profile'])
                if target.get('account_id') and (account != target['account_id'] or partition != target['partition']):
                    raise HTTPException(409,'Account identity changed; save a reviewed rule version')
                current = resources.observe(self.web,target)
                if target.get('generation') and current['generation'] != target['generation']:
                    raise HTTPException(409,'Instance was replaced; save a reviewed rule version')
                target.update(account_id=account,partition=partition,generation=current['generation'],created_at=current.get('created_at',''))
        row['payload']['enabled'] = enabled
        with self.web.db() as c:
            c.execute('UPDATE v2_documents SET payload=?,updated_at=? WHERE id=?',
                      (self.web.vault().encrypt(json.dumps(row['payload']).encode()), stamp(), identifier))
            c.execute('INSERT OR REPLACE INTO v2_rule_flags VALUES(?,?)', (identifier, int(enabled)))
            if not enabled:
                self.cancel_queue(c, identifier)
        self.web.audit(actor, 'rule', resource=identifier, operation='enable' if enabled else 'disable')
        return {'enabled': enabled}


    def delete(self, identifier, actor):
        row = document(self.web, identifier)
        if row['kind'] not in ('bootstrap', 'connection', 'script', 'rule'):
            raise HTTPException(400, 'Cannot delete this record')
        for rule in documents(self.web, 'rule'):
            if identifier in (rule['payload'].get('script_id'), rule['payload'].get('connection_id')):
                raise HTTPException(409, 'Configuration is referenced by a rule')
        with self.web.db() as c:
            pending = c.execute("SELECT 1 FROM v2_items WHERE status NOT IN ('succeeded','failed','unknown','canceled') AND job_id IN (SELECT j.id FROM v2_jobs j JOIN v2_events e ON j.request_key='event-'||e.id WHERE e.rule_id=?)", (identifier,)).fetchone()
            if pending:
                raise HTTPException(409, 'Cancel active automation tasks first')
            c.execute('DELETE FROM v2_documents WHERE id=?', (identifier,))
            c.execute('DELETE FROM v2_rule_flags WHERE rule_id=?', (identifier,))
        self.web.audit(actor, row['kind'], resource=identifier, operation='delete')
        return {'ok': True}


