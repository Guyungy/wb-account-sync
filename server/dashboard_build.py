#!/usr/bin/env python3
"""Build the private WorkBuddy dashboard and durable per-day snapshots from run logs."""
from __future__ import annotations

import datetime as dt
import grp
import json
import os
import re
import sqlite3
import tempfile
from pathlib import Path

TZ = dt.timezone(dt.timedelta(hours=8))
ROOT = Path(os.environ.get('WB_DAILY_ROOT', '/opt/workbuddy-daily'))
WEB = Path(os.environ.get('WB_DASHBOARD_ROOT', '/var/www/workbuddy-dashboard'))
STATE = Path(os.environ.get('WB_PORTAL_STATE', '/var/lib/workbuddy-portal'))
LOG_DIR = Path(os.environ.get('WB_DAILY_LOGS', '/var/log/workbuddy-daily'))
LOGS = [LOG_DIR / name for name in ('manual-verify.log', 'manual-current.log', 'daily.log')]
DB = STATE / 'metrics.sqlite'


def mask(phone: str) -> str:
    if phone == '64455604':
        return '+852 6445 ****'
    return phone[:3] + '****' + phone[-4:] if len(phone) == 11 and phone.isdigit() else phone


def _atomic_json(path: Path, payload: object, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix='.wb-dashboard-', dir=path.parent)
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, 'w', encoding='utf-8') as out:
            json.dump(payload, out, ensure_ascii=False, indent=2)
            out.flush()
            os.fsync(out.fileno())
        os.replace(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def parse_logs(paths: list[Path]) -> list[tuple[str, str, str, object]]:
    events: list[tuple[str, str, str, object]] = []
    for path in paths:
        if not path.exists():
            continue
        run_date = dt.datetime.fromtimestamp(path.stat().st_mtime, TZ).date().isoformat()
        active: dict[str, str] = {}
        for line in path.read_text(errors='replace').splitlines():
            marker = re.search(r'=== WorkBuddy run (\d{4}-\d{2}-\d{2})', line)
            if marker:
                run_date = marker.group(1)
                active = {}
                continue
            stamp = re.match(r'\[(\d{2}:\d{2}:\d{2})\]\[账号(\d+)\]', line)
            if not stamp:
                continue
            when, idx = run_date + ' ' + stamp.group(1), stamp.group(2)
            banner = re.search(r'╭─ 👤 账号\d+\s+(\S+)', line)
            if banner:
                active[idx] = banner.group(1)
            phone = active.get(idx)
            if not phone:
                continue
            credit = re.search(r'💰 积分: 主套餐剩余([\d.]+)积分\(共([\d.]+),已用([\d.]+)\)', line)
            if credit:
                addon = re.search(r'加量包\d+剩余([\d.]+)积分\(共([\d.]+),已用([\d.]+)\)', line)
                main_values = [float(x) for x in credit.groups()]
                addon_values = [float(x) for x in addon.groups()] if addon else [0., 0., 0.]
                events.append((when, phone, 'credits', {
                    'remaining': round(main_values[0] + addon_values[0], 2),
                    'total': round(main_values[1] + addon_values[1], 2),
                    'used': round(main_values[2] + addon_values[2], 2),
                    'main_remaining': main_values[0], 'addon_remaining': addon_values[0],
                }))
            summary = re.search(r'🏁\s+(\S+): 完成(\d+)/(\d+) 等级(\d+) 剩余:\s*(.*)', line)
            if summary:
                events.append((when, phone, 'summary', {
                    'completed': int(summary.group(2)), 'total': int(summary.group(3)),
                    'level': int(summary.group(4)),
                    'remaining': [s.strip() for s in summary.group(5).split(',') if s.strip()],
                }))
            if '✅签到成功' in line:
                events.append((when, phone, 'sign_in', '签到成功'))
            elif '今天已签到' in line:
                events.append((when, phone, 'sign_in', '今日已签到'))
    return sorted(events)


def aggregate(events: list[tuple[str, str, str, object]]) -> dict[tuple[str, str], dict]:
    daily: dict[tuple[str, str], dict] = {}
    for when, phone, kind, value in events:
        key = (when[:10], phone)
        row = daily.setdefault(key, {'date': when[:10], 'phone': phone,
            'sign_in': '暂无记录', 'completed': None, 'total': None, 'level': None,
            'remaining': [], 'last_run': None, 'credits': None})
        row['last_run'] = max(when, row['last_run'] or when)
        if kind == 'summary':
            row.update(value)
        elif kind == 'credits':
            row['credits'] = value
        else:
            row['sign_in'] = value
    return daily


def save_daily(rows: dict[tuple[str, str], dict]) -> None:
    STATE.mkdir(parents=True, exist_ok=True, mode=0o700)
    group = os.environ.get('WB_PORTAL_GROUP', 'workbuddyportal')
    try:
        portal_gid = grp.getgrnam(group).gr_gid
    except KeyError:
        portal_gid = None
    if portal_gid is not None:
        os.chown(STATE, 0, portal_gid)
        os.chmod(STATE, 0o750)
    else:
        os.chmod(STATE, 0o700)
    with sqlite3.connect(DB) as db:
        db.execute('''CREATE TABLE IF NOT EXISTS daily (
            day TEXT NOT NULL, phone TEXT NOT NULL, snapshot TEXT NOT NULL,
            PRIMARY KEY(day, phone))''')
        for (day, phone), row in rows.items():
            db.execute('INSERT INTO daily(day,phone,snapshot) VALUES(?,?,?) '
                       'ON CONFLICT(day,phone) DO UPDATE SET snapshot=excluded.snapshot',
                       (day, phone, json.dumps(row, ensure_ascii=False)))
    if portal_gid is not None:
        os.chown(DB, 0, portal_gid)
        os.chmod(DB, 0o640)
    else:
        os.chmod(DB, 0o600)


def main() -> None:
    accounts = json.loads((ROOT / 'wb_refresh_tokens.json').read_text())
    daily = aggregate(parse_logs(LOGS))
    save_daily(daily)
    entries = []
    history = []
    with sqlite3.connect(DB) as db:
        for phone, saved in accounts.items():
            found = db.execute('SELECT day,snapshot FROM daily WHERE phone=? ORDER BY day DESC LIMIT 1',
                               (phone,)).fetchone()
            row = json.loads(found[1]) if found else {'sign_in': '暂无记录',
                'completed': None, 'total': None, 'level': None, 'remaining': [],
                'last_run': None, 'credits': None}
            if found and found[0] != dt.datetime.now(TZ).date().isoformat():
                row['sign_in'] = '今日暂无记录'
            row.pop('phone', None)
            row.pop('date', None)
            row.update(account=mask(phone), token_updated=saved.get('updated'))
            entries.append(row)
        for day, phone, snapshot in db.execute('SELECT day,phone,snapshot FROM daily ORDER BY day DESC,phone'):
            if phone not in accounts:
                continue
            row = json.loads(snapshot)
            row.pop('phone', None)
            row['account'] = mask(phone)
            history.append(row)
    now = dt.datetime.now(TZ).strftime('%Y-%m-%d %H:%M:%S')
    _atomic_json(WEB / 'data.json', {'generated_at': now, 'accounts': entries,
        'schedule': ['07:00', '12:00', '23:30']})
    _atomic_json(WEB / 'history.json', {'generated_at': now, 'days': history})
    print('Dashboard updated:', len(entries), 'accounts,', len(history), 'daily snapshots')


if __name__ == '__main__':
    main()
