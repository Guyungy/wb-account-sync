import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'tools'))
import wb_account_switch as sw


class SwitchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.store = root / 'store/accounts.json'
        self.auth = root / 'auth/workbuddy-desktop.info'
        self.legacy = root / 'legacy/accounts.json'
        self.db = root / 'workbuddy.db'
        self.db.write_bytes(b'history-unchanged')
        self.auth.parent.mkdir()
        self.auth.write_text(json.dumps({'account': {'uid': 'first', 'nickname': 'Current'},
            'auth': {'accessToken': {'$wbEncrypted': True, 'envelope': 'opaque'},
                     'refreshToken': 'refresh'}, 'allAccounts': [{'uid': 'first'}]}))
        self.legacy.parent.mkdir()
        self.legacy.write_text(json.dumps([{'uid': 'second', 'nickname': 'Second',
             'access_token': 'second-token', 'refresh_token': 'second-refresh',
             'profile_raw': {'uid': 'second', 'phoneNumber': 'masked'},
             'auth_raw': {'auth': {'scope': 'openid'}}}]))

    def test_import_and_switch_preserves_history_and_encrypted_current_token(self):
        view = sw.discover(self.store, self.auth, self.legacy)
        self.assertEqual(2, len(view['accounts']))
        saved = json.loads(self.store.read_text())
        self.assertEqual({'$wbEncrypted': True, 'envelope': 'opaque'},
                         next(a for a in saved if a['uid'] == 'first')['access_token'])
        with patch.object(sw, '_running', return_value=False), patch.object(sw, '_launch'):
            result = sw.switch('second', self.store, self.auth, self.legacy)
        self.assertTrue(result['ok'])
        active = json.loads(self.auth.read_text())
        self.assertEqual('second', active['account']['uid'])
        self.assertEqual('second-token', active['auth']['accessToken'])
        self.assertEqual('openid', active['auth']['scope'])
        self.assertEqual(b'history-unchanged', self.db.read_bytes())
        self.assertEqual('first', json.loads(Path(result['backup']).read_text())['account']['uid'])
        self.assertEqual(0o600, self.auth.stat().st_mode & 0o777)
        self.assertEqual(0o600, self.store.stat().st_mode & 0o777)

    def test_launch_failure_rolls_back_auth(self):
        before = self.auth.read_bytes()
        with patch.object(sw, '_running', return_value=False), patch.object(sw, '_launch', side_effect=sw.SwitchError('launch failed')):
            with self.assertRaises(sw.SwitchError):
                sw.switch('second', self.store, self.auth, self.legacy)
        self.assertEqual(json.loads(before), json.loads(self.auth.read_bytes()))


if __name__ == '__main__':
    unittest.main()
