import hashlib
import hmac
import json
import math
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = Path(os.environ.get('OFFLINE_DATA_DIR', ROOT / 'data')).resolve()
DB_PATH = DATA_DIR / 'oj.sqlite3'
PROBLEMS_DIR = ROOT / 'problems'
BACKUPS_DIR = ROOT / 'backups' / 'student-folders'
LANGUAGES = {'CPP17', 'CPP20', 'PY3'}
DATABASE_URL = os.environ.get('OFFLINE_DATABASE_URL', '')


def is_postgres():
    return bool(DATABASE_URL)


class PgRow(dict):
    def __getitem__(self, key):
        if isinstance(key, int):
            return list(self.values())[key]
        return super().__getitem__(key)


class PgCursor:
    def __init__(self, cursor, lastrowid=None):
        self.cursor = cursor
        self.lastrowid = lastrowid

    def fetchone(self):
        row = self.cursor.fetchone()
        return PgRow(row) if row is not None else None

    def fetchall(self):
        return [PgRow(row) for row in self.cursor.fetchall()]


class PgConnection:
    def __init__(self):
        import psycopg
        from psycopg.rows import dict_row
        self.raw = psycopg.connect(DATABASE_URL, row_factory=dict_row)

    def execute(self, sql, params=()):
        sql = sql.replace('?', '%s')
        sql = sql.replace('INSERT OR IGNORE INTO', 'INSERT INTO')
        if 'INSERT INTO participations' in sql and 'ON CONFLICT' not in sql:
            sql += ' ON CONFLICT DO NOTHING'
        needs_id = sql.lstrip().startswith(('INSERT INTO users(', 'INSERT INTO submissions('))
        if needs_id and 'RETURNING' not in sql:
            sql += ' RETURNING id'
        cursor = self.raw.execute(sql, params)
        lastrowid = cursor.fetchone()['id'] if needs_id else None
        return PgCursor(cursor, lastrowid)

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        self.raw.close()


def utcnow():
    return datetime.now(timezone.utc)


def connect():
    if is_postgres():
        return PgConnection()
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(DB_PATH, timeout=20)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA foreign_keys=ON')
    db.execute('PRAGMA journal_mode=WAL')
    db.execute('PRAGMA busy_timeout=20000')
    return db


