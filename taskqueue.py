"""Durable task worker; all Web and CLI tasks share the same executor."""
import os
import time
import app as web


def run_once():
    return web.control_jobs.run_once()


def loop():
    while True:
        try:
            active = run_once()
        except Exception:
            web.logger.exception('Task worker failure')
            active = False
        if not active:
            time.sleep(max(1, int(os.getenv('TASK_POLL_SECONDS', '3'))))


if __name__ == '__main__':
    from concurrent.futures import ThreadPoolExecutor
    size = max(1, min(5, int(os.getenv('TASK_CONCURRENCY', '5'))))
    with ThreadPoolExecutor(max_workers=size) as pool:
        list(pool.map(lambda _: loop(), range(size)))
