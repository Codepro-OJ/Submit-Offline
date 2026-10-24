import os
from pathlib import Path


def load_env():
    file = Path(__file__).resolve().parent.parent / '.env'
    if not file.is_file():
        return
    for line in file.read_text(encoding='utf-8').splitlines():
        if not line or line.lstrip().startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        if key in {'OFFLINE_CONTEST_KEY', 'OFFLINE_HOST', 'OFFLINE_PORT',
                   'OFFLINE_SANDBOX_IMAGE', 'OFFLINE_DATA_DIR',
                   'OFFLINE_DATABASE_URL', 'OFFLINE_REDIS_URL',
                   'OFFLINE_MAX_DB_CONNECTIONS'}:
            os.environ.setdefault(key, value.strip())