@contextmanager
def transaction():
    db = connect()
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def init_schema():
    if is_postgres():
        with transaction() as db:
            for sql in POSTGRES_SCHEMA.split(';'):
                if sql.strip():
                    db.execute(sql)
        return
    with transaction() as db:
        db.executescript('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'contestant'
            );
            CREATE TABLE IF NOT EXISTS contests (
                key TEXT PRIMARY KEY, name TEXT NOT NULL, start_at TEXT NOT NULL,
                end_at TEXT NOT NULL, visible INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS problems (
                code TEXT PRIMARY KEY, name TEXT NOT NULL, statement TEXT NOT NULL,
                time_limit REAL NOT NULL, memory_limit INTEGER NOT NULL,
                checker TEXT NOT NULL DEFAULT 'token'
            );
            CREATE TABLE IF NOT EXISTS contest_problems (
                contest_key TEXT NOT NULL REFERENCES contests(key),
                problem_code TEXT NOT NULL REFERENCES problems(code),
                position INTEGER NOT NULL,
                PRIMARY KEY (contest_key, problem_code)
            );
            CREATE TABLE IF NOT EXISTS participations (
                user_id INTEGER NOT NULL REFERENCES users(id),
                contest_key TEXT NOT NULL REFERENCES contests(key),
                PRIMARY KEY (user_id, contest_key)
            );
            CREATE TABLE IF NOT EXISTS sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                expires_at TEXT NOT NULL, device_hash TEXT NOT NULL DEFAULT '',
                client_ip TEXT NOT NULL DEFAULT '', user_agent TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL DEFAULT '', last_seen_at TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE IF NOT EXISTS student_backups (
                id TEXT PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                contest_key TEXT NOT NULL REFERENCES contests(key),
                file_count INTEGER NOT NULL, bytes INTEGER NOT NULL,
                sha256 TEXT NOT NULL, created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS submissions (
                id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL REFERENCES users(id),
                contest_key TEXT NOT NULL REFERENCES contests(key),
                problem_code TEXT NOT NULL REFERENCES problems(code),
                language TEXT NOT NULL, source TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'QUEUED', result TEXT,
                points REAL NOT NULL DEFAULT 0, feedback TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL, judged_at TEXT, judge_started_at TEXT
            );
            CREATE TABLE IF NOT EXISTS audit (
                id INTEGER PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
                action TEXT NOT NULL, detail TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS submissions_user_contest ON submissions(user_id, contest_key, id DESC);
            CREATE INDEX IF NOT EXISTS submissions_queue ON submissions(status, id);
            CREATE INDEX IF NOT EXISTS student_backups_user ON student_backups(user_id, contest_key, created_at DESC);
        ''')
        session_columns = {row['name'] for row in db.execute('PRAGMA table_info(sessions)').fetchall()}
        for name in ('device_hash', 'client_ip', 'user_agent', 'created_at', 'last_seen_at'):
            if name not in session_columns:
                db.execute(f"ALTER TABLE sessions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")
        db.execute("DELETE FROM sessions WHERE device_hash='' AND user_id IN (SELECT id FROM users WHERE role='contestant')")
        db.execute('CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_user ON sessions(user_id)')
        columns = {row['name'] for row in db.execute('PRAGMA table_info(submissions)').fetchall()}
        if 'judge_started_at' not in columns:
            db.execute('ALTER TABLE submissions ADD COLUMN judge_started_at TEXT')
        problem_columns = {row['name'] for row in db.execute('PRAGMA table_info(problems)').fetchall()}
        if 'checker' not in problem_columns:
            db.execute("ALTER TABLE problems ADD COLUMN checker TEXT NOT NULL DEFAULT 'token'")


POSTGRES_SCHEMA = '''
CREATE TABLE IF NOT EXISTS users (
    id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'contestant');
CREATE TABLE IF NOT EXISTS contests (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, start_at TEXT NOT NULL,
    end_at TEXT NOT NULL, visible SMALLINT NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS problems (
    code TEXT PRIMARY KEY, name TEXT NOT NULL, statement TEXT NOT NULL,
    time_limit DOUBLE PRECISION NOT NULL, memory_limit INTEGER NOT NULL,
    checker TEXT NOT NULL DEFAULT 'token');
CREATE TABLE IF NOT EXISTS contest_problems (
    contest_key TEXT NOT NULL REFERENCES contests(key),
    problem_code TEXT NOT NULL REFERENCES problems(code),
    position INTEGER NOT NULL, PRIMARY KEY(contest_key,problem_code));
CREATE TABLE IF NOT EXISTS participations (
    user_id BIGINT NOT NULL REFERENCES users(id), contest_key TEXT NOT NULL REFERENCES contests(key),
    PRIMARY KEY(user_id,contest_key));
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id), expires_at TEXT NOT NULL,
    device_hash TEXT NOT NULL DEFAULT '', client_ip TEXT NOT NULL DEFAULT '',
    user_agent TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL DEFAULT '',
    last_seen_at TEXT NOT NULL DEFAULT '');
CREATE TABLE IF NOT EXISTS student_backups (
    id TEXT PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id),
    contest_key TEXT NOT NULL REFERENCES contests(key),
    file_count INTEGER NOT NULL, bytes BIGINT NOT NULL,
    sha256 TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS submissions (
    id BIGSERIAL PRIMARY KEY, user_id BIGINT NOT NULL REFERENCES users(id),
    contest_key TEXT NOT NULL REFERENCES contests(key),
    problem_code TEXT NOT NULL REFERENCES problems(code),
    language TEXT NOT NULL, source TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'QUEUED', result TEXT,
    points DOUBLE PRECISION NOT NULL DEFAULT 0, feedback TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL, judged_at TEXT, judge_started_at TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id BIGSERIAL PRIMARY KEY, at TEXT NOT NULL, actor TEXT NOT NULL,
    action TEXT NOT NULL, detail TEXT NOT NULL);
ALTER TABLE problems ADD COLUMN IF NOT EXISTS checker TEXT NOT NULL DEFAULT 'token';
ALTER TABLE submissions ADD COLUMN IF NOT EXISTS judge_started_at TEXT;
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS device_hash TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS client_ip TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS user_agent TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS created_at TEXT NOT NULL DEFAULT '';
ALTER TABLE sessions ADD COLUMN IF NOT EXISTS last_seen_at TEXT NOT NULL DEFAULT '';
DELETE FROM sessions WHERE device_hash='' AND user_id IN (SELECT id FROM users WHERE role='contestant');
CREATE UNIQUE INDEX IF NOT EXISTS sessions_one_user ON sessions(user_id);
CREATE INDEX IF NOT EXISTS submissions_user_contest ON submissions(user_id,contest_key,id DESC);
CREATE INDEX IF NOT EXISTS submissions_queue ON submissions(status,id);
CREATE INDEX IF NOT EXISTS student_backups_user ON student_backups(user_id,contest_key,created_at DESC);
'''


def password_hash(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 250000)
    return f'pbkdf2_sha256$250000${salt.hex()}${digest.hex()}'


def verify_password(password, encoded):
    try:
        algorithm, rounds, salt, expected = encoded.split('$')
        if algorithm != 'pbkdf2_sha256':
            return False
        digest = hashlib.pbkdf2_hmac('sha256', password.encode(), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(digest, bytes.fromhex(expected))
    except (ValueError, TypeError):
        return False


def audit(db, actor, action, detail):
    db.execute('INSERT INTO audit(at,actor,action,detail) VALUES(?,?,?,?)',
               (utcnow().isoformat(), actor, action, json.dumps(detail, ensure_ascii=False)))


def create_session(db, user_id, device_hash='', client_ip='', user_agent=''):
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    expires = (utcnow() + timedelta(hours=12)).isoformat()
    now = utcnow().isoformat()
    db.execute('DELETE FROM sessions WHERE user_id=?', (user_id,))
    db.execute('''INSERT INTO sessions(token_hash,user_id,expires_at,device_hash,
        client_ip,user_agent,created_at,last_seen_at) VALUES(?,?,?,?,?,?,?,?)''',
        (token_hash, user_id, expires, device_hash, client_ip, user_agent[:256], now, now))
    return token


def session_user(db, token):
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    return db.execute('''SELECT u.id, u.username, u.role,s.device_hash,s.client_ip FROM sessions s
        JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires_at>?''',
        (token_hash, utcnow().isoformat())).fetchone()


def load_contest_package(config_path):
    config_path = Path(config_path).resolve()
    package = json.loads(config_path.read_text(encoding='utf-8'))
    key = package['key']
    if not key.isalnum() or len(key) > 40:
        raise ValueError('Contest key must be alphanumeric, at most 40 characters')
    start = datetime.fromisoformat(package['start_at'].replace('Z', '+00:00'))
    end = datetime.fromisoformat(package['end_at'].replace('Z', '+00:00'))
    if not start.tzinfo or not end.tzinfo or start >= end:
        raise ValueError('Contest timestamps need UTC offsets and end_at after start_at')
    if not package.get('problems'):
        raise ValueError('Contest must contain at least one problem')
    checked = []
    for problem in package['problems']:
        code = problem['code']
        if not code.isalnum() or len(code) > 40:
            raise ValueError(f'Invalid problem code: {code}')
        if problem.get('checker', 'token') not in {'token', 'exact'}:
            raise ValueError(f'Invalid checker for {code}')
        time_limit = float(problem.get('time_limit', 2))
        memory_limit = int(problem.get('memory_limit', 256))
        if not math.isfinite(time_limit) or not 0.1 <= time_limit <= 10:
            raise ValueError(f'Invalid time limit for {code}')
        if not 64 <= memory_limit <= 1024:
            raise ValueError(f'Invalid memory limit for {code}')
        directory = (PROBLEMS_DIR / code).resolve()
        if directory.parent != PROBLEMS_DIR.resolve():
            raise ValueError('Problem path escapes private storage')
        statement = (directory / 'statement.md').read_text(encoding='utf-8')
        cases = json.loads((directory / 'tests.json').read_text(encoding='utf-8'))
        if not cases or not isinstance(cases, list):
            raise ValueError(f'Problem {code} has no tests')
        groups = {}
        for index, case in enumerate(cases, 1):
            for suffix in ('in', 'out'):
                if not (directory / f'{index}.{suffix}').is_file():
                    raise ValueError(f'Missing {code}/{index}.{suffix}')
            points = float(case.get('group_points', 0) if 'group' in case else case.get('points', 0))
            if not math.isfinite(points) or points < 0:
                raise ValueError('Invalid test points')
            if 'group' in case and (not isinstance(case['group'], str) or not case['group']):
                raise ValueError('Invalid subtask group')
            if 'group' in case:
                previous = groups.setdefault(case['group'], points)
                if previous != points:
                    raise ValueError(f'Inconsistent points for subtask {case["group"]}')
        checked.append((problem, statement))
    return package, checked


def import_contest(config_path):
    package, checked = load_contest_package(config_path)
    init_schema()
    with transaction() as db:
        if db.execute('SELECT 1 FROM submissions WHERE contest_key=? LIMIT 1',
                      (package['key'],)).fetchone():
            raise ValueError('Cannot reimport a contest after submissions exist')
        db.execute('''INSERT INTO contests(key,name,start_at,end_at,visible) VALUES(?,?,?,?,?)
            ON CONFLICT(key) DO UPDATE SET name=excluded.name,start_at=excluded.start_at,
            end_at=excluded.end_at,visible=excluded.visible''',
            (package['key'], package['name'], package['start_at'], package['end_at'], int(package.get('visible', False))))
        db.execute('DELETE FROM contest_problems WHERE contest_key=?', (package['key'],))
        for position, (problem, statement) in enumerate(checked, 1):
            db.execute('''INSERT INTO problems(code,name,statement,time_limit,memory_limit,checker) VALUES(?,?,?,?,?,?)
                ON CONFLICT(code) DO UPDATE SET name=excluded.name,statement=excluded.statement,
                time_limit=excluded.time_limit,memory_limit=excluded.memory_limit,checker=excluded.checker''',
                (problem['code'], problem['name'], statement,
                 float(problem.get('time_limit', 2)), int(problem.get('memory_limit', 256)),
                 problem.get('checker', 'token')))
            db.execute('''INSERT INTO contest_problems(contest_key,problem_code,position) VALUES(?,?,?)
                ON CONFLICT(contest_key,problem_code) DO UPDATE SET position=excluded.position''',
                (package['key'], problem['code'], position))
        audit(db, 'system', 'contest.import', {'key': package['key']})
    return package['key']
