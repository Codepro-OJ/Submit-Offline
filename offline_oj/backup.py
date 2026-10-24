"""Private student folder snapshots. Uploaded files are stored, never executed."""

import hashlib
import json
import os
import tempfile
import uuid
from email import policy
from email.parser import BytesParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from . import db as store


MAX_FILES = 200
MAX_TOTAL_BYTES = 32 * 1024 * 1024
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_REQUEST_BYTES = 36 * 1024 * 1024


class BackupError(ValueError):
    pass


def safe_name(value):
    if not isinstance(value, str) or len(value) > 240 or '\\' in value or value.startswith('/'):
        raise BackupError('Đường dẫn file không hợp lệ.')
    parts = value.split('/')
    if len(parts) < 2 or any(not part or part in {'.', '..'} or ':' in part or
                             any(ord(char) < 32 for char in part) for part in parts):
        raise BackupError('Đường dẫn file không hợp lệ.')
    return '/'.join(parts)


def parse_folder(body, content_type):
    if not content_type.lower().startswith('multipart/form-data;') or 'boundary=' not in content_type.lower():
        raise BackupError('Cần chọn một thư mục để sao lưu.')
    raw = b'MIME-Version: 1.0\r\nContent-Type: ' + content_type.encode('ascii', 'strict') + b'\r\n\r\n' + body
    message = BytesParser(policy=policy.default).parsebytes(raw)
    if not message.is_multipart():
        raise BackupError('Dữ liệu thư mục không hợp lệ.')
    files = []
    names = set()
    total = 0
    for part in message.iter_parts():
        if part.get_content_disposition() != 'form-data' or part.get_param('name', header='content-disposition') != 'files':
            raise BackupError('Dữ liệu thư mục không hợp lệ.')
        name = safe_name(part.get_filename())
        if name in names:
            raise BackupError('Thư mục chứa tên file trùng nhau.')
        names.add(name)
        data = part.get_payload(decode=True)
        if data is None or len(data) > MAX_FILE_BYTES:
            raise BackupError('Một file vượt giới hạn 8 MB.')
        total += len(data)
        if total > MAX_TOTAL_BYTES or len(files) >= MAX_FILES:
            raise BackupError('Thư mục vượt giới hạn 200 file hoặc 32 MB.')
        files.append((name, data))
    if not files:
        raise BackupError('Thư mục không có file.')
    return files, total


def path_for(contest_key, user_id, backup_id):
    return store.BACKUPS_DIR / contest_key / str(user_id) / f'{backup_id}.zip'


def save_folder(contest_key, user_id, files, total):
    backup_id = uuid.uuid4().hex
    target = path_for(contest_key, user_id, backup_id)
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(prefix='.upload-', suffix='.zip', dir=target.parent,
                                     delete=False) as temporary:
        temp_path = Path(temporary.name)
    try:
        manifest = []
        with ZipFile(temp_path, 'w', compression=ZIP_DEFLATED) as archive:
            for name, data in files:
                archive.writestr(name, data)
                manifest.append({'path': name, 'bytes': len(data),
                                 'sha256': hashlib.sha256(data).hexdigest()})
            archive.writestr('backup-manifest.json', json.dumps({
                'contest': contest_key, 'files': manifest}, ensure_ascii=False, indent=2))
        digest = hashlib.sha256(temp_path.read_bytes()).hexdigest()
        os.replace(temp_path, target)
        return {'id': backup_id, 'file_count': len(files), 'bytes': total, 'sha256': digest}
    finally:
        temp_path.unlink(missing_ok=True)
