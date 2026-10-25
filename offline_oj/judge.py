import json
import os
import subprocess
import tempfile
import time
import uuid
from datetime import timedelta
from pathlib import Path

from . import db as store
from . import queue


IMAGE = os.environ.get('OFFLINE_SANDBOX_IMAGE', 'codepro-offline-sandbox:1')


def docker_run(args, *, input_data=None, timeout=35):
    name = f'offline-judge-{uuid.uuid4().hex[:16]}'
    command = ['docker', 'run', '--rm', '--name', name, '--network', 'none',
               '--read-only', '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges',
               '--pids-limit', '64', '--cpus', '1', '--user', '65534:65534',
               '--tmpfs', '/tmp:rw,nosuid,nodev,size=64m', *args]
    try:
        return subprocess.run(command, input=input_data, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        subprocess.run(['docker', 'kill', name], capture_output=True, timeout=5, check=False)
        raise


def mount(directory, target, readonly=True):
    spec = f'type=bind,source={Path(directory).resolve()},target={target}'
    if readonly:
        spec += ',readonly'
    return ['--mount', spec]


def sandbox_writable(directory):
    """Let the unprivileged container write without opening host permissions."""
    if os.name == 'posix':
        os.chown(directory, 65534, 65534)
        os.chmod(directory, 0o700)


def score_cases(cases, statuses):
    total = earned = 0.0
    groups = {}
    for case, status in zip(cases, statuses):
        if 'group' in case:
            name = case['group']
            points = float(case.get('group_points', 0))
            if name not in groups:
                groups[name] = {'points': points, 'accepted': True}
            elif groups[name]['points'] != points:
                raise ValueError(f'Inconsistent points for subtask {name}')
            groups[name]['accepted'] &= status == 'AC'
        else:
            points = float(case.get('points', 0))
            total += points
            if status == 'AC':
                earned += points
    for group in groups.values():
        total += group['points']
        if group['accepted']:
            earned += group['points']
    return total, earned


def judge_submission(submission):
    problem_dir = store.PROBLEMS_DIR / submission['problem_code']
    cases = json.loads((problem_dir / 'tests.json').read_text(encoding='utf-8'))
    with store.transaction() as db:
        problem = db.execute('SELECT * FROM problems WHERE code=?', (submission['problem_code'],)).fetchone()
    if problem is None:
        return 'IE', 0, 'Không tìm thấy bài thi.'
    time_limit = max(0.1, min(10.0, float(problem['time_limit'])))
    memory_limit = max(64, min(1024, int(problem['memory_limit'])))
    with tempfile.TemporaryDirectory(prefix='offline-judge-') as directory:
        root = Path(directory)
        source_dir, binary_dir = root / 'source', root / 'binary'
        source_dir.mkdir()
        binary_dir.mkdir()
        sandbox_writable(binary_dir)
        language = submission['language']
        filename = 'main.py' if language == 'PY3' else 'main.cpp'
        (source_dir / filename).write_text(submission['source'], encoding='utf-8')
        if language != 'PY3':
            standard = 'c++20' if language == 'CPP20' else 'c++17'
            compiled = docker_run([
                '--memory', '1024m', *mount(source_dir, '/src'),
                *mount(binary_dir, '/out', False), IMAGE,
                'g++', '/src/main.cpp', f'-std={standard}', '-O2', '-pipe', '-o', '/out/program'
            ], timeout=45)
            if compiled.returncode:
                return 'CE', 0, compiled.stderr.decode('utf-8', 'replace')[:4000]
        verdicts = []
        statuses = []
        for index, case in enumerate(cases, 1):
            case_dir = root / f'case-{index}'
            case_dir.mkdir()
            sandbox_writable(case_dir)
            (case_dir / 'input').write_bytes((problem_dir / f'{index}.in').read_bytes())
            command = 'python3 /src/main.py' if language == 'PY3' else '/program/program'
            script = f'ulimit -f 2048; timeout -k 1s {time_limit}s {command} < /case/input > /out/output'
            mounts = [*mount(source_dir if language == 'PY3' else binary_dir,
                             '/src' if language == 'PY3' else '/program'),
                      *mount(case_dir, '/case'), *mount(case_dir, '/out', False)]
            try:
                run = docker_run(['--memory', f'{memory_limit}m', *mounts, IMAGE,
                                  'sh', '-c', script], timeout=time_limit + 12)
            except subprocess.TimeoutExpired:
                verdict = 'TLE'
            else:
                if run.returncode in (124, 137):
                    verdict = 'TLE' if run.returncode == 124 else 'RE'
                elif run.returncode:
                    verdict = 'RE'
                else:
                    output = (case_dir / 'output').read_bytes() if (case_dir / 'output').exists() else b''
                    expected = (problem_dir / f'{index}.out').read_bytes()
                    matched = output == expected if problem['checker'] == 'exact' else output.split() == expected.split()
                    verdict = 'AC' if matched else 'WA'
            verdicts.append(f'{index}:{verdict}')
            statuses.append(verdict)
            with store.transaction() as db:
                db.execute("UPDATE submissions SET judge_started_at=? WHERE id=? AND status='JUDGING'",
                           (store.utcnow().isoformat(), submission['id']))
        total, earned = score_cases(cases, statuses)
        result = 'AC' if all(status == 'AC' for status in statuses) else ('PARTIAL' if earned > 0 else next(
            (part.split(':')[1] for part in verdicts if not part.endswith(':AC')), 'WA'))
        return result, earned, ', '.join(verdicts)


def claim_next(db):
    if store.is_postgres():
        row = db.execute('''SELECT * FROM submissions WHERE status='QUEUED'
            ORDER BY id LIMIT 1 FOR UPDATE SKIP LOCKED''').fetchone()
    else:
        db.execute('BEGIN IMMEDIATE')
        row = db.execute('''SELECT * FROM submissions WHERE status='QUEUED' ORDER BY id LIMIT 1''').fetchone()
    if row:
        db.execute("UPDATE submissions SET status='JUDGING',judge_started_at=? WHERE id=?",
                   (store.utcnow().isoformat(), row['id']))
    db.commit()
    return row


def run_worker(poll_interval=1):
    store.init_schema()
    last_recovery = 0
    while True:
        if time.monotonic() - last_recovery > 60:
            with store.transaction() as db:
                stale = (store.utcnow() - timedelta(minutes=10)).isoformat()
                db.execute("UPDATE submissions SET status='QUEUED',judge_started_at=NULL WHERE status='JUDGING' AND judge_started_at<?",
                           (stale,))
            last_recovery = time.monotonic()
        with store.transaction() as db:
            submission = claim_next(db)
        if submission is None:
            queue.wait(poll_interval)
            continue
        try:
            result, points, feedback = judge_submission(submission)
        except Exception as error:
            result, points, feedback = 'IE', 0, str(error)[:1000]
        with store.transaction() as db:
            db.execute('''UPDATE submissions SET status='DONE',result=?,points=?,feedback=?,judged_at=?,judge_started_at=NULL
                WHERE id=?''', (result, points, feedback, store.utcnow().isoformat(), submission['id']))
            store.audit(db, 'judge', 'submission.judged',
                        {'id': submission['id'], 'result': result, 'points': points})


if __name__ == '__main__':
    run_worker()
