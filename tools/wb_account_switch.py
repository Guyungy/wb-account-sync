#!/usr/bin/env python3
"""Local WorkBuddy account switching. Credentials never leave this computer.

The auth-file shape and encrypted-token compatibility were informed by the
MIT-licensed wb-switch project (https://github.com/changexbc/workbuddy-switch).
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import wb_platform


class SwitchError(Exception):
    pass


def auth_path() -> Path:
    base = Path.home()
    if sys.platform == 'darwin':
        base /= 'Library/Application Support/CodeBuddyExtension/Data/Public/auth'
    elif sys.platform.startswith('win'):
        base /= 'AppData/Local/CodeBuddyExtension/Data/Public/auth'
    else:
        base /= '.local/share/CodeBuddyExtension/Data/Public/auth'
    return base / 'workbuddy-desktop.info'


def store_path() -> Path:
    return Path.home() / '.wb-home-bridge' / 'accounts.json'


def _read(path: Path) -> Any:
    with path.open(encoding='utf-8') as fh:
        return json.load(fh)


def _atomic(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, tmp = tempfile.mkstemp(prefix='.wb-account-', dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, 'w', encoding='utf-8') as fh:
            json.dump(data, fh, ensure_ascii=False, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
        os.chmod(path, 0o600)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def _valid(a: Any) -> bool:
    return isinstance(a, dict) and isinstance(a.get('uid'), str) and bool(a['uid']) and bool(a.get('access_token'))


def _from_auth(root: Any) -> dict[str, Any] | None:
    if not isinstance(root, dict):
        return None
    profile = root.get('account') or {}
    auth = root.get('auth') or {}
    uid = profile.get('uid') or profile.get('id')
    token = auth.get('accessToken') or auth.get('access_token')
    if not uid or not token:
        return None
    return {'uid': uid, 'nickname': profile.get('nickname') or '',
            'access_token': token, 'refresh_token': auth.get('refreshToken') or auth.get('refresh_token') or '',
            'token_type': auth.get('tokenType') or 'Bearer', 'domain': auth.get('domain') or '',
            'expiresAt': auth.get('expiresAt'), 'auth_raw': root, 'profile_raw': profile}


def _load_store(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    data = _read(path)
    if not isinstance(data, list):
        raise SwitchError('账号库格式不正确')
    return [a for a in data if _valid(a)]


def _merge(items: list[dict[str, Any]], incoming: dict[str, Any], *, prefer_existing: bool = False) -> None:
    for i, account in enumerate(items):
        if account['uid'] == incoming['uid']:
            if not prefer_existing:
                items[i] = {**account, **incoming}
            return
    items.append(incoming)


def discover(path: Path | None = None, auth: Path | None = None,
             legacy: Path | None = None) -> dict[str, Any]:
    """Import legacy switcher accounts once and current auth on every read."""
    path = path or store_path()
    auth = auth or auth_path()
    legacy = legacy or Path.home() / '.wb-switch' / 'accounts.json'
    accounts = _load_store(path)
    changed = False
    if legacy.exists():
        for a in _load_store(legacy):
            before = len(accounts)
            _merge(accounts, a, prefer_existing=True)
            changed |= len(accounts) != before
    current = None
    if auth.exists():
        root = _read(auth)
        live = _from_auth(root)
        if live:
            current = live['uid']
            # Keep refreshed live credentials, but retain a readable saved label.
            saved = next((a for a in accounts if a['uid'] == current), None)
            if saved and not isinstance(live['nickname'], str):
                live['nickname'] = saved.get('nickname', '')
            elif saved and not live['nickname']:
                live['nickname'] = saved.get('nickname', '')
            _merge(accounts, live)
            changed = True
    if changed:
        _atomic(path, accounts)
    return {'accounts': [
        {'uid': a['uid'], 'nickname': a.get('nickname') if isinstance(a.get('nickname'), str) else '',
         'current': a['uid'] == current, 'expiresAt': a.get('expiresAt')}
        for a in accounts], 'current_uid': current}


def _session(account: dict[str, Any], existing: dict[str, Any]) -> dict[str, Any]:
    profile = dict(account.get('profile_raw') or {})
    profile['uid'] = account['uid']
    if account.get('nickname'):
        profile['nickname'] = account['nickname']
    raw = account.get('auth_raw') or {}
    auth = dict(raw.get('auth') or {})
    auth.update(accessToken=account['access_token'],
                refreshToken=account.get('refresh_token') or '',
                tokenType=account.get('token_type') or 'Bearer',
                domain=account.get('domain') or auth.get('domain') or '')
    if account.get('expiresAt'):
        auth['expiresAt'] = account['expiresAt']
        auth['expiresIn'] = max(0, int((account['expiresAt'] - time.time() * 1000) / 1000))
    all_accounts = existing.get('allAccounts') or existing.get('accounts') or []
    all_accounts = [a for a in all_accounts if isinstance(a, dict) and a.get('uid') != account['uid']]
    all_accounts.append(profile)
    return {'account': profile, 'auth': auth, 'accounts': all_accounts, 'allAccounts': all_accounts}


def _running() -> bool:
    return any(item['key'] == 'wb' for item in wb_platform.running_clients())


def _launch() -> None:
    if sys.platform != 'darwin':
        raise SwitchError('目前只支持 macOS 上的一键切换')
    app = '/Applications/WorkBuddy.app'
    if not Path(app).exists():
        raise SwitchError('找不到 /Applications/WorkBuddy.app')
    result = subprocess.run(['open', '-a', app], capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise SwitchError('无法重新打开 WorkBuddy：' + result.stderr.strip())


def switch(uid: str, path: Path | None = None, auth: Path | None = None,
           legacy: Path | None = None) -> dict[str, Any]:
    path = path or store_path()
    auth = auth or auth_path()
    view = discover(path, auth, legacy)
    accounts = _load_store(path)
    target = next((a for a in accounts if a['uid'] == uid), None)
    if not target:
        raise SwitchError('未找到这个账号')
    if view['current_uid'] == uid:
        return {'ok': True, 'already_current': True, 'uid': uid}
    if not auth.exists():
        raise SwitchError('找不到 WorkBuddy 登录文件')
    if _running():
        if not wb_platform.request_quit(wb_platform.CLIENTS_BY_KEY['wb']):
            raise SwitchError('WorkBuddy 未能正常退出，请手动退出后重试')
        deadline = time.monotonic() + 20
        while _running() and time.monotonic() < deadline:
            time.sleep(0.25)
        if _running():
            raise SwitchError('WorkBuddy 仍在运行，请手动退出后重试')
    # After quitting, refresh the saved current token if WorkBuddy rotated it.
    discover(path, auth, legacy)
    existing = _read(auth)
    backup_dir = path.parent / 'auth-backups'
    backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    backup = backup_dir / f'workbuddy-{int(time.time() * 1000)}.info'
    shutil.copy2(auth, backup)
    os.chmod(backup, 0o600)
    try:
        _atomic(auth, _session(target, existing))
        written = _read(auth)
        if written['account']['uid'] != uid or written['auth']['accessToken'] != target['access_token']:
            raise SwitchError('登录文件校验失败')
        _launch()
    except Exception:
        shutil.copy2(backup, auth)
        os.chmod(auth, 0o600)
        raise
    return {'ok': True, 'uid': uid, 'backup': str(backup)}
