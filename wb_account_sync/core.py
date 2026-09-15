"""Offline, same-home ordinary session ownership changes only.

No automation table, credential, cloud memory or attachment migration is implemented.
Undo records are NOT full data backups.
"""
from __future__ import annotations

import base64
from contextlib import contextmanager
import json
from pathlib import Path
import re
import sqlite3

from . import __version__
from .safety import (SafetyError, atomic_json, digest, identity, read_json,
                     require_no_sidecars, require_offline, require_plain_path,
                     snapshot_identity, state_lock, state_root, fsync_dir)

REQUIRED = {"id", "user_id", "deleted_at", "is_background_automation"}
SUPPORTED = REQUIRED | {
    "cwd", "title", "custom_title", "status", "created_at", "updated_at",
    "is_playground", "source_mode", "mode", "model", "expert_id", "expert_locale",
    "expert_runtime_identity", "expert_marketplace", "permission_mode", "last_activity_at",
    "use_sandbox_cli", "project_id", "plugin_context_json", "last_user_prompt_expert_selection",
    "context_window", "addon_selection", "session_settings", "buddy_snapshot_id",
    "buddy_binding_json", "thought_level",
}
SCOPE = "ordinary-session-ownership-only"
EXCLUDED = ["automation data", "background automation sessions", "deleted sessions",
            "connector credentials", "cloud memory", "channel bindings",
            "cross-home data", "attachment and cloud/UI verification"]


def stat_token(home):
    s = (Path(home) / "workbuddy.db").stat()
    return (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)


@contextmanager
def read_db(home):
    """Immutable read has no side effects; never ignore outstanding WAL changes."""
    ident = identity(home)
    home = Path(ident["home"])
    require_no_sidecars(home)
    before = stat_token(home)
    con = sqlite3.connect((home / "workbuddy.db").as_uri() + "?mode=ro&immutable=1", uri=True)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA query_only=ON")
        yield con
        require_no_sidecars(home)
        if before != stat_token(home):
            raise SafetyError("Database changed during inspection; discard this result and retry offline.")
    finally:
        con.close()


def schema(con):
    info = con.execute("PRAGMA table_info(sessions)").fetchall()
    names = {row[1] for row in info}
    if not REQUIRED <= names or not names <= SUPPORTED:
        raise SafetyError("Unsupported sessions schema. Writes are disabled for unknown/missing columns.")
    primary = [row[1] for row in info if row[5]]
    types = {r[1]: str(r[2]).upper() for r in info}
    if primary != ["id"] or types["id"] != "TEXT" or types["user_id"] != "TEXT":
        raise SafetyError("Unsupported session key/owner schema.")
    sql = con.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='sessions'").fetchone()
    if not sql or not sql[0] or "VIRTUAL TABLE" in sql[0].upper():
        raise SafetyError("Unsupported sessions table.")
    if con.execute("SELECT 1 FROM sqlite_master WHERE type='trigger' AND tbl_name='sessions'").fetchone():
        raise SafetyError("Session triggers are not supported.")
    if con.execute("PRAGMA foreign_key_list(sessions)").fetchone():
        raise SafetyError("Session foreign-key side effects are not supported.")
    # Reject alternate unique keys: incoming FK actions on user_id/composite keys
    # could otherwise cascade into tables outside our permitted ownership scope.
    indexes = []
    for index in con.execute("PRAGMA index_list(sessions)").fetchall():
        escaped = index[1].replace('"', '""')
        columns = [r[2] for r in con.execute(f'PRAGMA index_info("{escaped}")')]
        if index[2] and columns != ["id"]:
            raise SafetyError("Alternate unique session keys may have external side effects.")
        indexes.append([list(index), columns])
    return digest({"columns": [list(r) for r in info], "sql": sql[0], "indexes": indexes})


def row_hash(row, owner=None):
    values = dict(row)
    if owner is not None:
        values["user_id"] = owner
    for key, value in values.items():
        if isinstance(value, bytes):
            values[key] = {"bytes": base64.b64encode(value).decode("ascii")}
    return digest(values)


