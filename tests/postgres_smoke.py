"""Manual PostgreSQL integration smoke. Requires Docker and psycopg."""

import os
import json
import secrets
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from http.server import ThreadingHTTPServer
from urllib.request import Request, urlopen
from zipfile import ZipFile

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manage
from offline_oj import db as store
from offline_oj import server
from offline_oj.judge import claim_next, judge_submission


name = 'offline-pg-smoke-' + secrets.token_hex(4)
password = secrets.token_hex(16)
subprocess.run(['docker', 'run', '-d', '--rm', '--name', name,
                '-e', f'POSTGRES_PASSWORD={password}', '-e', 'POSTGRES_USER=offline',
                '-e', 'POSTGRES_DB=offline', '-p', '127.0.0.1::5432',
                'postgres:15-alpine'], check=True, stdout=subprocess.DEVNULL)
try:
    for _ in range(30):
        ready = subprocess.run(['docker', 'exec', name, 'pg_isready', '-U', 'offline'],
                               capture_output=True, check=False)
        if ready.returncode == 0:
            break
        time.sleep(1)
    else:
        raise RuntimeError('PostgreSQL did not start')
    port_text = subprocess.check_output(['docker', 'port', name, '5432/tcp'], text=True).strip()
    port = port_text.rsplit(':', 1)[1]
    store.DATABASE_URL = f'postgresql://offline:{password}@127.0.0.1:{port}/offline'
    for attempt in range(30):
        try:
            with store.transaction() as db:
                db.execute('SELECT 1')
            break
        except Exception:
            if attempt == 29:
                raise
            time.sleep(1)
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        store.PROBLEMS_DIR = root / 'problems'
        shutil.copytree(store.ROOT / 'examples' / 'SUM', store.PROBLEMS_DIR / 'SUM')
        store.import_contest(store.ROOT / 'config' / 'contest.example.json')
        manage.add_user('smoke', 'password123')
        manage.add_user('operator', 'adminpass123', 'admin')
        manage.enroll('smoke', 'DEMO2026')
        with store.transaction() as db:
            user_id = db.execute("SELECT id FROM users WHERE username='smoke'").fetchone()['id']
            cursor = db.execute('''INSERT INTO submissions(user_id,contest_key,problem_code,language,source,created_at)
                VALUES(?,?,?,?,?,?)''', (user_id, 'DEMO2026', 'SUM', 'CPP17',
                '#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}\n',
                store.utcnow().isoformat()))
            submission_id = cursor.lastrowid
        with store.transaction() as db:
            claimed = claim_next(db)
        assert claimed['id'] == submission_id
        result, points, feedback = judge_submission(claimed)
        assert (result, points) == ('AC', 100.0), (result, points, feedback)
        print('PostgreSQL + Judge:', result, points, feedback)
        with store.transaction() as db:
            for _ in range(12):
                db.execute('''INSERT INTO submissions(user_id,contest_key,problem_code,language,source,created_at)
                    VALUES(?,?,?,?,?,?)''', (user_id, 'DEMO2026', 'SUM', 'PY3',
                    'a,b=map(int,input().split());print(a+b)', store.utcnow().isoformat()))

        def claim_id(_):
            with store.transaction() as connection:
                row = claim_next(connection)
                return row['id'] if row else None

        with ThreadPoolExecutor(max_workers=6) as pool:
            claimed_ids = list(pool.map(claim_id, range(12)))
        assert len(set(claimed_ids)) == 12 and None not in claimed_ids, claimed_ids
        print('Parallel workers claimed 12 distinct submissions')
        with store.transaction() as db:
            token = store.create_session(db, user_id)
        server.CONTEST_KEY = 'DEMO2026'
        http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        thread = threading.Thread(target=http.serve_forever, daemon=True)
        thread.start()
        try:
            def submit(_):
                body = b'{"problem":"SUM","language":"PY3","source":"print(1)"}'
                request = Request(f'http://127.0.0.1:{http.server_port}/api/submit',
                                  data=body, method='POST',
                                  headers={'Authorization': f'Bearer {token}',
                                           'Content-Type': 'application/json'})
                with urlopen(request, timeout=20) as response:
                    assert response.status == 201
                    return response.read()

            with ThreadPoolExecutor(max_workers=50) as pool:
                responses = list(pool.map(submit, range(100)))
            assert len(responses) == 100
            print('API accepted 100 concurrent LAN-style submissions')
            admin_login = Request(f'http://127.0.0.1:{http.server_port}/api/admin/login',
                                  data=json.dumps({'username': 'operator', 'password': 'adminpass123'}).encode(),
                                  method='POST', headers={'Content-Type': 'application/json'})
            with urlopen(admin_login, timeout=20) as response:
                admin_token = json.load(response)['token']
            dashboard = Request(f'http://127.0.0.1:{http.server_port}/api/admin/dashboard',
                                headers={'Authorization': f'Bearer {admin_token}'})
            with urlopen(dashboard, timeout=20) as response:
                assert json.load(response)['counts']['QUEUED'] == 100
            print('Admin dashboard reads PostgreSQL queue status')
        finally:
            http.shutdown()
            http.server_close()
        backup_dir = root / 'backups'
        backup_dir.mkdir()
        subprocess.run(['docker', 'run', '--rm', '--network', f'container:{name}',
                        '-e', f'OFFLINE_DATABASE_URL=postgresql://offline:{password}@127.0.0.1:5432/offline',
                        '-v', f'{store.PROBLEMS_DIR}:/app/problems:ro',
                        '-v', f'{backup_dir}:/app/backups',
                        'codepro-offline-web:test', 'python', 'manage.py', 'backup', '/app/backups/pg.zip'],
                       check=True)
        with ZipFile(backup_dir / 'pg.zip') as archive:
            assert 'data/database.sql' in archive.namelist()
            assert 'problems/SUM/1.out' in archive.namelist()
        print('PostgreSQL backup contains database and private tests')
finally:
    subprocess.run(['docker', 'stop', name], capture_output=True, check=False)
