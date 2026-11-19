"""One-way data import, not a second API or executor."""
import json
from .store import stamp


def import_tasks(web):
    with web.db() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='tasks'").fetchone():
            return
        for task in c.execute('SELECT * FROM tasks').fetchall():
            identifier = 'historical-'+task['id']
            if c.execute('SELECT 1 FROM v2_jobs WHERE id=?', (identifier,)).fetchone():
                continue
            c.execute('INSERT INTO v2_jobs VALUES(?,?,?,?,?,?,?)', (identifier, identifier, identifier, 'archived',
                      'historical', task['created_at'], task['updated_at']))
            for item in c.execute('SELECT * FROM task_items WHERE task_id=?', (task['id'],)).fetchall():
                target = {'profile': task['profile'], 'region': task['region'], 'service': 'lightsail', 'instance': item['resource_name']}
                payload = web.vault().encrypt(json.dumps({'historical': True}).encode())
                result = json.dumps({'legacy_status': item['status'], 'verification': item['verification'] if 'verification' in item.keys() else 'unchecked',
                                     'note': 'Historical request state; not proof of resource or bootstrap success'})
                c.execute('INSERT INTO v2_items(id,job_id,kind,target,payload,status,result,error,updated_at) VALUES(?,?,?,?,?,?,?,?,?)',
                          (identifier+'-'+str(item['id']), identifier, 'archived_create', json.dumps(target), payload, 'archived', result, item['error'], stamp()))


def import_monitors(web):
    from .models import Rule
    from .resources import identity, observe
    with web.db() as c:
        old = c.execute('SELECT * FROM monitors').fetchall()
    for row in old:
        marker = 'imported-monitor-'+str(row['id'])
        with web.db() as c:
            if c.execute('SELECT 1 FROM app_settings WHERE name=?', (marker,)).fetchone():
                continue
        try:
            target = dict(profile=row['profile'], region=row['region'], service='lightsail', instance=row['instance'])
            target['account_id'], target['partition'] = identity(web, target['profile'])
            observed = observe(web, target)
            target['generation'] = observed['generation']
            target['created_at'] = observed.get('created_at', '')
            rule = Rule(targets=[target], threshold=row['threshold_gib'], enabled=bool(row['enabled']), consecutive=1).model_dump()
            with web.db() as c:
                c.execute('INSERT OR IGNORE INTO v2_documents VALUES(?,?,?,?,?,?,?)',
                          (marker, 'rule', 'Imported '+row['instance'], 1, web.vault().encrypt(json.dumps(rule).encode()), stamp(), stamp()))
                c.execute('INSERT OR REPLACE INTO app_settings VALUES(?,?)', (marker, 'true'))
                c.execute('INSERT OR REPLACE INTO v2_rule_flags VALUES(?,?)', (marker, int(rule['enabled'])))
                c.execute('UPDATE monitors SET enabled=0 WHERE id=?', (row['id'],))
        except Exception as exc:
            # Unreachable/deleted instances still need a manageable entry in the only UI.
            target = dict(profile=row['profile'],region=row['region'],service='lightsail',instance=row['instance'])
            rule = Rule(targets=[target],threshold=row['threshold_gib'],enabled=False,consecutive=1).model_dump()
            with web.db() as c:
                c.execute('INSERT OR IGNORE INTO v2_documents VALUES(?,?,?,?,?,?,?)',
                          (marker,'rule','Imported '+row['instance']+' — review: '+type(exc).__name__,1,
                           web.vault().encrypt(json.dumps(rule).encode()),stamp(),stamp()))
                c.execute('INSERT OR REPLACE INTO app_settings VALUES(?,?)',(marker,'true'))
                c.execute('INSERT OR REPLACE INTO v2_rule_flags VALUES(?,0)',(marker,))
                c.execute('UPDATE monitors SET enabled=0 WHERE id=?',(row['id'],))
            web.logger.warning('Imported monitor disabled id=%s type=%s', row['id'], type(exc).__name__)