def eligible(con, owner):
    return con.execute(
        "SELECT * FROM sessions WHERE user_id=? AND deleted_at IS NULL "
        "AND is_background_automation=0 ORDER BY id", (owner,)).fetchall()


def row_map(con, ids):
    result = {}
    for sid in ids:
        row = con.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
        if row is None:
            raise SafetyError("A planned session is missing; refusing partial migration.")
        result[sid] = row
    return result


def doctor(home):
    ident = identity(home)
    uid, _ = snapshot_identity(home)
    with read_db(home) as con:
        fingerprint = schema(con)
        rows = con.execute("SELECT user_id, COUNT(*) AS count FROM sessions "
                           "WHERE deleted_at IS NULL AND is_background_automation=0 "
                           "GROUP BY user_id ORDER BY user_id").fetchall()
    return {"version": __version__, "home": ident["home"], "current_account": uid,
            "scope": SCOPE, "schema": fingerprint,
            "automatic_account_sync": False, "user_settings_sync": False,
            "ordinary_session_counts": [dict(r) for r in rows], "excluded": EXCLUDED,
            "note": "Read-only inspection, not an endorsement of client/cloud compatibility."}


def make_plan(home, source, target):
    from .safety import valid_uid
    source, target = valid_uid(source), valid_uid(target)
    if source == target:
        raise SafetyError("Source and target must differ.")
    ident = identity(home)
    current, snapshot_hash = snapshot_identity(home)
    if current != target:
        raise SafetyError("Target must match the selected home's current account snapshot.")
    with read_db(home) as con:
        fingerprint = schema(con)
        rows = eligible(con, source)
        changes = [{"id": r["id"], "before": row_hash(r), "after": row_hash(r, target)} for r in rows]
    if snapshot_identity(home) != (current, snapshot_hash):
        raise SafetyError("Account changed while building the plan.")
    plan = {"format": 1, "tool_version": __version__, "scope": SCOPE, "identity": ident,
            "source": source, "target": target, "snapshot_hash": snapshot_hash,
            "schema": fingerprint, "changes": changes, "excluded": EXCLUDED}
    plan["plan_id"] = digest(plan)
    return plan


def validate_plan(plan):
    from .safety import valid_uid
    required = {"format", "tool_version", "scope", "identity", "source", "target",
                "snapshot_hash", "schema", "changes", "excluded", "plan_id"}
    if not isinstance(plan, dict) or set(plan) != required:
        raise SafetyError("Invalid plan structure.")
    if plan["format"] != 1 or plan["scope"] != SCOPE or plan["tool_version"] != __version__:
        raise SafetyError("Unsupported plan version/scope.")
    calculated = digest({k: v for k, v in plan.items() if k != "plan_id"})
    if calculated != plan["plan_id"]:
        raise SafetyError("Plan checksum mismatch; regenerate and review the plan.")
    valid_uid(plan["source"])
    valid_uid(plan["target"])
    if plan["source"] == plan["target"]:
        raise SafetyError("Plan source and target must differ.")
    ident = plan["identity"]
    if not isinstance(ident, dict) or set(ident) != {"home", "device", "inode"} or not isinstance(ident["home"], str):
        raise SafetyError("Invalid home identity.")
    if not isinstance(plan["changes"], list) or len(plan["changes"]) > 10000:
        raise SafetyError("Plan supports at most 10,000 sessions per operation.")
    ids = set()
    for change in plan["changes"]:
        if not isinstance(change, dict) or set(change) != {"id", "before", "after"}:
            raise SafetyError("Invalid session change.")
        if not isinstance(change["id"], str) or not change["id"] or change["id"] in ids:
            raise SafetyError("Invalid/duplicate session ID.")
        ids.add(change["id"])
        for key in ("before", "after"):
            if not isinstance(change[key], str) or not re.fullmatch("[a-f0-9]{64}", change[key]):
                raise SafetyError("Invalid row fingerprint.")
    return plan


