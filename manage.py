import argparse
import csv
import getpass
import os
import sqlite3
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlsplit

from offline_oj.config import load_env

load_env()

from offline_oj import db as store


def add_user(username, password, role='contestant'):
    if not username or len(username) > 64 or any(char.isspace() for char in username):
        raise ValueError('Username must be 1–64 characters without spaces')
    if len(password) < 8:
        raise ValueError('Password must be at least 8 characters')
    if role not in {'contestant', 'admin'}:
        raise ValueError('Invalid role')
    store.init_schema()
    with store.transaction() as db:
        db.execute('INSERT INTO users(username,password_hash,role) VALUES(?,?,?)',
                   (username, store.password_hash(password), role))
        store.audit(db, 'operator', 'user.create', {'username': username, 'role': role})


def enroll(username, contest_key):
    with store.transaction() as db:
        user = db.execute('SELECT id FROM users WHERE username=?', (username,)).fetchone()
        contest = db.execute('SELECT key FROM contests WHERE key=?', (contest_key,)).fetchone()
        if not user or not contest:
            raise ValueError('User or contest does not exist')
        db.execute('INSERT OR IGNORE INTO participations(user_id,contest_key) VALUES(?,?)',
                   (user['id'], contest_key))
        store.audit(db, 'operator', 'contest.enroll', {'username': username, 'contest': contest_key})


def import_users(file_path, contest_key):
    with open(file_path, newline='', encoding='utf-8-sig') as file:
        rows = list(csv.DictReader(file))
    if not rows or any(not row.get('username') or not row.get('password') for row in rows):
        raise ValueError('CSV needs username,password columns and at least one user')
    with store.transaction() as db:
        if not db.execute('SELECT 1 FROM contests WHERE key=?', (contest_key,)).fetchone():
            raise ValueError('Contest does not exist')
        for row in rows:
            username, password = row['username'].strip(), row['password']
            if not username or len(username) > 64 or any(char.isspace() for char in username) or len(password) < 8:
                raise ValueError(f'Invalid username or password for {username}')
            cursor = db.execute('INSERT INTO users(username,password_hash) VALUES(?,?)',
                                (username, store.password_hash(password)))
            db.execute('INSERT INTO participations(user_id,contest_key) VALUES(?,?)',
                       (cursor.lastrowid, contest_key))
            store.audit(db, 'operator', 'user.import', {'username': username, 'contest': contest_key})
    return len(rows)


def status(contest_key):
    with store.transaction() as db:
        contest = db.execute('SELECT * FROM contests WHERE key=?', (contest_key,)).fetchone()
        if not contest:
            raise ValueError('Contest does not exist')
        participants = db.execute('SELECT COUNT(*) FROM participations WHERE contest_key=?',
                                  (contest_key,)).fetchone()[0]
        counts = db.execute('''SELECT status,COUNT(*) AS n FROM submissions WHERE contest_key=?
            GROUP BY status''', (contest_key,)).fetchall()
    return {'contest': contest_key, 'participants': participants,
            'submissions': {row['status']: row['n'] for row in counts}}


def requeue(submission_id):
    with store.transaction() as db:
        row = db.execute('SELECT result,status FROM submissions WHERE id=?', (submission_id,)).fetchone()
        if not row or row['status'] != 'DONE' or row['result'] != 'IE':
            raise ValueError('Only completed IE submissions can be requeued')
        db.execute("UPDATE submissions SET status='QUEUED',result=NULL,points=0,feedback='',judged_at=NULL WHERE id=?",
                   (submission_id,))
        store.audit(db, 'operator', 'submission.requeue', {'id': submission_id})


def export_results(contest_key, output):
    with store.transaction() as db:
        rows = db.execute('''SELECT u.username,s.problem_code,s.id,s.points,s.result,s.created_at
            FROM submissions s JOIN users u ON u.id=s.user_id
            WHERE s.contest_key=? AND s.status='DONE'
            ORDER BY u.username,s.problem_code,s.points DESC,s.id DESC''',
            (contest_key,)).fetchall()
    best = {}
    for row in rows:
        best.setdefault((row['username'], row['problem_code']), row)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open('w', newline='', encoding='utf-8-sig') as file:
        writer = csv.writer(file)
        writer.writerow(['username', 'problem', 'best_submission_id', 'points', 'result', 'submitted_at'])
        for row in best.values():
            writer.writerow([row['username'], row['problem_code'], row['id'],
                             row['points'], row['result'], row['created_at']])
    return len(best)


