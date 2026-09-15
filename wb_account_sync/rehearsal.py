"""Fixed synthetic, read-only preview rehearsal. No --home or guard bypass.

All filesystem activity is confined to a newly created temporary fixture. This
exercises preview code, NOT real-client synchronization or migration writes.
"""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sqlite3
import tempfile

from . import core, preview
from .safety import SafetyError

ACCOUNTS = [f"00000000-0000-0000-0000-{n:012d}" for n in (1, 2, 3)]


def run():
    checks = []

    def check(name, passed):
        if not passed:
            raise SafetyError("Synthetic rehearsal failed: " + name)
        checks.append({"name": name, "passed": True})

    with tempfile.TemporaryDirectory(prefix="wb-sharing-demo-") as directory:
        home = Path(directory).resolve() / "synthetic-client"
        skeleton = home / "storage" / "skeleton"
        skeleton.mkdir(parents=True)
        snapshot = skeleton / "account-snapshot.json"
        db = home / "workbuddy.db"
        con = sqlite3.connect(db)
        try:
            con.execute("CREATE TABLE sessions (id TEXT PRIMARY KEY, user_id TEXT, deleted_at INTEGER, "
                        "is_background_automation INTEGER, title TEXT)")
            for i, uid in enumerate(ACCOUNTS):
                con.execute("INSERT INTO sessions VALUES (?, ?, NULL, 0, ?)",
                            (f"synthetic-{i}", uid, f"Synthetic account {i + 1} history"))
            con.execute("INSERT INTO sessions VALUES ('deleted', ?, 1, 0, 'Excluded')", (ACCOUNTS[0],))
            con.execute("INSERT INTO sessions VALUES ('background', ?, NULL, 1, 'Excluded')", (ACCOUNTS[0],))
            con.commit()
        finally:
            con.close()
        sentinel = home / "do-not-copy.synthetic"
        sentinel.write_text("synthetic credential sentinel - not a real secret", encoding="utf-8")
        before = db.read_bytes()
        views = []
        reports = []
        for target in ACCOUNTS:
            snapshot.write_text(json.dumps({"primary": {"uid": target}}), encoding="utf-8")
            reports.append(preview.history_preview(home, ACCOUNTS, target))
            views.append({row["id"] for row in reports[-1]["sessions"]})
        check("Three selected accounts produce the same history-union preview", views[0] == views[1] == views[2])
        check("All three original ordinary histories remain present", len(views[0]) == 3)
        check("Deleted/background histories are excluded", "deleted" not in views[0] and "background" not in views[0])
        check("Preview leaves the synthetic database byte-for-byte unchanged", before == db.read_bytes())
        check("Synthetic credential sentinel stays untouched", sentinel.read_text(encoding="utf-8") == "synthetic credential sentinel - not a real secret")
        try:
            core.validate_plan(reports[0])
        except SafetyError:
            rejected = True
        else:
            rejected = False
        check("Sharing preview cannot be passed to the ownership migration executor", rejected)
        source = {"language": "简体中文", "model": "synthetic-source-model", "showTokensCounter": True,
                  "env": {"API_KEY": "synthetic-secret"}, "hooks": {"run": "must-not-run"},
                  "permissions": {"defaultMode": "bypassPermissions"}, "payment": {"bound": True}}
        target = {"model": "synthetic-target-model"}
        saved = deepcopy((source, target))
        preferences = preview.preference_diff(source, target)
        check("Allowlisted missing preferences are proposed", {r["key"] for r in preferences["changes"]} == {"language", "showTokensCounter"})
        check("Conflicting target preference is preserved", preferences["conflicts"][0]["action"] == "keep_target" and saved == (source, target))
        check("Sensitive settings values are absent from the output", "synthetic-secret" not in json.dumps(preferences) and preferences["excluded_source_key_count"] == 4)
        check("Client integration is explicitly NOT marked complete", all(not r["client_sync_implemented"] and not r["can_apply"] for r in reports))
    return {
        "kind": "synthetic-sharing-rehearsal", "passed": True, "checks": checks,
        "account_count": 3, "ordinary_union_count": 3,
        "real_account_data_accessed": False, "real_client_modified": False,
        "client_sync_implemented": False,
        "scope": "Read-only preview rehearsal only; not an apply/restore or client UI test.",
        "settings_summary": {"proposed": len(preferences["changes"]), "conflicts_preserved": len(preferences["conflicts"]),
                             "sensitive_or_unknown_keys_excluded": preferences["excluded_source_key_count"]},
        "remaining": ["Real-client non-destructive history sharing", "Automatic account-switch integration",
                      "Account-scoped preference writeback", "Rules/skills/connector adapters and authorization review",
                      "Independent full backup and real-client recovery acceptance"],
    }
