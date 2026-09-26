import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'server'))
import dashboard_build as build
import import_enrollments as importer
import portal


class DailyTests(unittest.TestCase):
    def test_daily_snapshots_keep_separate_days(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / 'daily.log'
            p.write_text('''=== WorkBuddy run 2026-09-25T07:00:00 ===
[07:01:00][账号1] ╭─ 👤 账号1  13800000001
[07:01:10][账号1] ✅签到成功
[07:01:11][账号1] 💰 积分: 主套餐剩余90积分(共100,已用10)
[07:01:12][账号1] 🏁 13800000001: 完成12/19 等级2 剩余: 任务A
=== WorkBuddy run 2026-09-26T07:00:00 ===
[07:01:00][账号1] ╭─ 👤 账号1  13800000001
[07:01:11][账号1] 今天已签到
[07:01:12][账号1] 💰 积分: 主套餐剩余85积分(共100,已用15)
''')
            rows = build.aggregate(build.parse_logs([p]))
            self.assertEqual(2, len(rows))
            self.assertEqual('签到成功', rows[('2026-09-25', '13800000001')]['sign_in'])
            self.assertEqual(85, rows[('2026-09-26', '13800000001')]['credits']['remaining'])


class PortalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.app = root / 'app'
        self.store = root / 'wb_refresh_tokens.json'
        self.store.write_text('{}')
        self.old_state, self.old_metrics, self.old_secret = portal.STATE, portal.METRICS, portal.ADMIN_SECRET
        self.old_app, self.old_store, self.old_lock, self.old_token_file = importer.APP, importer.STORE, importer.LOCK, importer.TOKEN_FILE
        portal.STATE, portal.METRICS = self.app, root / 'metrics.sqlite'
        portal.ADMIN_SECRET = 'test-admin-secret'
        importer.APP, importer.STORE, importer.LOCK, importer.TOKEN_FILE = self.app, self.store, root / 'runner.lock', root / 'access.txt'
        self.addCleanup(self.restore)
        self.server = portal.ThreadingHTTPServer(('127.0.0.1', 0), portal.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.server.server_close)
        self.addCleanup(self.server.shutdown)
        self.sms = patch.object(portal, 'send_sms')
        self.login = patch.object(portal, 'login', return_value=('access-sample', 'refresh-sample'))
        self.mock_sms = self.sms.start()
        self.mock_login = self.login.start()
        self.addCleanup(self.sms.stop)
        self.addCleanup(self.login.stop)

    def restore(self):
        portal.STATE, portal.METRICS, portal.ADMIN_SECRET = self.old_state, self.old_metrics, self.old_secret
        importer.APP, importer.STORE, importer.LOCK, importer.TOKEN_FILE = self.old_app, self.old_store, self.old_lock, self.old_token_file

    def call(self, path, data=None, cookie=None, admin=False):
        headers = {'Origin': 'https://aicn.wiki'}
        if data is not None:
            headers['Content-Type'] = 'application/json'
        if cookie:
            headers['Cookie'] = cookie
        if admin:
            headers['X-WorkBuddy-Admin'] = 'test-admin-secret'
        req = Request(f'http://127.0.0.1:{self.server.server_port}{path}',
                      data=json.dumps(data).encode() if data is not None else None,
                      headers=headers)
        try:
            with urlopen(req, timeout=3) as res:
                return res.status, json.load(res), res.headers.get('Set-Cookie')
        except HTTPError as exc:
            return exc.code, json.load(exc), exc.headers.get('Set-Cookie')

    def enroll(self, phone):
        invite = portal.create_invites(1)[0]
        status, _, _ = self.call('/api/send', {'phone': phone, 'invite': invite})
        self.assertEqual(200, status)
        status, _, cookie = self.call('/api/verify', {'phone': phone, 'code': '123456'})
        self.assertEqual(200, status)
        return cookie.split(';')[0]

    def test_invite_sms_isolation_and_queue_import(self):
        status, data, _ = self.call('/api/me')
        self.assertEqual((200, False), (status, data['authenticated']))
        status, _, _ = self.call('/api/send', {'phone': '13800000001', 'invite': 'bad'})
        self.assertEqual(400, status)
        self.mock_sms.assert_not_called()
        one = self.enroll('13800000001')
        two = self.enroll('13800000002')
        status, data, _ = self.call('/api/me', cookie=one)
        self.assertEqual(200, status)
        self.assertEqual(['13800000001'], [a['account_id'] for a in data['accounts']])
        self.assertEqual('queued', data['accounts'][0]['state'])
        self.assertEqual(2, len(list((self.app / 'queue').glob('*.json'))))
        self.assertEqual(0, importer.run())
        self.assertEqual(2, len(json.loads(self.store.read_text())))
        status, data, _ = self.call('/api/me', cookie=two)
        self.assertEqual(['13800000002'], [a['account_id'] for a in data['accounts']])
        self.assertEqual('active', data['accounts'][0]['state'])
        status, _, _ = self.call('/api/remove', {'account_id': '13800000002', 'csrf': data['csrf']}, cookie=one)
        self.assertEqual(403 if status == 403 else 404, status)
        status, _, _ = self.call('/api/remove', {'account_id': '13800000002', 'csrf': data['csrf']}, cookie=two)
        self.assertEqual(200, status)
        importer.run()
        self.assertNotIn('13800000002', json.loads(self.store.read_text()))
        self.assertIn('13800000001', json.loads(self.store.read_text()))

    def test_phone_rate_limit(self):
        invite = portal.create_invites(1)[0]
        self.assertEqual(200, self.call('/api/send', {'phone': '13800000001', 'invite': invite})[0])
        self.assertEqual(400, self.call('/api/send', {'phone': '13800000001', 'invite': invite})[0])
        self.assertEqual(1, self.mock_sms.call_count)

    def test_admin_invites_and_account_removal_require_proxy_secret(self):
        self.assertEqual(403, self.call('/api/admin/state')[0])
        self.assertEqual(403, self.call('/api/admin/invites', {'count': 1})[0])
        status, data, _ = self.call('/api/admin/invites', {'count': 1}, admin=True)
        self.assertEqual(200, status)
        self.assertEqual(1, len(data['urls']))
        self.assertEqual(data['urls'][0], self.call('/api/admin/state', admin=True)[1]['invites'][0]['url'])
        invite = data['urls'][0].split('invite=', 1)[1]
        self.assertEqual(200, self.call('/api/send', {'phone': '13800000003', 'invite': invite})[0])
        self.assertEqual(200, self.call('/api/verify', {'phone': '13800000003', 'code': '123456'})[0])
        self.assertEqual(1, len(self.call('/api/admin/state', admin=True)[1]['accounts']))
        self.assertEqual(403, self.call('/api/admin/remove', {'account_id': '13800000003'})[0])
        self.assertEqual(200, self.call('/api/admin/remove', {'account_id': '13800000003'}, admin=True)[0])
        importer.run()
        self.assertNotIn('13800000003', json.loads(self.store.read_text()))


if __name__ == '__main__':
    unittest.main()
