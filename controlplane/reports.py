import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from fastapi import HTTPException
from .resources import identity, enabled_profiles


def costs(web, profile, fetch):
    if profile != 'all' and profile not in enabled_profiles(web):
        raise HTTPException(400, 'Unknown or disabled account')
    cache_key = 'cost/'+profile
    with web.db() as c:
        cached = c.execute('SELECT * FROM v2_reports WHERE name=? AND expires>?', (cache_key, time.time())).fetchone()
    if cached:
        return json.loads(cached['payload'])
    if profile != 'all':
        result = fetch(profile)
    else:
        totals, services, accounts, errors = 0.0, {}, [], []
        def identify(p):
            account, partition = identity(web, p)
            return p, partition+'/'+account
        with ThreadPoolExecutor(max_workers=4) as pool:
            pending = {pool.submit(identify, p): p for p in enabled_profiles(web)}
            unique = {}
            for future in as_completed(pending):
                try:
                    p, account = future.result()
                    unique.setdefault(account,p)
                except Exception as exc:
                    errors.append({'profile': pending[future], 'error': web.aws_error(exc)})
            pending = {pool.submit(fetch,p): (account,p) for account,p in unique.items()}
            for future in as_completed(pending):
                account,p = pending[future]
                try:
                    data = future.result()
                    totals += data['total']
                    accounts.append({'profile': p, 'account': account, 'total': data['total']})
                    for item in data['items']:
                        services[item['name']] = services.get(item['name'], 0)+item['amount']
                except Exception as exc:
                    errors.append({'profile': p, 'error': web.aws_error(exc)})
        result = {'month': 'Current month', 'total': totals, 'items': [{'name': k, 'amount': v} for k, v in services.items()],
                  'accounts': accounts, 'errors': errors}
    result['cached_until'] = time.time()+300
    with web.db() as c:
        c.execute('INSERT OR REPLACE INTO v2_reports VALUES(?,?,?)', (cache_key, json.dumps(result), time.time()+300))
    return result
