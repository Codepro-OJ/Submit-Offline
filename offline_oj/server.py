import hashlib
import json
import logging
import os
import secrets
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from http.cookies import SimpleCookie, CookieError
from urllib.parse import urlsplit

from . import backup
from . import db as store
from . import queue


PUBLIC = Path(__file__).resolve().parent.parent / 'public'
CONTEST_KEY = os.environ.get('OFFLINE_CONTEST_KEY', '')
LOGIN_ATTEMPTS = {}
LOGIN_LOCK = threading.Lock()
DB_SLOTS = threading.BoundedSemaphore(int(os.environ.get('OFFLINE_MAX_DB_CONNECTIONS', '32')))
UPLOAD_SLOTS = threading.BoundedSemaphore(4)
DEVICE_COOKIE = 'offline_device'


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def require_contest(db, user):
    contest = db.execute('SELECT * FROM contests WHERE key=? AND visible=1', (CONTEST_KEY,)).fetchone()
    if contest is None:
        raise ApiError(503, 'Kỳ thi offline chưa sẵn sàng.')
    member = db.execute('SELECT 1 FROM participations WHERE user_id=? AND contest_key=?',
                        (user['id'], CONTEST_KEY)).fetchone()
    if member is None:
        raise ApiError(403, 'Tài khoản chưa được đăng ký vào kỳ thi.')
    return contest


def require_started(contest):
    start = datetime.fromisoformat(contest['start_at'].replace('Z', '+00:00'))
    if store.utcnow() < start:
        raise ApiError(403, 'Kỳ thi chưa bắt đầu.')


def contest_problem(db, code):
    row = db.execute('''SELECT p.* FROM problems p JOIN contest_problems cp ON cp.problem_code=p.code
        WHERE cp.contest_key=? AND p.code=?''', (CONTEST_KEY, code)).fetchone()
    if row is None:
        raise ApiError(404, 'Bài không thuộc kỳ thi offline.')
    return row


@contextmanager
def request_db():
    if not DB_SLOTS.acquire(timeout=10):
        raise ApiError(503, 'Máy chủ đang bận. Vui lòng thử lại.')
    try:
        with store.transaction() as db:
            yield db
    finally:
        DB_SLOTS.release()


