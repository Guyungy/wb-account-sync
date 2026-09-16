# Safety contract

**0.3.0a1 is an alpha safety preview, not production-ready.** These are gates, not completed validation.
Only migrate accounts/data you personally have authority to migrate.
This unofficial tool depends on private client storage; client/cloud effects are unverified.

## Allowed change and trust boundary

- One explicit client home; no home discovery or cross-directory account transfer.
- Only ordinary, non-deleted `sessions` rows; change only their `user_id` ownership.
- Exclude background automation sessions using supported session metadata.
  If ordinary sessions cannot be distinguished safely, refuse rather than broaden selection.
- Do not access the `automations` table or migrate automation state.
- Do not copy bodies, attachments, credentials, authorization/master keys, channels,
  cloud memory, or other account assets; do not restore a whole database.
- Strictly validate full source/target UUIDs, never prefixes or wildcards.
  Target must equal the selected home's account-snapshot current `primary.uid`.
- Detect supported ordinary-session base fields and schema fingerprints only; not all-5.5.x compatibility.

## Read-only sharing and preference previews (unreleased)

- `demo` accepts no home/path override, constructs only temporary synthetic data, and
  exercises ten preview assertions. It does not bypass the process guard, use the
  migration writer, or prove real-client apply/restore, UI or cloud compatibility.
- `share-preview` only inventories explicitly chosen UUIDs and keeps original ownership.
  Its output is not an executable migration plan; `can_apply` and
  `client_sync_implemented` must remain false. UUID selection does not prove authority.
- Sharing output includes paths, account/session identifiers and row fingerprints.
  It is not anonymous; keep it local. No message bodies or attachments are opened.
- `preferences-preview` reads only the two explicitly selected JSON files. Known
  preference keys produce actions, not values or value hashes. Unknown key names
  and values are not exported. It does not infer account-scoped file layouts.
- General settings may coexist with secrets or executable configuration in the same
  JSON file. Never recursively copy it: hooks, env, permissions, authorization,
  payment bindings, channels, rules/skill activation and connector state are excluded.
- The preview does not write settings, synchronize skills/rules, or enable connectors.
  Same-home shared device settings do not require cross-account copies.
- Path validation rejects `..` and symlink components before canonicalization and
  containment checks. This is not a guarantee against malicious concurrent filesystem
  replacement; use trusted directories and keep the client stopped for real-data reads.

## Offline and database gates

Quit the client and stop the old daemon before `doctor`, `plan`, or `verify` reads real data.
The planner reads real rows; read-only does not mean safe while the client is running.
Recheck offline state before writes; uncertain process/lock state must cause refusal.
`apply` and `restore` must refuse non-macOS platforms. Linux is mock-fixture-only.

`doctor`, `plan`, and `verify` must refuse non-empty WAL, active SQLite transaction
journals, and unknown schemas. No copying the database or ignoring WAL to manufacture
an apparently complete read. Do not checkpoint, delete, or truncate those files to bypass checks.
The SQLite transaction journal is distinct from the tool's persistent run journal.
Independent backups are precautions, not alternate databases that evade these gates.

## Plan and execution integrity

Freeze selected home, source/target, DB inode, schema fingerprint, account-snapshot digest,
and per-affected-row hashes for before/after comparison; changed or unexpected inputs must refuse.
A plan alone is not authorization: apply needs its complete matching `PLAN_ID`.
Do not edit a plan to bypass refusal; investigate and generate a newly reviewed plan when appropriate.

Planning must not modify client data. Without explicit `--output`, stdout is the only output;
no default plan file, backup, or run-state directory. Explicit output belongs outside client data.
Apply requires an independent user-selected state directory outside the client home.
Persist runs under `STATE_DIR/runs/PLAN_ID`; never silently choose a real user directory.

Apply rechecks offline status, acquires locks, and checks row hashes inside the write transaction.
A preflight check alone cannot authorize a later write. Only planned affected rows may change.
Persist undo and `prepared` durably before DB mutation commit, then durably record `committed`.
Unexpected/partial state must refuse; a journal write failure must not be reported as full success.

## Persistent run state and interruption recovery

| Observed state | Permitted action after identity, offline, lock, and hash checks |
|---|---|
| `prepared`, all affected rows match exact before-state | Retry the planned transaction |
| `prepared`, all affected rows match exact after-state | Only finalize `committed`; do not apply again |
| `committed`, expected after-state intact | Repeated apply is idempotent; no duplicate migration |
| Mixed state, unexpected ownership/content, or invalid journal | Refuse; preserve evidence for review |
| Successful guarded restore | Persist `restored`; never treat it as a pending apply |

A restored run is not an implicit request to reapply; a new migration needs explicit review.
Do not discard `prepared` records or locks to force another run.
Keep the same plan/state directory for interrupted apply; automatic recovery is not guaranteed.

## Undo and verification limits

Undo records only affected ownership values plus row identifiers/fingerprints needed to match them.
It is not a full-row export, full database backup, conversation copy, or attachment backup.
Restore requires the complete matching plan ID, offline checks, locks, and row validation.
If affected-row content has drifted, refuse before changing ownership; protect subsequent edits.
Restore must not overwrite unrelated rows or replace the database.
Missing/corrupt run state is not permission to guess old ownership or use legacy snapshot restore.

Verify proves only local row ownership, not UI visibility, client correctness, or cloud synchronization.
Reopening WorkBuddy may alter local/cloud state and invalidate undo.
Use official export where available and/or your own backup first. Automatic rollback is not promised.

## Privacy and operational limits

Plans contain UUIDs and paths; journals/diagnostics may also expose identifying metadata.
Do not publish raw plans, run directories, logs, snapshots, or database samples.
Do not claim fully redacted logs or anonymous metadata without evidence; use synthetic support examples
and manually review any shared excerpt.
The tool must not manage services, bypass sandbox denials, or launch a helper app.
Updating files does not stop an in-memory old daemon; see [migration](MIGRATION.md).