def load_plan(path):
    return validate_plan(read_json(path))


def check_identity(plan, *, snapshot=True):
    home = plan["identity"]["home"]
    if identity(home) != plan["identity"]:
        raise SafetyError("Client home or database identity changed.")
    if snapshot and snapshot_identity(home) != (plan["target"], plan["snapshot_hash"]):
        raise SafetyError("Account snapshot changed; regenerate the plan.")


def phase(con, plan):
    rows = row_map(con, [c["id"] for c in plan["changes"]])
    before = all(row_hash(rows[c["id"]]) == c["before"] for c in plan["changes"])
    after = all(row_hash(rows[c["id"]]) == c["after"] for c in plan["changes"])
    if not rows:
        return "empty"
    if before or after:
        current_owner = plan["source"] if before else plan["target"]
        other_owner = plan["target"] if before else plan["source"]
        other_hash = "after" if before else "before"
        for change in plan["changes"]:
            row = rows[change["id"]]
            if (row["deleted_at"] is not None or row["is_background_automation"] != 0
                    or row["user_id"] != current_owner
                    or row_hash(row, other_owner) != change[other_hash]):
                raise SafetyError("Plan exceeds ordinary-session ownership scope or has invalid fingerprints.")
        return "before" if before else "after"
    raise SafetyError("Planned content changed or is partially applied; manual review required.")


def verify(plan):
    validate_plan(plan)
    check_identity(plan)
    with read_db(plan["identity"]["home"]) as con:
        if schema(con) != plan["schema"]:
            raise SafetyError("Schema changed since planning.")
        current = phase(con, plan)
    return {"plan_id": plan["plan_id"], "phase": current,
            "verified": current in ("after", "empty"), "sessions": len(plan["changes"]),
            "no_op": current == "empty",
            "scope": SCOPE, "note": ("Empty plan: no history was migrated or synchronized."
                                     if current == "empty" else
                                     "Ownership rows only; UI, cloud and attachments are not verified.")}


def journal_write(path, plan, status):
    atomic_json(path, {"format": 1, "plan": plan, "status": status})


def journal_read(path, plan=None):
    value = read_json(path)
    if not isinstance(value, dict) or set(value) != {"format", "plan", "status"} or value["format"] != 1:
        raise SafetyError("Invalid operation journal.")
    validate_plan(value["plan"])
    if value["status"] not in ("prepared", "committed", "restoring", "restored"):
        raise SafetyError("Unknown journal state.")
    if plan is not None and value["plan"] != plan:
        raise SafetyError("Run journal does not match the confirmed plan.")
    return value


@contextmanager
def write_db(plan):
    home = Path(plan["identity"]["home"])
    check_identity(plan)
    require_offline()
    require_no_sidecars(home)
    con = sqlite3.connect((home / "workbuddy.db").as_uri() + "?mode=rw", uri=True, timeout=0)
    con.row_factory = sqlite3.Row
    try:
        # No creation, journal-mode changes, or permissive foreign-key overrides.
        con.execute("PRAGMA foreign_keys=ON")
        con.execute("PRAGMA synchronous=FULL")
        con.execute("BEGIN EXCLUSIVE")
        check_identity(plan)
        if schema(con) != plan["schema"]:
            raise SafetyError("Schema changed since planning.")
        yield con
    except BaseException:
        con.rollback()
        raise
    finally:
        con.close()


def confirm_plan(plan, confirmation):
    validate_plan(plan)
    if confirmation != plan["plan_id"]:
        raise SafetyError("Pass the full reviewed plan ID with --confirm; no implicit --yes.")


