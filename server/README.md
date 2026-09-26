# WorkBuddy Daily server extension

`dashboard_build.py` parses run logs into durable daily snapshots in `/var/lib/workbuddy-portal/metrics.sqlite`, then publishes masked admin dashboard JSON. `portal.py` provides invite-only, SMS-verified enrollment. It runs as an unprivileged user and cannot read the runner's existing token store. `import_enrollments.py` imports verified credentials from the private queue under the runner's lock.

The public entry is `/workbuddy/join/` on the same HTTPS host. The `/workbuddy/` daily dashboard and `/workbuddy/admin/` account management panel remain behind Caddy basic authentication. The management panel shows unused links, creates new links, reviews guest account states, and queues removal. Its API additionally requires a private proxy header. New users need an invite code and a WorkBuddy SMS code. Returning users can log in by SMS. Each session only receives its own account records. Portal credential cookies are Secure, HttpOnly and SameSite=Strict. New credentials are written to mode 0600 queue files and imported into the runner's mode 0600 token store. The portal never exposes access or refresh tokens to the browser.

Deploy the server files with `deploy.sh`. It installs `portal.py`, `portal.html`, `admin.html`, and `import_enrollments.py` to `/opt/workbuddy-portal`, sets up the protected Caddy routes, and configures the unprivileged service. Set `WB_PORTAL_GROUP=workbuddyportal` for the dashboard builder, so the portal can read its per-account metrics. The importer runs every minute. Create and copy invite links from `/workbuddy/admin/`.

Only deploy the invite portal on an HTTPS origin. Do not run it as root or make the runner token store readable by the portal user. The SMS API uses normal TLS certificate verification.
