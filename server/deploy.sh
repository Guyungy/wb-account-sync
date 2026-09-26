#!/usr/bin/env bash
set -euo pipefail
umask 077
src=/tmp/workbuddy-portal-deploy
backup=/root/workbuddy-portal-backup-$(date +%Y%m%d-%H%M%S)
mkdir -m 700 "$backup"
cp -a /etc/caddy/Caddyfile /usr/local/bin/workbuddy-dashboard-build /var/www/workbuddy-dashboard/index.html /opt/workbuddy-daily/workbuddy_daily.py /opt/workbuddy-daily/workbuddy_login.py /opt/workbuddy-daily/wb_refresh_tokens.json "$backup/"

exec 9>/var/lock/workbuddy-daily.lock
flock -x 9

if ! id workbuddyportal >/dev/null 2>&1; then
  useradd --system --user-group --home-dir /var/lib/workbuddy-portal/app --shell /sbin/nologin workbuddyportal
fi
install -d -m 750 -o root -g workbuddyportal /var/lib/workbuddy-portal
install -d -m 700 -o workbuddyportal -g workbuddyportal /var/lib/workbuddy-portal/app
install -d -m 755 -o root -g root /opt/workbuddy-portal
install -m 755 "$src/portal.py" /opt/workbuddy-portal/portal.py
install -m 644 "$src/portal.html" /opt/workbuddy-portal/portal.html
install -m 755 "$src/import_enrollments.py" /opt/workbuddy-portal/import_enrollments.py
install -m 755 "$src/dashboard_build.py" /usr/local/bin/workbuddy-dashboard-build
install -m 644 "$src/dashboard.html" /var/www/workbuddy-dashboard/index.html
install -m 644 "$src/workbuddy-portal.service" /etc/systemd/system/workbuddy-portal.service

python3 - <<'PY'
from pathlib import Path
p=Path('/etc/caddy/Caddyfile')
s=p.read_text()
needle='\t@workbuddy path /workbuddy /workbuddy/*'
assert needle in s, 'Cannot locate existing WorkBuddy route'
if '@workbuddyPortal path' in s:
    raise SystemExit(0)
insert='''\t@workbuddyPortalRoot path /workbuddy/join
\tredir @workbuddyPortalRoot /workbuddy/join/ 308
\t@workbuddyPortal path /workbuddy/join/*
\thandle @workbuddyPortal {
\t\turi strip_prefix /workbuddy/join
\t\treverse_proxy 127.0.0.1:18886 {
\t\t\theader_up X-Real-IP {remote_host}
\t\t}
\t}
'''
p.write_text(s.replace(needle, insert+needle, 1))
PY

# All WorkBuddy endpoints have valid public certificates. Protect refresh tokens in transit.
python3 - <<'PY'
from pathlib import Path
for name in ('workbuddy_daily.py','workbuddy_login.py'):
    p=Path('/opt/workbuddy-daily')/name
    s=p.read_text()
    p.write_text(s.replace('verify=False', 'verify=True'))
PY
python3 -m py_compile /opt/workbuddy-portal/portal.py /opt/workbuddy-portal/import_enrollments.py /usr/local/bin/workbuddy-dashboard-build /opt/workbuddy-daily/workbuddy_daily.py
/usr/local/bin/workbuddy-dashboard-build
/usr/bin/caddy validate --config /etc/caddy/Caddyfile
systemctl daemon-reload
systemctl enable --now workbuddy-portal.service
systemctl reload caddy
cat > /etc/cron.d/workbuddy-portal <<'EOF'
* * * * * root /usr/bin/python3 /opt/workbuddy-portal/import_enrollments.py >> /var/log/workbuddy-portal-import.log 2>&1
EOF
chmod 644 /etc/cron.d/workbuddy-portal
printf 'Installed. Backup: %s\n' "$backup"
