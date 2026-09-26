#!/usr/bin/env python3
"""Invite-only self-service enrollment for WorkBuddy Daily.

Run behind an HTTPS reverse proxy, bound to loopback as an unprivileged user.
The web process cannot read the runner's existing credential store: newly
verified credentials go into a private queue imported by root separately.
"""
from __future__ import annotations

from contextlib import closing
import hashlib
import hmac
import http.cookies
import json
import os
import re
import secrets
import sqlite3
import sys
import tempfile
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

STATE = Path(os.environ.get('WB_PORTAL_APP_STATE', '/var/lib/workbuddy-portal/app'))
METRICS = Path(os.environ.get('WB_PORTAL_METRICS', '/var/lib/workbuddy-portal/metrics.sqlite'))
HTML = Path(__file__).with_name('portal.html')
ADMIN_HTML = Path(__file__).with_name('admin.html')
ADMIN_SECRET = os.environ.get('WB_ADMIN_PROXY_SECRET', '')
BASE = 'https://www.workbuddy.cn'
SEND_URL = BASE + '/v2/plugin/login/send-sms'
LOGIN_URL = BASE + '/v2/plugin/login/token'
HEADERS = {'Content-Type': 'application/json', 'Accept': 'application/json, text/plain, */*',
           'Origin': BASE, 'Referer': BASE + '/',
           'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
                         '(KHTML, like Gecko) Chrome/138.0.7204.251 Safari/537.36'}
COOKIE = 'wb_portal_session'
SESSION_LIFE = 30 * 86400
PHONE_RE = re.compile(r'^(?:1\d{10}|\d{8})$')


class UpstreamError(Exception):
    pass


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def mask(phone: str) -> str:
    return phone[:3] + '****' + phone[-4:] if len(phone) == 11 else '+852 ' + phone[:4] + ' ****'


