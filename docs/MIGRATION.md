# Migration from the legacy tool

**Target: 0.3.0a1 safety preview, not a production upgrade.**
A manual transition from broad live sync to narrow offline ownership changes, not reversal of all legacy effects.
It cannot certify historical credential isolation. Only migrate accounts/data you personally have authority over.

## 1. Preserve a recovery option

Use official export where available and/or your own backup; the core provides no full backup or rollback guarantee.
Keep backups privately; do not delete pools, snapshots, journals, or client files as cleanup.
Legacy snapshots/pools are not new plans, state directories, or ownership-undo journals.
Do not run old `backup`, `adopt`, `revert`, or snapshot `restore` as preparation.

## 2. Stop the old behavior manually

Upgrading the repository/package does **not** stop legacy code already running in memory.
If needed, select the intended target, then fully quit WorkBuddy with Command-Q; closing a window is insufficient.
The target must remain the snapshot's current `primary.uid`.
In your own macOS terminal, unload the known legacy service if installed:

```sh
launchctl bootout gui/$(id -u)/com.workbuddy.account-sync
```

This is a user action, not something the CLI executes. It unloads the current service, not its persisted definition;
a remaining LaunchAgent may load again at a later login. Manually disable/remove that specific definition
through your normal macOS administration workflow; do not use broad cleanup commands.
Stop any separately started legacy `live` process through its owning terminal.
Confirm client and legacy processes are stopped before each real-data operation.
Investigate failed `bootout`: neither an error nor an updated source tree proves shutdown.
No sandbox bypass, helper app, or `sudo` workaround. If offline state cannot be established, stop.

## 3. Install only the safe implementation

See [README](../README.md) for branch/venv, optional `pipx install .`, and conditional offline-wheel installation.
GitHub PR/prerelease wheel availability is not assumed; no PyPI package, signed GUI, or updater is claimed.
All three entry points must reach `wb_account_sync`, never the legacy script.
If the package is absent or the wrapper still invokes legacy code, wait; do not execute old code as a safety probe.

## 4. Replace old commands with explicit decisions

| Old command | New behavior / manual replacement |
|---|---|
| `sync` | Refuse with exit 2; review an explicit offline plan, then apply |
| `live` | Refuse with exit 2; no background replacement |
| `daemon-install` | Refuse with exit 2; no service installation |
| `daemon-uninstall` | Refuse with exit 2; use the manual shutdown procedure above |
| `backup` | Refuse with exit 2; use official export or your own backup |
| `adopt` | Refuse with exit 2; use plan/apply for eligible sessions only |
| `revert` | Refuse with exit 2; use guarded restore of a new run journal |
| Legacy snapshot `restore` | Unsupported; new restore takes `--run-dir` and full `--confirm` |
| `status` | May alias offline `doctor`, not legacy all-asset inventory |

Retired commands must explain the new workflow, never dispatch legacy code.
No compatibility alias may silently transfer automations, credentials, channels, or memory.

## 5. Review, apply, verify, and retain evidence privately

Follow the [README command sequence](../README.md), replacing placeholders locally.
Select one explicit home and strictly valid full source/current-target UUIDs.
`doctor`, `plan`, and `verify` require client/old daemon shutdown even though they only read.
Non-empty WAL, active SQLite journals, or unknown schemas mean refusal, not bypass flags or DB copies.
Plan creates no file unless `--output` is explicit; review it before confirming the complete plan ID.
Apply needs an independent state directory outside client home; keep it and the plan private (UUIDs/paths).

Stay offline for verify/restore. Verify proves row ownership only, not UI visibility or cloud-sync behavior.
For interrupted `prepared` runs, repeat apply with the same plan and `--state-dir`; retain `STATE_DIR/runs/PLAN_ID`:
exact after-state only finalizes commit; exact before-state allows retry; drift refuses. Successful reapply is idempotent.
Restore undoes affected ownership only and refuses content drift to protect later modifications.
Do not replace the DB, delete WAL/journals, or force recovery via old snapshot restore.
Reopening the client can alter local/cloud state; see [safety](SAFETY.md) and [roadmap](ROADMAP.md).
