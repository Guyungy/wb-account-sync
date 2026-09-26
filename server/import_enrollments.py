#!/usr/bin/env python3
"""Root-side importer for self-service enrollments; run every minute."""
from __future__ import annotations

import fcntl
import json
import os
import re
import sqlite3
import stat
import tempfile
import time
from pathlib import Path

APP = Path(os.environ.get('WB_PORTAL_APP_STATE', '/var/lib/workbuddy-portal/app'))
STORE = Path(os.environ.get('WB_REFRESH_STORE', '/opt/workbuddy-daily/wb_refresh_tokens.json'))
TOKEN_FILE = Path(os.environ.get('WB_ACCESS_TOKENS', '/opt/workbuddy-daily/WORKBUDDY_ACCESS_TOKEN.txt'))
LOCK = Path(os.environ.get('WB_DAILY_LOCK', '/var/lock/workbuddy-daily.lock'))
NAME = re.compile(r'^[a-f0-9]{32}\.json$')


def atomic_store(data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.wb-refresh-', dir=STORE.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(data, out, ensure_ascii=False, indent=1)
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, STORE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def atomic_tokens(data: dict) -> None:
    fd, tmp = tempfile.mkstemp(prefix='.wb-access-', dir=TOKEN_FILE.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            out.write('@'.join(v['access_token'] for v in data.values() if v.get('access_token')))
            out.flush()
            os.fsync(out.fileno())
        os.replace(tmp, TOKEN_FILE)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def read_queue(path: Path) -> dict:
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > 60000:
            raise ValueError('Invalid queue entry')
        with os.fdopen(fd, encoding='utf-8') as stream:
            fd = -1
            return json.load(stream)
    finally:
        if fd >= 0:
            os.close(fd)


def run() -> int:
    queue = APP / 'queue'
    if not queue.exists():
        return 0
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open('a+') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return 0
        portal_db = APP / 'portal.sqlite'
        if not portal_db.exists():
            return 0
        with sqlite3.connect(portal_db) as db:
            db.row_factory = sqlite3.Row
            for item in sorted(queue.iterdir()):
                if not NAME.fullmatch(item.name):
                    continue
                try:
                    change = read_queue(item)
                    phone, owner, action = change['phone'], change['owner_id'], change['action']
                    if not isinstance(phone, str) or not re.fullmatch(r'(?:1\d{10}|\d{8})', phone):
                        raise ValueError('Invalid phone')
                    row = db.execute('SELECT owner_id,state FROM accounts WHERE phone=?', (phone,)).fetchone()
                    expected = 'queued' if action == 'add' else 'removing'
                    if not row or row['owner_id'] != owner or row['state'] != expected:
                        item.unlink()
                        continue
                    store = json.loads(STORE.read_text()) if STORE.exists() else {}
                    if action == 'add':
                        access, refresh = change['access_token'], change['refresh_token']
                        if not isinstance(access, str) or not isinstance(refresh, str) or not access or not refresh:
                            raise ValueError('Missing credential')
                        store[phone] = {'access_token': access, 'refresh_token': refresh,
                                        'updated': time.strftime('%Y-%m-%d %H:%M')}
                        next_state = 'active'
                    elif action == 'remove':
                        store.pop(phone, None)
                        next_state = 'removed'
                    else:
                        raise ValueError('Invalid action')
                    atomic_store(store)
                    atomic_tokens(store)
                    db.execute('UPDATE accounts SET state=? WHERE phone=? AND owner_id=?',
                               (next_state, phone, owner))
                    db.commit()
                    item.unlink()
                    print(f'Processed {action} for account {phone[:3]}****{phone[-4:]}')
                except (OSError, ValueError, KeyError, json.JSONDecodeError, sqlite3.Error) as exc:
                    print(f'Enrollment import skipped {item.name}: {type(exc).__name__}')
    return 0


if __name__ == '__main__':
    raise SystemExit(run())
