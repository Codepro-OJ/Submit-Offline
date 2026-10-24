import json
import io
import shutil
import tempfile
import threading
import unittest
from http.cookiejar import CookieJar
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, build_opener, HTTPCookieProcessor
from zipfile import ZipFile

import manage
from offline_oj import db as store
from offline_oj import server
from offline_oj.judge import score_cases


class OfflineFlowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        store.DATA_DIR = root / 'data'
        store.DB_PATH = store.DATA_DIR / 'oj.sqlite3'
        store.PROBLEMS_DIR = root / 'problems'
        store.BACKUPS_DIR = root / 'backups' / 'student-folders'
        shutil.copytree(store.ROOT / 'examples' / 'SUM', store.PROBLEMS_DIR / 'SUM')
        store.import_contest(store.ROOT / 'config' / 'contest.example.json')
        manage.add_user('hs001', 'password123')
        manage.add_user('hs002', 'password456')
        manage.add_user('operator', 'adminpass123', 'admin')
        manage.enroll('hs001', 'DEMO2026')
        manage.enroll('hs002', 'DEMO2026')
        server.CONTEST_KEY = 'DEMO2026'
        self.http = ThreadingHTTPServer(('127.0.0.1', 0), server.Handler)
        self.thread = threading.Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.http.server_close)
        self.addCleanup(self.http.shutdown)
        self.url = f'http://127.0.0.1:{self.http.server_port}'
        self.client = build_opener(HTTPCookieProcessor(CookieJar()))
        self.other_client = build_opener(HTTPCookieProcessor(CookieJar()))

    def request(self, path, method='GET', body=None, token=None, client=None):
        headers = {'Content-Type': 'application/json'}
        if token:
            headers['Authorization'] = f'Bearer {token}'
        request = Request(self.url + path, data=json.dumps(body).encode() if body is not None else None,
                          headers=headers, method=method)
        try:
            with (client or self.client).open(request, timeout=3) as response:
                return response.status, json.load(response)
        except HTTPError as error:
            return error.code, json.load(error)

    def login(self, username, password, client=None):
        status, data = self.request('/api/login', 'POST', {'username': username, 'password': password},
                                    client=client)
        self.assertEqual(status, 200)
        return data['token']

    def test_login_submit_and_isolate_contest_data(self):
        status, _ = self.request('/api/contest')
        self.assertEqual(status, 401)
        token = self.login('hs001', 'password123')
        status, contest = self.request('/api/contest', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(contest['contest']['problems'][0]['code'], 'SUM')
        status, _ = self.request('/api/problem/OTHER', token=token)
        self.assertEqual(status, 404)
        status, result = self.request('/api/submit', 'POST',
                                      {'problem': 'SUM', 'language': 'CPP17',
                                       'source': 'int main(){}', 'contest': 'OTHER'}, token)
        self.assertEqual(status, 201)
        submission_id = result['id']
        with store.transaction() as db:
            saved = db.execute('SELECT contest_key,status FROM submissions WHERE id=?',
                               (submission_id,)).fetchone()
        self.assertEqual((saved['contest_key'], saved['status']), ('DEMO2026', 'QUEUED'))
        token2 = self.login('hs002', 'password456')
        status, data = self.request('/api/submissions', token=token2)
        self.assertEqual(status, 200)
        self.assertEqual(data['submissions'], [])
        status, _ = self.request(f'/api/submission/{submission_id}', token=token2)
        self.assertEqual(status, 404)

    def test_server_closes_submission_after_end(self):
        token = self.login('hs001', 'password123')
        with store.transaction() as db:
            db.execute("UPDATE contests SET end_at='2020-01-02T00:00:00Z' WHERE key='DEMO2026'")
        status, _ = self.request('/api/submit', 'POST',
                                 {'problem': 'SUM', 'language': 'CPP17', 'source': 'int main(){}'}, token)
        self.assertEqual(status, 403)

    def test_new_login_replaces_old_session_and_logout_revokes_token(self):
        old_token = self.login('hs001', 'password123')
        new_token = self.login('hs001', 'password123')
        status, _ = self.request('/api/contest', token=old_token)
        self.assertEqual(status, 401)
        status, _ = self.request('/api/logout', 'POST', {}, new_token)
        self.assertEqual(status, 200)
        status, _ = self.request('/api/contest', token=new_token)
        self.assertEqual(status, 401)

    def test_other_device_is_denied_until_admin_releases_session(self):
        first_token = self.login('hs001', 'password123')
        status, _ = self.request('/api/contest', token=first_token, client=self.other_client)
        self.assertEqual(status, 401)
        status, _ = self.request('/api/login', 'POST',
                                 {'username': 'hs001', 'password': 'password123'},
                                 client=self.other_client)
        self.assertEqual(status, 409)
        status, result = self.request('/api/admin/login', 'POST',
                                      {'username': 'operator', 'password': 'adminpass123'})
        self.assertEqual(status, 200)
        admin_token = result['token']
        status, dashboard = self.request('/api/admin/dashboard', token=admin_token)
        self.assertEqual(status, 200)
        session = next(row for row in dashboard['sessions'] if row['username'] == 'hs001')
        self.assertEqual(session['client_ip'], '127.0.0.1')
        self.assertTrue(session['device'])
        status, _ = self.request('/api/admin/session/release', 'POST',
                                 {'username': 'hs001'}, admin_token)
        self.assertEqual(status, 200)
        second_token = self.login('hs001', 'password123', self.other_client)
        self.assertNotEqual(first_token, second_token)
        status, _ = self.request('/api/contest', token=first_token)
        self.assertEqual(status, 401)

    def test_folder_backup_is_private_and_preserves_paths(self):
        token = self.login('hs001', 'password123')
        boundary = 'exam-test-boundary'
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                   'filename="Exam/main.cpp"\r\nContent-Type: text/plain\r\n\r\n'
                   'int main(){}\r\n'
                   f'--{boundary}--\r\n').encode()
        request = Request(self.url + '/api/backup', data=payload, method='POST', headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with self.client.open(request, timeout=3) as response:
            self.assertEqual(response.status, 201)
            saved = json.load(response)
        self.assertEqual(saved['file_count'], 1)
        status, data = self.request('/api/backups', token=token)
        self.assertEqual(status, 200)
        self.assertEqual(data['backups'][0]['id'], saved['id'])
        archive = store.BACKUPS_DIR / 'DEMO2026' / '1' / f'{saved["id"]}.zip'
        with ZipFile(archive) as zip_file:
            self.assertEqual(zip_file.read('Exam/main.cpp'), b'int main(){}')
        status, _ = self.request(f'/api/admin/backup/{saved["id"]}', token=token)
        self.assertEqual(status, 403)
        status, admin = self.request('/api/admin/login', 'POST',
                                     {'username': 'operator', 'password': 'adminpass123'})
        self.assertEqual(status, 200)
        download = Request(self.url + f'/api/admin/backup/{saved["id"]}',
                           headers={'Authorization': f'Bearer {admin["token"]}'})
        with self.client.open(download, timeout=3) as response:
            with ZipFile(io.BytesIO(response.read())) as zip_file:
                self.assertEqual(zip_file.read('Exam/main.cpp'), b'int main(){}')

    def test_folder_backup_rejects_path_traversal(self):
        token = self.login('hs001', 'password123')
        boundary = 'exam-test-boundary'
        payload = (f'--{boundary}\r\nContent-Disposition: form-data; name="files"; '
                   'filename="Exam/../evil.cpp"\r\n\r\nX\r\n'
                   f'--{boundary}--\r\n').encode()
        request = Request(self.url + '/api/backup', data=payload, method='POST', headers={
            'Authorization': f'Bearer {token}',
            'Content-Type': f'multipart/form-data; boundary={boundary}'})
        with self.assertRaises(HTTPError) as caught:
            self.client.open(request, timeout=3)
        self.assertEqual(caught.exception.code, 400)

    def test_subtask_points_require_all_cases_in_group(self):
        cases = [{'group': 'a', 'group_points': 60},
                 {'group': 'a', 'group_points': 60},
                 {'points': 40}]
        self.assertEqual(score_cases(cases, ['AC', 'WA', 'AC']), (100.0, 40.0))
        self.assertEqual(score_cases(cases, ['AC', 'AC', 'WA']), (100.0, 60.0))

    def test_admin_dashboard_is_role_protected(self):
        contestant = self.login('hs001', 'password123')
        status, _ = self.request('/api/admin/dashboard', token=contestant)
        self.assertEqual(status, 403)
        status, _ = self.request('/api/login', 'POST',
                                 {'username': 'operator', 'password': 'adminpass123'})
        self.assertEqual(status, 403)
        status, result = self.request('/api/admin/login', 'POST',
                                      {'username': 'operator', 'password': 'adminpass123'})
        self.assertEqual(status, 200)
        admin_token = result['token']
        status, data = self.request('/api/admin/dashboard', token=admin_token)
        self.assertEqual(status, 200)
        self.assertEqual(data['participants'], 2)
        status, _ = self.request('/api/contest', token=admin_token)
        self.assertEqual(status, 403)


if __name__ == '__main__':
    unittest.main()