def database() -> sqlite3.Connection:
    STATE.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(STATE, 0o700)
    db = sqlite3.connect(STATE / 'portal.sqlite', timeout=15)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
      CREATE TABLE IF NOT EXISTS owners (id TEXT PRIMARY KEY, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS accounts (phone TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        state TEXT NOT NULL, created_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS invites (code_hash TEXT PRIMARY KEY,
        created_at INTEGER NOT NULL, used_at INTEGER, owner_id TEXT, code TEXT);
      CREATE TABLE IF NOT EXISTS sessions (token_hash TEXT PRIMARY KEY, owner_id TEXT NOT NULL,
        csrf TEXT NOT NULL, expires_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS sms_attempts (phone TEXT NOT NULL, ip TEXT NOT NULL,
        sent_at INTEGER NOT NULL);
      CREATE TABLE IF NOT EXISTS pending (phone TEXT PRIMARY KEY, ip TEXT NOT NULL,
        sent_at INTEGER NOT NULL, invite_hash TEXT, owner_id TEXT, attempts INTEGER NOT NULL DEFAULT 0);
    ''')
    if 'code' not in [row['name'] for row in db.execute('PRAGMA table_info(invites)')]:
        db.execute('ALTER TABLE invites ADD COLUMN code TEXT')
    db.commit()
    os.chmod(STATE / 'portal.sqlite', 0o600)
    return db


def clean_phone(value: str) -> str:
    phone = re.sub(r'[\s+\-()]', '', value or '')
    if phone.startswith('852') and len(phone) == 11:
        phone = phone[3:]
    if not PHONE_RE.fullmatch(phone):
        raise ValueError('请输入有效的中国大陆或香港手机号')
    return phone


def require_rate_limit(db: sqlite3.Connection, phone: str, ip: str, now: int) -> None:
    db.execute('DELETE FROM sms_attempts WHERE sent_at<?', (now - 86400,))
    last = db.execute('SELECT MAX(sent_at) FROM sms_attempts WHERE phone=?', (phone,)).fetchone()[0]
    if last and now - last < 60:
        raise ValueError('发送太频繁，请一分钟后重试')
    hour_phone = db.execute('SELECT COUNT(*) FROM sms_attempts WHERE phone=? AND sent_at>?',
                            (phone, now - 3600)).fetchone()[0]
    hour_ip = db.execute('SELECT COUNT(*) FROM sms_attempts WHERE ip=? AND sent_at>?',
                         (ip, now - 3600)).fetchone()[0]
    if hour_phone >= 3 or hour_ip >= 10:
        raise ValueError('发送次数已达上限，请稍后再试')


def post_workbuddy(url: str, payload: dict, timeout: int) -> dict:
    request = Request(url, data=json.dumps(payload).encode(), headers=HEADERS, method='POST')
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.load(response)
    except (HTTPError, URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise UpstreamError('WorkBuddy 服务暂时不可用') from exc
    if not isinstance(data, dict):
        raise UpstreamError('WorkBuddy 返回数据异常')
    return data


def send_sms(phone: str) -> None:
    data = post_workbuddy(SEND_URL, {'phone': phone}, 20)
    if data.get('code') != 0:
        raise ValueError(str(data.get('msg') or '验证码发送失败')[:100])


def login(phone: str, code: str) -> tuple[str, str]:
    data = post_workbuddy(LOGIN_URL,
        {'login_method': 'phone', 'phone': phone, 'sms_code': code}, 25)
    if data.get('code') != 0:
        raise ValueError('验证码错误或已过期')
    inner = data.get('data') or {}
    access = inner.get('accessToken') or inner.get('access_token')
    refresh = inner.get('refreshToken') or inner.get('refresh_token')
    if not access or not refresh:
        raise ValueError('登录成功但未获得完整授权，请稍后重试')
    return access, refresh


def queue_change(action: str, phone: str, owner_id: str, access: str = '', refresh: str = '') -> None:
    queue = STATE / 'queue'
    queue.mkdir(mode=0o700, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.pending-', dir=queue)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump({'action': action, 'phone': phone, 'owner_id': owner_id,
                       'access_token': access, 'refresh_token': refresh, 'created_at': int(time.time())}, out)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, queue / (uuid.uuid4().hex + '.json'))
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def daily_for(phone: str) -> list[dict]:
    if not METRICS.exists():
        return []
    db = sqlite3.connect(f'file:{METRICS}?mode=ro', uri=True)
    try:
        return [json.loads(row[0]) for row in db.execute(
            'SELECT snapshot FROM daily WHERE phone=? ORDER BY day DESC LIMIT 30', (phone,))]
    finally:
        db.close()


def create_invites(count: int) -> list[str]:
    if not 1 <= count <= 100:
        raise ValueError('count must be 1..100')
    with closing(database()) as db:
        codes = [secrets.token_urlsafe(18) for _ in range(count)]
        db.executemany('INSERT INTO invites(code_hash,created_at,code) VALUES(?,?,?)',
                       [(digest(code), int(time.time()), code) for code in codes])
        db.commit()
    return codes


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *args):
        pass

    def respond(self, status: int, data: object, cookie: str | None = None) -> None:
        raw = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        if cookie:
            self.send_header('Set-Cookie', cookie)
        self.end_headers()
        self.wfile.write(raw)

    def session(self, db: sqlite3.Connection) -> sqlite3.Row | None:
        jar = http.cookies.SimpleCookie()
        try:
            jar.load(self.headers.get('Cookie') or '')
            token = jar[COOKIE].value if COOKIE in jar else ''
        except http.cookies.CookieError:
            return None
        if not token:
            return None
        return db.execute('SELECT owner_id,csrf FROM sessions WHERE token_hash=? AND expires_at>?',
                          (digest(token), int(time.time()))).fetchone()

    def json_body(self) -> dict:
        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
            raise ValueError('请求格式不正确')
        length = int(self.headers.get('Content-Length') or 0)
        if not 0 < length <= 4096:
            raise ValueError('请求内容过大或为空')
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError('请求内容不正确')
        return data

    def same_origin(self) -> bool:
        return self.headers.get('Origin') == 'https://aicn.wiki'

    def ip(self) -> str:
        # Caddy overwrites this header with its own observed remote address.
        return (self.headers.get('X-Real-IP') or self.client_address[0])[:80]

    def is_admin(self) -> bool:
        return bool(ADMIN_SECRET) and hmac.compare_digest(
            self.headers.get('X-WorkBuddy-Admin') or '', ADMIN_SECRET)

    def html(self, path: Path) -> None:
        raw = path.read_bytes()
        self.send_response(200)
        self.send_header('Content-Type', 'text/html; charset=utf-8')
        self.send_header('Content-Length', str(len(raw)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; connect-src 'self'; base-uri 'none'; frame-ancestors 'none'")
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path.startswith('/admin/') or path.startswith('/api/admin/'):
            if not self.is_admin():
                self.respond(403, {'error': '无权访问'})
                return
            if path in ('/admin/', '/admin/index.html'):
                self.html(ADMIN_HTML)
            elif path == '/api/admin/state':
                with closing(database()) as db:
                    accounts = []
                    for row in db.execute('SELECT phone,state,created_at FROM accounts WHERE state!=? ORDER BY created_at DESC', ('removed',)):
                        days = daily_for(row['phone'])
                        latest = days[0] if days else None
                        accounts.append({'account_id': row['phone'], 'phone': mask(row['phone']),
                                         'state': row['state'], 'created_at': row['created_at'],
                                         'latest': latest})
                    invites = [{'url': 'https://aicn.wiki/workbuddy/join/?invite=' + row['code'],
                                'created_at': row['created_at']} for row in db.execute(
                        'SELECT code,created_at FROM invites WHERE used_at IS NULL AND code IS NOT NULL ORDER BY created_at DESC')]
                    hidden = db.execute('SELECT COUNT(*) FROM invites WHERE used_at IS NULL AND code IS NULL').fetchone()[0]
                    self.respond(200, {'accounts': accounts, 'invites': invites, 'unlisted_invites': hidden})
            else:
                self.respond(404, {'error': '页面不存在'})
            return
        if path in ('/', '/index.html'):
            self.html(HTML)
            return
        if path == '/api/me':
            with closing(database()) as db:
                session = self.session(db)
                if not session:
                    self.respond(200, {'authenticated': False})
                    return
                accounts = []
                for row in db.execute('SELECT phone,state FROM accounts WHERE owner_id=? AND state!=? ORDER BY created_at',
                                      (session['owner_id'], 'removed')):
                    accounts.append({'phone': mask(row['phone']), 'account_id': row['phone'],
                        'state': row['state'], 'days': daily_for(row['phone'])})
                self.respond(200, {'authenticated': True, 'csrf': session['csrf'], 'accounts': accounts})
            return
        self.respond(404, {'error': '页面不存在'})

    def do_POST(self):
        path = urlsplit(self.path).path
        if path.startswith('/api/admin/') and not self.is_admin():
            self.respond(403, {'error': '无权访问'})
            return
        if not self.same_origin():
            self.respond(403, {'error': '请求来源不正确'})
            return
        try:
            data = self.json_body()
            with closing(database()) as db:
                session = self.session(db)
                if path == '/api/admin/invites':
                    count = data.get('count', 1)
                    if type(count) is not int or not 1 <= count <= 10:
                        raise ValueError('每次可生成 1 到 10 条邀请')
                    codes = create_invites(count)
                    self.respond(200, {'urls': ['https://aicn.wiki/workbuddy/join/?invite=' + code for code in codes]})
                elif path == '/api/admin/remove':
                    phone = clean_phone(str(data.get('account_id') or ''))
                    row = db.execute('SELECT owner_id FROM accounts WHERE phone=? AND state!=?',
                                     (phone, 'removed')).fetchone()
                    if not row:
                        self.respond(404, {'error': '找不到这个账号'})
                    else:
                        queue_change('remove', phone, row['owner_id'])
                        db.execute('UPDATE accounts SET state=? WHERE phone=?', ('removing', phone))
                        db.commit()
                        self.respond(200, {'ok': True})
                elif path == '/api/send':
                    self.send_code(db, data, session)
                elif path == '/api/verify':
                    self.verify(db, data, session)
                elif path == '/api/remove':
                    self.remove(db, data, session)
                elif path == '/api/logout':
                    self.logout(db, session)
                else:
                    self.respond(404, {'error': '接口不存在'})
        except (ValueError, json.JSONDecodeError) as exc:
            self.respond(400, {'error': str(exc)[:120]})
        except (UpstreamError, sqlite3.Error, OSError):
            self.respond(503, {'error': '服务暂时不可用，请稍后重试'})

    def send_code(self, db: sqlite3.Connection, data: dict, session: sqlite3.Row | None):
        phone = clean_phone(str(data.get('phone') or ''))
        now = int(time.time())
        owner = db.execute('SELECT owner_id FROM accounts WHERE phone=? AND state!=?',
                           (phone, 'removed')).fetchone()
        invite_hash = None
        if session:
            if owner and owner['owner_id'] != session['owner_id']:
                raise ValueError('这个账号已由其他用户管理')
            owner_id = session['owner_id']
        elif owner:
            owner_id = owner['owner_id']
        else:
            invite_hash = digest(str(data.get('invite') or ''))
            invite = db.execute('SELECT code_hash FROM invites WHERE code_hash=? AND used_at IS NULL',
                                (invite_hash,)).fetchone()
            if not invite:
                raise ValueError('请填写有效的邀请码')
            owner_id = None
        ip = self.ip()
        require_rate_limit(db, phone, ip, now)
        send_sms(phone)
        db.execute('INSERT INTO sms_attempts(phone,ip,sent_at) VALUES(?,?,?)', (phone, ip, now))
        db.execute('INSERT INTO pending(phone,ip,sent_at,invite_hash,owner_id,attempts) VALUES(?,?,?,?,?,0) '
                   'ON CONFLICT(phone) DO UPDATE SET ip=excluded.ip,sent_at=excluded.sent_at,'
                   'invite_hash=excluded.invite_hash,owner_id=excluded.owner_id,attempts=0',
                   (phone, ip, now, invite_hash, owner_id))
        db.commit()
        self.respond(200, {'ok': True, 'message': '验证码已发送'})

    def verify(self, db: sqlite3.Connection, data: dict, session: sqlite3.Row | None):
        phone = clean_phone(str(data.get('phone') or ''))
        code = str(data.get('code') or '')
        if not re.fullmatch(r'\d{4,8}', code):
            raise ValueError('验证码格式不正确')
        pending = db.execute('SELECT * FROM pending WHERE phone=?', (phone,)).fetchone()
        if not pending or int(time.time()) - pending['sent_at'] > 300 or pending['ip'] != self.ip():
            raise ValueError('验证码已过期，请重新发送')
        if pending['attempts'] >= 5:
            raise ValueError('验证码尝试次数过多，请重新发送')
        db.execute('UPDATE pending SET attempts=attempts+1 WHERE phone=?', (phone,))
        db.commit()
        access, refresh = login(phone, code)
        owner = db.execute('SELECT owner_id FROM accounts WHERE phone=? AND state!=?',
                           (phone, 'removed')).fetchone()
        if owner:
            owner_id = owner['owner_id']
            if session and session['owner_id'] != owner_id:
                raise ValueError('这个账号已由其他用户管理')
        elif pending['owner_id']:
            owner_id = pending['owner_id']
            if not session or session['owner_id'] != owner_id:
                raise ValueError('登录状态已变化，请重新开始')
        else:
            invite = db.execute('SELECT used_at FROM invites WHERE code_hash=?',
                                (pending['invite_hash'],)).fetchone()
            if not invite or invite['used_at'] is not None:
                raise ValueError('邀请码已使用，请重新开始')
            owner_id = uuid.uuid4().hex
            db.execute('INSERT INTO owners(id,created_at) VALUES(?,?)', (owner_id, int(time.time())))
            updated = db.execute('UPDATE invites SET used_at=?,owner_id=? '
                                 'WHERE code_hash=? AND used_at IS NULL',
                                 (int(time.time()), owner_id, pending['invite_hash']))
            if updated.rowcount != 1:
                raise ValueError('邀请码已使用，请重新开始')
        queue_change('add', phone, owner_id, access, refresh)
        db.execute('INSERT INTO accounts(phone,owner_id,state,created_at) VALUES(?,?,?,?) '
                   'ON CONFLICT(phone) DO UPDATE SET owner_id=excluded.owner_id,state=excluded.state',
                   (phone, owner_id, 'queued', int(time.time())))
        db.execute('DELETE FROM pending WHERE phone=?', (phone,))
        token = secrets.token_urlsafe(32)
        csrf = secrets.token_urlsafe(24)
        db.execute('INSERT INTO sessions(token_hash,owner_id,csrf,expires_at) VALUES(?,?,?,?)',
                   (digest(token), owner_id, csrf, int(time.time()) + SESSION_LIFE))
        db.commit()
        cookie = f'{COOKIE}={token}; Path=/workbuddy/join/; Max-Age={SESSION_LIFE}; Secure; HttpOnly; SameSite=Strict'
        self.respond(200, {'ok': True, 'message': '账号已加入，下一次自动运行前会生效'}, cookie)

    def remove(self, db: sqlite3.Connection, data: dict, session: sqlite3.Row | None):
        if not session or not hmac.compare_digest(str(data.get('csrf') or ''), session['csrf']):
            self.respond(403, {'error': '请重新登录'})
            return
        phone = clean_phone(str(data.get('account_id') or ''))
        row = db.execute('SELECT owner_id FROM accounts WHERE phone=? AND state!=?',
                         (phone, 'removed')).fetchone()
        if not row or row['owner_id'] != session['owner_id']:
            self.respond(404, {'error': '找不到这个账号'})
            return
        queue_change('remove', phone, session['owner_id'])
        db.execute('UPDATE accounts SET state=? WHERE phone=?', ('removing', phone))
        db.commit()
        self.respond(200, {'ok': True})

    def logout(self, db: sqlite3.Connection, session: sqlite3.Row | None):
        if session:
            db.execute('DELETE FROM sessions WHERE owner_id=?', (session['owner_id'],))
            db.commit()
        self.respond(200, {'ok': True}, f'{COOKIE}=; Path=/workbuddy/join/; Max-Age=0; Secure; HttpOnly; SameSite=Strict')


def main():
    if len(sys.argv) > 1 and sys.argv[1] == 'invite':
        count = int(sys.argv[2]) if len(sys.argv) > 2 else 1
        for code in create_invites(count):
            print(code)
        return
    database().close()
    server = ThreadingHTTPServer(('127.0.0.1', int(os.environ.get('WB_PORTAL_PORT', '18886'))), Handler)
    server.serve_forever()


if __name__ == '__main__':
    main()
