# Single-instance periodic Lightsail metric collector. Run only one worker.
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from botocore.exceptions import BotoCoreError, ClientError
from app import client, db, logger

INTERVAL = max(300, int(os.getenv('MONITOR_INTERVAL_SECONDS', '900')))
COOLDOWN = max(900, int(os.getenv('ALERT_COOLDOWN_SECONDS', '3600')))
TELEGRAM_TOKEN = os.getenv('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT = os.getenv('TELEGRAM_CHAT_ID', '')


def telegram(message):
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT:
        return False
    # Secrets are never logged. Telegram request has bounded timeout.
    req = Request('https://api.telegram.org/bot'+TELEGRAM_TOKEN+'/sendMessage',
                  data=urlencode({'chat_id': TELEGRAM_CHAT, 'text': message[:3000]}).encode(),
                  method='POST')
    try:
        with urlopen(req, timeout=8) as response:
            return response.status == 200
    except Exception:
        logger.warning('Telegram delivery failed', exc_info=False)
        return False


def collect(monitor, *, now=None):
    now = now or datetime.now(timezone.utc)
    # AWS can report delayed periods. Data are sums of samples in a sliding 1-hour window.
    end = now - timedelta(minutes=5)
    start = end - timedelta(hours=1)
    inbound, outbound = 0, 0
    try:
        api = client(monitor['profile'], 'lightsail', monitor['region'])
        for metric in ('NetworkIn', 'NetworkOut'):
            result = api.get_instance_metric_data(instanceName=monitor['instance'], metricName=metric,
                         period=300, startTime=start, endTime=end, unit='Bytes', statistics=['Sum'])
            value = round(sum(float(p.get('sum') or 0) for p in result.get('metricData', [])))
            if metric == 'NetworkIn':
                inbound = value
            else:
                outbound = value
        state, message = 'ok', ''
    except (ClientError, BotoCoreError, ValueError, TypeError) as exc:
        state, message = 'error', type(exc).__name__
    # Datestamp is rounded to 15 minute bucket, making repeats idempotent.
    bucket = now.replace(minute=now.minute // 15 * 15, second=0, microsecond=0).isoformat()
    with db() as conn:
        conn.execute('''INSERT INTO monitor_samples(monitor_id,measured_at,inbound,outbound,state,message)
            VALUES(?,?,?,?,?,?) ON CONFLICT(monitor_id,measured_at) DO UPDATE SET
            inbound=excluded.inbound,outbound=excluded.outbound,state=excluded.state,message=excluded.message''',
            (monitor['id'], bucket, inbound, outbound, state, message))
        # Keep samples for 35 days; short-lived DB keeps deployment simple.
        conn.execute('DELETE FROM monitor_samples WHERE measured_at<?', ((now-timedelta(days=35)).isoformat(),))
        if state == 'error':
            return {'state': state, 'message': message}
        total = (inbound + outbound) / 1024**3
        last = monitor['last_alert_at']
        elapsed = (now - datetime.fromisoformat(last)).total_seconds() if last else COOLDOWN + 1
        if total >= monitor['threshold_gib'] and elapsed >= COOLDOWN:
            detail = (f"{monitor['profile']}/{monitor['region']}/{monitor['instance']}: "
                      f"1h metrics {total:.3f} GiB >= {monitor['threshold_gib']:.3f} GiB")
            cursor = conn.execute('INSERT INTO alerts(monitor_id,created_at,level,detail) VALUES(?,?,?,?)',
                                  (monitor['id'], now.isoformat(), 'warning', detail))
            conn.execute('UPDATE monitors SET last_alert_at=? WHERE id=?', (now.isoformat(), monitor['id']))
            alert_id = cursor.lastrowid
        else:
            alert_id, detail = None, None
    if alert_id is not None:
        delivered = telegram('AWSCC traffic alert: '+detail)
        with db() as conn:
            conn.execute('UPDATE alerts SET delivered=? WHERE id=?', (int(delivered),alert_id))
    return {'state': state, 'inbound': inbound, 'outbound': outbound, 'alert': alert_id}


def run_once():
    with db() as conn:
        active = [dict(row) for row in conn.execute('SELECT * FROM monitors WHERE enabled=1 ORDER BY id LIMIT 500')]
    for monitor in active:
        try:
            collect(monitor)
        except Exception:
            logger.exception('Monitor collection failure id=%s', monitor['id'])
    return len(active)


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    while True:
        started = time.monotonic()
        count = run_once()
        logger.info('Monitoring pass complete: %s monitors', count)
        time.sleep(max(10, INTERVAL-(time.monotonic()-started)))