def apply(plan, state_dir, confirmation):
    confirm_plan(plan, confirmation)
    check_identity(plan)
    require_offline()
    require_no_sidecars(plan["identity"]["home"])
    root = state_root(state_dir, plan["identity"]["home"])
    run_dir = root / "runs" / plan["plan_id"]
    require_plain_path(run_dir)
    with state_lock(root, plan["identity"]["home"]):
        if not run_dir.parent.exists():
            run_dir.parent.mkdir(mode=0o700)
        fsync_dir(root)
        if not run_dir.exists():
            run_dir.mkdir(mode=0o700)
        fsync_dir(run_dir.parent)
        journal = run_dir / "journal.json"
        previous = journal_read(journal, plan) if journal.exists() else None
        if previous and previous["status"] in ("restoring", "restored"):
            raise SafetyError("This run is being/has been restored; it cannot be reapplied.")
        with write_db(plan) as con:
            current = phase(con, plan)
            if current == "empty" and eligible(con, plan["source"]):
                raise SafetyError("Source sessions changed after the empty plan; regenerate it.")
            if current in ("after", "empty"):
                if current == "after" and previous is None:
                    raise SafetyError("After-state without this tool's journal; refusing to claim success.")
            elif previous and previous["status"] == "committed":
                raise SafetyError("Committed journal disagrees with live data.")
            else:
                actual = eligible(con, plan["source"])
                if [r["id"] for r in actual] != [c["id"] for c in plan["changes"]]:
                    raise SafetyError("Source sessions changed; regenerate the plan.")
                for row, change in zip(actual, plan["changes"]):
                    if row_hash(row, plan["target"]) != change["after"]:
                        raise SafetyError("Invalid planned after-state.")
                # Durable, restrictive undo metadata BEFORE the first database write.
                journal_write(journal, plan, "prepared")
                for change in plan["changes"]:
                    count = con.execute("UPDATE sessions SET user_id=? WHERE id=? AND user_id=?",
                                        (plan["target"], change["id"], plan["source"])).rowcount
                    if count != 1:
                        raise SafetyError("Unexpected changed-row count; rolling back.")
                if phase(con, plan) not in ("after", "empty"):
                    raise SafetyError("Post-write verification failed.")
            require_offline()
            check_identity(plan)
            con.commit()
        # Crash here leaves prepared + after. Reapplying resumes journal finalization.
        journal_write(journal, plan, "committed")
    result = verify(plan)
    if not result["verified"]:
        raise SafetyError("Post-commit verification failed; review the durable run journal.")
    return {**result, "status": "committed", "run_dir": str(run_dir),
            "undo_is_full_backup": False}


def restore(run_dir, confirmation):
    run_dir = require_plain_path(run_dir)
    journal = run_dir / "journal.json"
    recorded = journal_read(journal)
    plan = recorded["plan"]
    confirm_plan(plan, confirmation)
    check_identity(plan)
    require_offline()
    require_no_sidecars(plan["identity"]["home"])
    if run_dir.name != plan["plan_id"] or run_dir.parent.name != "runs":
        raise SafetyError("Run directory does not match the plan ID.")
    root = state_root(run_dir.parent.parent, plan["identity"]["home"])
    with state_lock(root, plan["identity"]["home"]):
        recorded = journal_read(journal, plan)
        with write_db(plan) as con:
            current = phase(con, plan)
            if current == "after":
                if recorded["status"] == "restored":
                    raise SafetyError("Restored journal disagrees with live data.")
                journal_write(journal, plan, "restoring")
                for change in plan["changes"]:
                    count = con.execute("UPDATE sessions SET user_id=? WHERE id=? AND user_id=?",
                                        (plan["source"], change["id"], plan["target"])).rowcount
                    if count != 1:
                        raise SafetyError("Unexpected restore row count; rolling back.")
                if phase(con, plan) not in ("before", "empty"):
                    raise SafetyError("Restore verification failed.")
            elif current == "before" and recorded["status"] == "committed":
                raise SafetyError("Unexplained ownership reversal; refusing to claim a restore.")
            require_offline()
            check_identity(plan)
            con.commit()
        journal_write(journal, plan, "restored")
    return {"plan_id": plan["plan_id"], "status": "restored", "sessions": len(plan["changes"]),
            "scope": SCOPE, "note": "Only the recorded ownership changes were undone."}
