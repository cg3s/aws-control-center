# Periodic inventory, metric and automation worker. Run one process.
import os
import time
from datetime import datetime, timezone
from urllib.parse import urlencode
from urllib.request import Request, urlopen

import app as web
from app import logger
from controlplane import metrics, resources

MONITOR_INTERVAL = max(60, int(os.getenv('MONITOR_INTERVAL_SECONDS', '300')))
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



def run_once():
    from controlplane.history import import_monitors
    import_monitors(web)
    with web.db() as c:
        c.execute("DELETE FROM v2_documents WHERE kind='preview' AND updated_at<?", (datetime.fromtimestamp(time.time()-3600, timezone.utc).isoformat(timespec='seconds'),))
        row = c.execute("SELECT seen_at FROM v2_workers WHERE name='inventory'").fetchone()
    if not row or time.time()-datetime.fromisoformat(row[0]).timestamp() >= 300:
        resources.refresh(web)
    return metrics.run_once(web, web.control_jobs, telegram)


if __name__ == '__main__':
    while True:
        started = time.monotonic()
        try:
            run_once()
        except Exception:
            logger.exception('Monitor pass failed')
        time.sleep(max(10, MONITOR_INTERVAL-(time.monotonic()-started)))
