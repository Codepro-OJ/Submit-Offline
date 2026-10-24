"""Run manually after building the Docker sandbox image."""

import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import manage
from offline_oj import db as store
from offline_oj.judge import judge_submission


with tempfile.TemporaryDirectory() as directory:
    root = Path(directory)
    store.DATA_DIR = root / 'data'
    store.DB_PATH = store.DATA_DIR / 'oj.sqlite3'
    store.PROBLEMS_DIR = root / 'problems'
    shutil.copytree(store.ROOT / 'examples' / 'SUM', store.PROBLEMS_DIR / 'SUM')
    store.import_contest(store.ROOT / 'config' / 'contest.example.json')
    manage.add_user('smoke', 'password123')
    manage.enroll('smoke', 'DEMO2026')
    with store.transaction() as db:
        user_id = db.execute("SELECT id FROM users WHERE username='smoke'").fetchone()['id']
        cursor = db.execute('''INSERT INTO submissions(user_id,contest_key,problem_code,language,source,created_at)
            VALUES(?,?,?,?,?,?)''', (user_id, 'DEMO2026', 'SUM', 'CPP17',
            '#include <iostream>\nint main(){long long a,b;std::cin>>a>>b;std::cout<<a+b;}\n',
            store.utcnow().isoformat()))
        submission_id = cursor.lastrowid
    with store.transaction() as db:
        submission = db.execute('SELECT * FROM submissions WHERE id=?', (submission_id,)).fetchone()
    result, points, feedback = judge_submission(submission)
    print(result, points, feedback)
    if (result, points) != ('AC', 100.0):
        raise SystemExit(1)