class Handler(BaseHTTPRequestHandler):
    server_version = 'SubmitOffline/1.0'

    def log_message(self, format, *args):
        logging.info('%s %s', self.client_address[0], format % args)

    def send_json(self, status, payload, cookie=None):
        body = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        if cookie is not None:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(body)

    def device_cookie(self):
        try:
            cookie = SimpleCookie()
            cookie.load(self.headers.get('Cookie', ''))
            value = cookie[DEVICE_COOKIE].value if DEVICE_COOKIE in cookie else ''
            return value if len(value) <= 128 else ''
        except CookieError:
            return ''

    def send_zip(self, path, backup_id):
        self.send_response(200)
        self.send_header('Content-Type', 'application/zip')
        self.send_header('Content-Disposition', f'attachment; filename="exam-backup-{backup_id}.zip"')
        self.send_header('Content-Length', str(path.stat().st_size))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        with path.open('rb') as file:
            while chunk := file.read(65536):
                self.wfile.write(chunk)

    def read_json(self):
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ApiError(400, 'Content-Length không hợp lệ.') from None
        if length < 0 or length > 262144:
            raise ApiError(413, 'Mã nguồn quá lớn.')
        try:
            value = json.loads(self.rfile.read(length))
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ApiError(400, 'JSON không hợp lệ.') from None
        if not isinstance(value, dict):
            raise ApiError(400, 'Dữ liệu không hợp lệ.')
        return value

    def auth(self, db):
        header = self.headers.get('Authorization', '')
        if not header.startswith('Bearer ') or len(header) > 128:
            raise ApiError(401, 'Vui lòng đăng nhập.')
        user = store.session_user(db, header[7:])
        if user is None:
            raise ApiError(401, 'Phiên đăng nhập đã hết hạn.')
        if user['role'] == 'contestant':
            device_value = self.device_cookie()
            device_hash = hashlib.sha256(device_value.encode()).hexdigest() if device_value else ''
            if not device_value or device_hash != user['device_hash'] or self.client_address[0] != user['client_ip']:
                raise ApiError(401, 'Phiên đăng nhập không thuộc máy này.')
        db.execute('UPDATE sessions SET last_seen_at=? WHERE token_hash=?',
                   (store.utcnow().isoformat(), hashlib.sha256(header[7:].encode()).hexdigest()))
        return user

    def serve_static(self, path):
        files = {
            '/': ('index.html', 'text/html; charset=utf-8'),
            '/admin': ('admin.html', 'text/html; charset=utf-8'),
            '/app.js': ('app.js', 'text/javascript; charset=utf-8'),
            '/admin.js': ('admin.js', 'text/javascript; charset=utf-8'),
            '/style.css': ('style.css', 'text/css; charset=utf-8'),
        }
        if path not in files:
            raise ApiError(404, 'Không tìm thấy trang.')
        filename, content_type = files[path]
        body = (PUBLIC / filename).read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Content-Security-Policy',
                         "default-src 'self'; script-src 'self'; style-src 'self'; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.dispatch('GET')

    def do_POST(self):
        self.dispatch('POST')

    def dispatch(self, method):
        self.connection.settimeout(15)
        path = urlsplit(self.path).path
        try:
            if not path.startswith('/api/'):
                if method != 'GET':
                    raise ApiError(405, 'Phương thức không được hỗ trợ.')
                return self.serve_static(path)
            if path == '/api/health' and method == 'GET':
                return self.send_json(200, {'status': 'ok'})
            with request_db() as db:
                if path == '/api/login' and method == 'POST':
                    return self.login(db, 'contestant')
                if path == '/api/admin/login' and method == 'POST':
                    return self.login(db, 'admin')
                user = self.auth(db)
                if path == '/api/logout' and method == 'POST':
                    token = self.headers['Authorization'][7:]
                    db.execute('DELETE FROM sessions WHERE token_hash=?',
                               (hashlib.sha256(token.encode()).hexdigest(),))
                    store.audit(db, user['username'], 'auth.logout', {'ip': self.client_address[0]})
                    db.commit()
                    cookie = (f'{DEVICE_COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0'
                              if user['role'] == 'contestant' else None)
                    return self.send_json(200, {'status': 'logged_out'}, cookie)
                if path == '/api/admin/session/release' and method == 'POST':
                    if user['role'] != 'admin':
                        raise ApiError(403, 'Chỉ ban tổ chức được truy cập.')
                    username = self.read_json().get('username')
                    if not isinstance(username, str) or not username:
                        raise ApiError(400, 'Cần chọn tài khoản thí sinh.')
                    target = db.execute('SELECT id FROM users WHERE username=? AND role=?',
                                        (username, 'contestant')).fetchone()
                    if target is None:
                        raise ApiError(404, 'Không tìm thấy thí sinh.')
                    db.execute('DELETE FROM sessions WHERE user_id=?', (target['id'],))
                    store.audit(db, user['username'], 'auth.release', {'username': username})
                    db.commit()
                    return self.send_json(200, {'status': 'released'})
                if path == '/api/admin/dashboard' and method == 'GET':
                    if user['role'] != 'admin':
                        raise ApiError(403, 'Chỉ ban tổ chức được truy cập.')
                    contest = db.execute('SELECT key,name,start_at,end_at FROM contests WHERE key=?',
                                         (CONTEST_KEY,)).fetchone()
                    if contest is None:
                        raise ApiError(503, 'Kỳ thi offline chưa sẵn sàng.')
                    participants = db.execute('SELECT COUNT(*) FROM participations WHERE contest_key=?',
                                              (CONTEST_KEY,)).fetchone()[0]
                    counts = db.execute('''SELECT status,COUNT(*) AS count FROM submissions
                        WHERE contest_key=? GROUP BY status''', (CONTEST_KEY,)).fetchall()
                    recent = db.execute('''SELECT s.id,u.username,s.problem_code,s.language,s.status,
                        s.result,s.points,s.created_at FROM submissions s
                        JOIN users u ON u.id=s.user_id WHERE s.contest_key=?
                        ORDER BY s.id DESC LIMIT 50''', (CONTEST_KEY,)).fetchall()
                    sessions = db.execute('''SELECT u.username,s.client_ip,s.user_agent,
                        substr(s.device_hash,1,10) AS device,s.created_at,s.last_seen_at
                        FROM sessions s JOIN users u ON u.id=s.user_id
                        JOIN participations p ON p.user_id=u.id
                        WHERE p.contest_key=? AND u.role='contestant' AND s.expires_at>?
                        ORDER BY s.last_seen_at DESC''', (CONTEST_KEY, store.utcnow().isoformat())).fetchall()
                    recent_backups = db.execute('''SELECT b.id,u.username,b.file_count,b.bytes,b.created_at
                        FROM student_backups b JOIN users u ON u.id=b.user_id
                        WHERE b.contest_key=? ORDER BY b.created_at DESC LIMIT 50''',
                        (CONTEST_KEY,)).fetchall()
                    backup_count = db.execute('SELECT COUNT(*) FROM student_backups WHERE contest_key=?',
                                              (CONTEST_KEY,)).fetchone()[0]
                    audit = db.execute('SELECT at,actor,action,detail FROM audit ORDER BY id DESC LIMIT 50').fetchall()
                    return self.send_json(200, {'contest': dict(contest), 'participants': participants,
                        'counts': {row['status']: row['count'] for row in counts},
                        'recent': [dict(row) for row in recent], 'audit': [dict(row) for row in audit],
                        'sessions': [dict(row) for row in sessions],
                        'recent_backups': [dict(row) for row in recent_backups],
                        'backup_count': backup_count})
                if path.startswith('/api/admin/backup/') and method == 'GET':
                    if user['role'] != 'admin':
                        raise ApiError(403, 'Chỉ ban tổ chức được truy cập.')
                    backup_id = path.removeprefix('/api/admin/backup/')
                    row = db.execute('SELECT user_id FROM student_backups WHERE id=? AND contest_key=?',
                                     (backup_id, CONTEST_KEY)).fetchone()
                    if row is None:
                        raise ApiError(404, 'Không tìm thấy bản sao lưu.')
                    archive = backup.path_for(CONTEST_KEY, row['user_id'], backup_id)
                    if not archive.is_file():
                        raise ApiError(404, 'File sao lưu không còn trên máy chủ.')
                    return self.send_zip(archive, backup_id)
                if user['role'] != 'contestant':
                    raise ApiError(403, 'Trang này chỉ dành cho thí sinh.')
                contest = require_contest(db, user)
                if method == 'GET' and path == '/api/contest':
                    problems = db.execute('''SELECT p.code,p.name,cp.position FROM contest_problems cp
                        JOIN problems p ON p.code=cp.problem_code WHERE cp.contest_key=? ORDER BY cp.position''',
                        (CONTEST_KEY,)).fetchall()
                    return self.send_json(200, {'username': user['username'], 'contest': {
                        'key': contest['key'], 'name': contest['name'],
                        'start_time': contest['start_at'], 'end_time': contest['end_at'],
                        'problems': [{'code': p['code'], 'name': p['name'],
                                      'order': p['position'], 'output_prefix': str(p['position'])}
                                     for p in problems]}})
                if method == 'GET' and path == '/api/languages':
                    return self.send_json(200, {'languages': [
                        {'key': 'CPP17', 'name': 'C++17'}, {'key': 'CPP20', 'name': 'C++20'},
                        {'key': 'PY3', 'name': 'Python 3'}]})
                if method == 'GET' and path.startswith('/api/problem/'):
                    require_started(contest)
                    code = path.removeprefix('/api/problem/')
                    problem = contest_problem(db, code)
                    return self.send_json(200, {'problem': {
                        'code': problem['code'], 'name': problem['name'],
                        'description': problem['statement'],
                        'time_limit': problem['time_limit'], 'memory_limit': problem['memory_limit']}})
                if method == 'POST' and path == '/api/submit':
                    return self.submit(db, user, contest)
                if method == 'POST' and path == '/api/backup':
                    return self.upload_backup(db, user, contest)
                if method == 'GET' and path == '/api/backups':
                    rows = db.execute('''SELECT id,file_count,bytes,created_at FROM student_backups
                        WHERE user_id=? AND contest_key=? ORDER BY created_at DESC LIMIT 20''',
                        (user['id'], CONTEST_KEY)).fetchall()
                    return self.send_json(200, {'backups': [dict(row) for row in rows]})
                if method == 'GET' and path == '/api/submissions':
                    rows = db.execute('''SELECT id,problem_code,language,status,result,points,created_at
                        FROM submissions WHERE user_id=? AND contest_key=? ORDER BY id DESC LIMIT 100''',
                        (user['id'], CONTEST_KEY)).fetchall()
                    return self.send_json(200, {'submissions': [dict(row) | {
                        'problem': row['problem_code'], 'date': row['created_at']} for row in rows]})
                if method == 'GET' and path.startswith('/api/submission/'):
                    raw_id = path.removeprefix('/api/submission/')
                    if not raw_id.isdecimal():
                        raise ApiError(404, 'Không tìm thấy bài nộp.')
                    row = db.execute('''SELECT id,problem_code,language,source,status,result,points,
                        feedback,created_at,judged_at FROM submissions
                        WHERE id=? AND user_id=? AND contest_key=?''',
                        (int(raw_id), user['id'], CONTEST_KEY)).fetchone()
                    if row is None:
                        raise ApiError(404, 'Không tìm thấy bài nộp.')
                    return self.send_json(200, {'submission': dict(row)})
            raise ApiError(404, 'Đường dẫn không tồn tại.')
        except ApiError as error:
            self.send_json(error.status, {'error': str(error)})
        except Exception:
            logging.exception('Unhandled request error')
            self.send_json(500, {'error': 'Lỗi máy chủ kỳ thi.'})

    def login(self, db, role):
        ip = self.client_address[0]
        now = time.monotonic()
        with LOGIN_LOCK:
            attempts = [at for at in LOGIN_ATTEMPTS.get(ip, []) if now - at < 60]
            LOGIN_ATTEMPTS[ip] = attempts
            if len(attempts) >= 8:
                raise ApiError(429, 'Thử đăng nhập quá nhiều. Vui lòng đợi một phút.')
        body = self.read_json()
        username, password = body.get('username'), body.get('password')
        if not isinstance(username, str) or not isinstance(password, str):
            raise ApiError(400, 'Thiếu tên đăng nhập hoặc mật khẩu.')
        if role == 'contestant' and not store.is_postgres():
            db.execute('BEGIN IMMEDIATE')
        query = 'SELECT * FROM users WHERE username=?'
        if role == 'contestant' and store.is_postgres():
            query += ' FOR UPDATE'
        user = db.execute(query, (username,)).fetchone()
        if user is None or not store.verify_password(password, user['password_hash']):
            with LOGIN_LOCK:
                LOGIN_ATTEMPTS.setdefault(ip, []).append(now)
            store.audit(db, username[:80], 'auth.failed', {'ip': ip})
            db.commit()
            raise ApiError(401, 'Tên đăng nhập hoặc mật khẩu không đúng.')
        if user['role'] != role:
            raise ApiError(403, 'Tài khoản không được đăng nhập tại trang này.')
        device_value = self.device_cookie()
        if role == 'contestant':
            active = db.execute('SELECT device_hash,client_ip FROM sessions WHERE user_id=? AND expires_at>?',
                                (user['id'], store.utcnow().isoformat())).fetchone()
            device_hash = hashlib.sha256(device_value.encode()).hexdigest() if device_value else ''
            if active and (not device_value or active['device_hash'] != device_hash or active['client_ip'] != ip):
                store.audit(db, username, 'auth.device_denied', {'ip': ip})
                db.commit()
                raise ApiError(409, 'Tài khoản đang đăng nhập trên máy khác. Liên hệ giám thị để chuyển máy.')
            if not device_value:
                device_value = secrets.token_urlsafe(32)
                device_hash = hashlib.sha256(device_value.encode()).hexdigest()
        else:
            device_hash = ''
        token = store.create_session(db, user['id'], device_hash, ip, self.headers.get('User-Agent', ''))
        store.audit(db, username, 'auth.login', {'ip': ip, 'device': device_hash[:10]})
        db.commit()
        cookie = f'{DEVICE_COOKIE}={device_value}; HttpOnly; SameSite=Strict; Path=/; Max-Age=43200' if role == 'contestant' else None
        return self.send_json(200, {'token': token, 'user': {'username': username}}, cookie)

    def upload_backup(self, db, user, contest):
        require_started(contest)
        end = datetime.fromisoformat(contest['end_at'].replace('Z', '+00:00'))
        if store.utcnow() > end:
            raise ApiError(403, 'Kỳ thi đã kết thúc.')
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            raise ApiError(400, 'Content-Length không hợp lệ.') from None
        if length < 1 or length > backup.MAX_REQUEST_BYTES:
            raise ApiError(413, 'Thư mục vượt giới hạn dung lượng.')
        if not UPLOAD_SLOTS.acquire(timeout=10):
            raise ApiError(503, 'Máy chủ đang bận sao lưu. Vui lòng thử lại.')
        try:
            self.connection.settimeout(90)
            body = self.rfile.read(length)
            if len(body) != length:
                raise ApiError(400, 'Tải thư mục lên chưa hoàn tất.')
            try:
                files, total = backup.parse_folder(body, self.headers.get('Content-Type', ''))
            except (backup.BackupError, UnicodeError) as error:
                raise ApiError(400, str(error)) from None
            saved = backup.save_folder(CONTEST_KEY, user['id'], files, total)
            db.execute('''INSERT INTO student_backups(id,user_id,contest_key,file_count,bytes,sha256,created_at)
                VALUES(?,?,?,?,?,?,?)''', (saved['id'], user['id'], CONTEST_KEY, saved['file_count'],
                saved['bytes'], saved['sha256'], store.utcnow().isoformat()))
            store.audit(db, user['username'], 'backup.create', {'id': saved['id'],
                'files': saved['file_count'], 'bytes': saved['bytes']})
            db.commit()
            return self.send_json(201, saved)
        finally:
            UPLOAD_SLOTS.release()

    def submit(self, db, user, contest):
        require_started(contest)
        end = datetime.fromisoformat(contest['end_at'].replace('Z', '+00:00'))
        if store.utcnow() > end:
            raise ApiError(403, 'Kỳ thi đã kết thúc.')
        body = self.read_json()
        code, language, source = body.get('problem'), body.get('language'), body.get('source')
        if not isinstance(code, str) or not isinstance(language, str) or not isinstance(source, str):
            raise ApiError(400, 'Cần chọn bài, ngôn ngữ và nhập mã nguồn.')
        contest_problem(db, code)
        if language not in store.LANGUAGES or not source.strip() or len(source.encode('utf-8')) > 131072:
            raise ApiError(400, 'Ngôn ngữ hoặc mã nguồn không hợp lệ.')
        cursor = db.execute('''INSERT INTO submissions(user_id,contest_key,problem_code,language,source,created_at)
            VALUES(?,?,?,?,?,?)''', (user['id'], CONTEST_KEY, code, language, source, store.utcnow().isoformat()))
        store.audit(db, user['username'], 'submission.create', {'id': cursor.lastrowid, 'problem': code})
        db.commit()
        queue.notify(cursor.lastrowid)
        return self.send_json(201, {'id': cursor.lastrowid, 'status': 'QUEUED'})


def run(host=None, port=None):
    if not CONTEST_KEY:
        raise RuntimeError('OFFLINE_CONTEST_KEY is required')
    store.init_schema()
    address = (host or os.environ.get('OFFLINE_HOST', '0.0.0.0'),
               int(port or os.environ.get('OFFLINE_PORT', '8890')))
    server = ThreadingHTTPServer(address, Handler)
    logging.info('Submit-Offline listening on %s:%s', *address)
    server.serve_forever()


if __name__ == '__main__':
    logging.basicConfig(level=logging.INFO)
    run()
