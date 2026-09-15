"""Non-mutating sharing discovery. These reports are NOT executable plans.

No client writer, authentication adapter, credential decoder or background watcher
is provided. Settings keys below describe the documented user settings contract,
NOT arbitrary account-scoped preferences, connector state or extension storage.
"""
from __future__ import annotations

from . import core
from .safety import SafetyError, digest, identity, read_json, snapshot_identity, valid_uid

PREFERENCE_TYPES = {
    "language": str,
    "model": str,
    "outputStyle": str,
    "includeCoAuthoredBy": bool,
    "reasoningEffort": str,
    "autoCompactEnabled": bool,
    "alwaysThinkingEnabled": bool,
    "showTokensCounter": bool,
    "promptSuggestionEnabled": bool,
}
# Non-empty bindings require semantic review, even when their bytes are unchanged.
ACCOUNT_BINDINGS = (
    "plugin_context_json", "buddy_binding_json", "buddy_snapshot_id",
    "expert_runtime_identity", "session_settings",
)


def preference_diff(source, target):
    """Return key names/actions only: never export values or guessable value hashes.

    Missing allowlisted keys can be proposed. Conflicts keep the target; no
    recursive merge, no file write, and no implicit enablement of tools/skills.
    Unknown keys are counted, not named: even a user-defined key can be sensitive.
    """
    if not isinstance(source, dict) or not isinstance(target, dict):
        raise SafetyError("Settings must be JSON objects; malformed data is never replaced.")
    changes, conflicts, invalid = [], [], []
    unchanged = 0
    for key, kind in PREFERENCE_TYPES.items():
        if key not in source:
            continue
        value = source[key]
        if type(value) is not kind or (kind is str and (not value or len(value) > 256)):
            invalid.append(key)
            continue
        if key == "reasoningEffort" and value not in {"minimal", "low", "medium", "high", "xhigh", "max"}:
            invalid.append(key)
            continue
        # This is not an execution plan. Value hashes offer no integrity benefit
        # here and can reveal low-entropy preferences via dictionary guesses.
        entry = {"key": key}
        if key not in target:
            changes.append({**entry, "action": "propose_fill_missing"})
        elif type(target[key]) is kind and target[key] == value:
            unchanged += 1
        else:
            conflicts.append({**entry, "action": "keep_target"})
    return {
        "kind": "settings-diff-preview", "read_only": True, "can_apply": False,
        "changes": changes, "conflicts": conflicts, "unchanged": unchanged,
        "invalid_allowlisted_keys": invalid,
        "excluded_source_key_count": len(set(source) - set(PREFERENCE_TYPES)),
        "excluded_target_key_count": len(set(target) - set(PREFERENCE_TYPES)),
        "values_redacted": True,
        "not_supported": ["account-scoped theme/preferences mapping", "rules/skills installation or enablement",
                          "connector configuration/authentication", "permissions/hooks/env", "payment bindings"],
        "note": "Preview only. Shared device settings need no account-to-account copy. No settings were written.",
    }


def preferences_preview(source_path, target_path):
    """Only read the two explicitly selected files. Do not discover user files."""
    return preference_diff(read_json(source_path), read_json(target_path))


def history_preview(home, accounts, target):
    """Inventory the union for explicitly opted-in accounts, without repointing.

    This is intentionally incompatible with core.apply: reassigning sessions
    would not satisfy the requirement that every account retains its history.
    Message bodies, attachments, rules, skills and credentials are not opened.
    """
    target = valid_uid(target)
    if not isinstance(accounts, (list, tuple)) or not 2 <= len(accounts) <= 100:
        raise SafetyError("Choose between 2 and 100 explicit participating accounts.")
    accounts = [valid_uid(uid) for uid in accounts]
    if len(set(accounts)) != len(accounts) or target not in accounts:
        raise SafetyError("Participating accounts must be unique and include the target.")
    accounts = sorted(accounts)
    ident = identity(home)
    current = snapshot_identity(home)
    if current[0] != target:
        raise SafetyError("Target must match the selected home's current account snapshot.")
    inventories, sessions, review_count = [], [], 0
    with core.read_db(home) as con:
        fingerprint = core.schema(con)
        for uid in accounts:
            rows = core.eligible(con, uid)
            total = con.execute("SELECT COUNT(*) FROM sessions WHERE user_id=?", (uid,)).fetchone()[0]
            inventories.append({"account": uid, "ordinary": len(rows),
                                "excluded": total - len(rows), "has_local_rows": total > 0})
            for row in rows:
                columns = dict(row)
                needs_review = any(columns.get(k) not in (None, "", "{}", "null") for k in ACCOUNT_BINDINGS)
                review_count += int(needs_review)
                sessions.append({"id": row["id"], "original_owner": uid,
                                 "fingerprint": core.row_hash(row),
                                 "account_metadata_review_required": needs_review})
    if identity(home) != ident or snapshot_identity(home) != current:
        raise SafetyError("Client identity changed during sharing inspection; discard the preview.")
    warnings = ["Explicit account membership is user input, not proof of cloud authorization.",
                "No session ownership, message body, attachment or setting was changed."]
    if not sessions:
        warnings.append("No eligible local history: this is not successful synchronization.")
    if any(not entry["has_local_rows"] for entry in inventories):
        warnings.append("Some participants have no local rows; check the chosen home and account IDs.")
    report = {
        "kind": "shared-history-preview", "format": 1, "read_only": True, "can_apply": False,
        "home": ident["home"], "target": target, "accounts": inventories, "schema": fingerprint,
        "sessions": sessions, "ordinary_union_count": len(sessions),
        "account_metadata_review_count": review_count, "source_ownership_preserved": True,
        "client_sync_implemented": False, "ui_verified": False, "cloud_verified": False,
        "blockers": ["Non-destructive client history-sharing adapter is not implemented.",
                     "Account/enterprise storage and attachment visibility require client acceptance testing.",
                     "Rules/skills and connector configuration need separate reviewed adapters."],
        "warnings": warnings,
    }
    report["preview_id"] = digest(report)
    return report
