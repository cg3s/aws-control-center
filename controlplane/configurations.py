"""Shared configuration services used by the Web API and native CLI."""
import json
from uuid import uuid4
from datetime import datetime
from zoneinfo import ZoneInfo
from pydantic import ValidationError
from fastapi import HTTPException
from . import resources
from .models import Launch, Script, Connection, Rule
from .bootstrap import render
from .store import document, documents, stamp


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
            model = {'bootstrap': Launch, 'connection': Connection, 'script': Script, 'rule': Rule}[data.kind](**data.payload)
            payload = model.model_dump()
            if data.kind == 'bootstrap':
                render(model, 'lightsail')
                # Password input is supplied only per creation.
                if payload.get('root_password'):
                    raise HTTPException(400, 'Saved templates cannot contain root passwords; set them per creation')
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
            version = c.execute('SELECT COALESCE(MAX(version),0)+1 FROM v2_documents WHERE kind=? AND name=?', (data.kind, data.name)).fetchone()[0]
            if data.kind == 'rule':
                for old in c.execute("SELECT id,payload FROM v2_documents WHERE kind='rule' AND name=?", (data.name,)).fetchall():
                    old_payload = json.loads(self.web.vault().decrypt(old['payload']))
                    old_payload['enabled'] = False
                    c.execute('UPDATE v2_documents SET payload=? WHERE id=?',
                              (self.web.vault().encrypt(json.dumps(old_payload).encode()), old['id']))
                    c.execute('INSERT OR REPLACE INTO v2_rule_flags VALUES(?,0)', (old['id'],))
                    self.cancel_queue(c, old['id'])
            c.execute('INSERT INTO v2_documents VALUES(?,?,?,?,?,?,?)', (identifier, data.kind, data.name, version,
                      self.web.vault().encrypt(json.dumps(payload).encode()), stamp(), stamp()))
            if data.kind == 'rule':
                c.execute('INSERT INTO v2_rule_flags VALUES(?,?)', (identifier, int(payload['enabled'])))
        self.web.audit(actor, data.kind, resource=identifier, operation='save-version', detail=data.name)
        return self.public_document(document(self.web, identifier))


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