def backup(output):
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    store.DATA_DIR.mkdir(parents=True, exist_ok=True)
    fd, snapshot_name = tempfile.mkstemp(prefix='offline-backup-', dir=store.DATA_DIR)
    os.close(fd)
    snapshot = Path(snapshot_name)
    try:
        if store.is_postgres():
            parsed = urlsplit(store.DATABASE_URL)
            environment = os.environ.copy()
            environment['PGPASSWORD'] = unquote(parsed.password or '')
            with snapshot.open('wb') as file:
                subprocess.run(['pg_dump', '--no-owner', '--no-privileges',
                                '--host', parsed.hostname or 'localhost',
                                '--port', str(parsed.port or 5432),
                                '--username', unquote(parsed.username or ''),
                                '--dbname', parsed.path.lstrip('/')],
                               env=environment, stdout=file, check=True)
            database_name = 'data/database.sql'
        else:
            with store.transaction() as source:
                target = sqlite3.connect(snapshot)
                try:
                    source.backup(target)
                finally:
                    target.close()
            database_name = 'data/oj.sqlite3'
        with zipfile.ZipFile(output, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
            archive.write(snapshot, database_name)
            for file in store.PROBLEMS_DIR.rglob('*'):
                if file.is_file():
                    archive.write(file, str(file.relative_to(store.ROOT)))
    finally:
        snapshot.unlink(missing_ok=True)


def preflight(contest_key):
    with store.transaction() as db:
        contest = db.execute('SELECT * FROM contests WHERE key=?', (contest_key,)).fetchone()
        if not contest:
            raise ValueError('Contest missing')
        participants = db.execute('SELECT COUNT(*) FROM participations WHERE contest_key=?',
                                  (contest_key,)).fetchone()[0]
        problems = db.execute('SELECT problem_code FROM contest_problems WHERE contest_key=?',
                              (contest_key,)).fetchall()
    if not participants or not problems or not contest['visible']:
        raise ValueError('Contest needs visible=1, registered users and problems')
    for row in problems:
        directory = store.PROBLEMS_DIR / row['problem_code']
        if not (directory / 'tests.json').is_file():
            raise ValueError(f'Missing tests for {row["problem_code"]}')
    image = os.environ.get('OFFLINE_SANDBOX_IMAGE', 'codepro-offline-sandbox:1')
    check = subprocess.run(['docker', 'image', 'inspect', image], capture_output=True, check=False)
    if check.returncode:
        raise ValueError(f'Docker sandbox image not found: {image}')
    return participants, len(problems)


def main():
    parser = argparse.ArgumentParser(description='Standalone offline OJ operations')
    commands = parser.add_subparsers(dest='command', required=True)
    imported = commands.add_parser('import-contest')
    imported.add_argument('config')
    user = commands.add_parser('add-user')
    user.add_argument('username')
    user.add_argument('--password-stdin', action='store_true')
    admin = commands.add_parser('add-admin')
    admin.add_argument('username')
    admin.add_argument('--password-stdin', action='store_true')
    enrolled = commands.add_parser('enroll')
    enrolled.add_argument('username')
    enrolled.add_argument('contest_key')
    imported_users = commands.add_parser('import-users')
    imported_users.add_argument('csv_file')
    imported_users.add_argument('contest_key')
    current = commands.add_parser('status')
    current.add_argument('contest_key')
    retry = commands.add_parser('requeue')
    retry.add_argument('submission_id', type=int)
    exported = commands.add_parser('export-results')
    exported.add_argument('contest_key')
    exported.add_argument('output')
    backed = commands.add_parser('backup')
    backed.add_argument('output')
    checked = commands.add_parser('preflight')
    checked.add_argument('contest_key')
    args = parser.parse_args()
    store.init_schema()
    try:
        if args.command == 'import-contest':
            print('Imported', store.import_contest(args.config))
        elif args.command in {'add-user', 'add-admin'}:
            password = sys.stdin.readline().rstrip('\n') if args.password_stdin else getpass.getpass('Password: ')
            add_user(args.username, password, 'admin' if args.command == 'add-admin' else 'contestant')
            print('Created', args.username)
        elif args.command == 'enroll':
            enroll(args.username, args.contest_key)
            print('Enrolled', args.username)
        elif args.command == 'import-users':
            print('Imported', import_users(args.csv_file, args.contest_key), 'users')
        elif args.command == 'status':
            print(status(args.contest_key))
        elif args.command == 'requeue':
            requeue(args.submission_id)
            print('Requeued', args.submission_id)
        elif args.command == 'export-results':
            print('Exported', export_results(args.contest_key, args.output), 'rows')
        elif args.command == 'backup':
            backup(args.output)
            print('Backup saved:', args.output)
        elif args.command == 'preflight':
            participants, problems = preflight(args.contest_key)
            print(f'Ready: {participants} participants, {problems} problems')
    except (ValueError, sqlite3.Error, OSError, subprocess.CalledProcessError) as error:
        parser.exit(1, f'Error: {error}\n')


if __name__ == '__main__':
    main()
