"""Redis is a wakeup channel; the database remains the durable job queue."""

import os
import time


REDIS_URL = os.environ.get('OFFLINE_REDIS_URL', '')
KEY = 'submit-offline:judge-wakeup'


def notify(submission_id):
    if not REDIS_URL:
        return
    try:
        import redis
        redis.Redis.from_url(REDIS_URL, socket_timeout=1).lpush(KEY, submission_id)
    except Exception:
        # The worker also checks the durable database queue periodically.
        pass


def wait(seconds=1):
    if not REDIS_URL:
        time.sleep(seconds)
        return
    try:
        import redis
        redis.Redis.from_url(REDIS_URL, socket_timeout=seconds + 1).brpop(KEY, timeout=seconds)
    except Exception:
        time.sleep(seconds)
